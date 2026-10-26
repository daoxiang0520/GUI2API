"""Session-bound multimodal resources. Discovery never fetches resource bytes."""
from collections import OrderedDict
import base64
import hashlib
import json
import os
from pathlib import Path
import struct
import tempfile
import time
import uuid

from object_bridge import Fault

KINDS = {'image','audio','video','document','file','text'}
MAX_BYTES = 256 * 1024 * 1024
ROOT = Path(__file__).resolve().parent


def media_info(prefix, declared=None):
    """Signature detection is not a decoder or proof of semantic content."""
    detected=None;dimensions={}
    if prefix.startswith(b'\x89PNG\r\n\x1a\n') and len(prefix)>=24:
        detected='image/png';dimensions=dict(zip(('width','height'),struct.unpack('>II',prefix[16:24])))
    elif prefix.startswith((b'GIF87a',b'GIF89a')) and len(prefix)>=10:
        detected='image/gif';dimensions=dict(zip(('width','height'),struct.unpack('<HH',prefix[6:10])))
    elif prefix.startswith(b'\xff\xd8\xff'):detected='image/jpeg'
    elif prefix.startswith(b'RIFF') and prefix[8:12]==b'WEBP':detected='image/webp'
    elif prefix.startswith(b'RIFF') and prefix[8:12]==b'WAVE':detected='audio/wav'
    elif prefix.startswith(b'OggS'):detected='audio/ogg' if not (declared or '').startswith('video/') else declared
    elif prefix.startswith(b'ID3') or prefix[:2] in (b'\xff\xfb',b'\xff\xf3',b'\xff\xf2'):detected='audio/mpeg'
    elif prefix.startswith(b'\x1aE\xdf\xa3'):detected='audio/webm' if (declared or '').startswith('audio/') else 'video/webm'
    elif len(prefix)>=12 and prefix[4:8]==b'ftyp':detected='audio/mp4' if (declared or '').startswith('audio/') else 'video/mp4'
    elif prefix.startswith(b'%PDF-'):detected='application/pdf'
    value=detected or declared or 'application/octet-stream'
    if not isinstance(value,str) or len(value)>120 or '/' not in value:value='application/octet-stream'
    value=value.split(';')[0].lower()
    return value,{'signature_detected':detected is not None,'fully_decoded':False,**dimensions}


def resource_kind(mime, hint='file'):
    for kind in ('image','audio','video'):
        if mime.startswith(kind+'/'):return kind
    if mime.startswith('text/') or mime in ('application/json','application/xml'):return 'text'
    if mime=='application/pdf' or any(value in mime for value in ('officedocument','msword','opendocument')):return 'document'
    return hint if hint in KINDS else 'file'


class ArtifactStore:
    """Private immutable artifacts; paths are generated, never supplied by a page."""
    def __init__(self, root=None):
        self.root=Path(root or os.environ.get('GUI2API_RESOURCE_ROOT') or ROOT/'.cache/resources').resolve()
        self.directory=None;self.artifacts={};self.total=0
    def temporary(self):
        if self.directory is None:
            self.root.mkdir(parents=True,exist_ok=True)
            self.directory=Path(tempfile.mkdtemp(prefix='session-',dir=self.root)).resolve()
        return tempfile.NamedTemporaryFile(prefix='transfer-',suffix='.part',dir=self.directory,delete=False)
    def commit(self, path, digest, size, mime, metadata):
        if self.total+size>MAX_BYTES:raise Fault('resource_storage_limit','Session artifact quota exceeded; release owned artifacts explicitly')
        path=Path(path).resolve();assert path.parent==self.directory
        token=uuid.uuid4().hex
        extensions={'image/png':'.png','image/jpeg':'.jpg','image/webp':'.webp','image/gif':'.gif',
            'audio/wav':'.wav','audio/mpeg':'.mp3','audio/ogg':'.ogg','audio/webm':'.webm','video/webm':'.webm',
            'video/mp4':'.mp4','application/pdf':'.pdf','text/plain':'.txt','text/vtt':'.vtt','application/json':'.json'}
        target=self.directory/(token+extensions.get(mime,'.bin'))
        path.replace(target)
        record={'artifact_id':token,'uri':'gui2api://artifacts/'+token,'path':str(target),'mime_type':mime,'bytes':size,
            'sha256':digest,'content_complete':True,'retention':'until explicit artifact cleanup','original_quality_verified':False,**metadata}
        record.update(chunk_uri_template=record['uri']+'/bytes/{offset}/{length}',chunk_max_bytes=1048576)
        self.artifacts[token]=record;self.total+=size
        return dict(record)
    def read(self, token, offset=0, length=None):
        record=self.artifacts.get(token)
        if not record:raise Fault('stale_resource','Artifact is not owned by this resource session')
        path=Path(record['path'])
        if path.is_symlink() or path.resolve().parent!=self.directory:raise Fault('resource_changed','Artifact path changed')
        if type(offset) is not int or offset<0 or offset>record['bytes'] or length is not None and (type(length) is not int or length<1):
            raise Fault('invalid_arguments','Invalid artifact byte range')
        with path.open('rb') as handle:
            digest=hashlib.sha256();size=0
            for block in iter(lambda:handle.read(1048576),b''):digest.update(block);size+=len(block)
            if size!=record['bytes'] or digest.hexdigest()!=record['sha256']:raise Fault('resource_changed','Artifact contents changed')
            handle.seek(offset);data=handle.read() if length is None else handle.read(length)
        return dict(record),data
    def delete(self, token):
        record=self.artifacts.get(token)
        if not record:return False
        path=Path(record['path'])
        if path.is_symlink() or path.resolve().parent!=self.directory:raise Fault('resource_changed','Artifact path changed')
        path.unlink(missing_ok=True);self.total-=record['bytes'];del self.artifacts[token]
        return True


class Resources:
    def __init__(self, gui, root=None, capture=None):
        self.gui=gui;self.store=ArtifactStore(root);self.capture=capture
        self.records=OrderedDict();self.keys={};self.nonce=uuid.uuid4().hex[:12]
    def _specs(self, row):
        if row.get('password'):return []
        specs=row.get('resources',row.get('_resources',[]))
        if not isinstance(specs,list):return []
        specs=[s for s in specs if isinstance(s,dict) and isinstance(s.get('provider_ref'),str) and s.get('kind') in KINDS]
        if row.get('role') in ('Image','Canvas') and not specs:
            specs=[{'provider_ref':'rendered:'+row['native_ref'],'kind':'image','mime_type':'image/png',
                'name':row.get('name',''),'relation':'rendered_control','source_readable':False,
                'evidence':'Provider image/canvas role; no original resource bytes exposed'}]
        return specs
    def _register(self, source, row, spec):
        key=(source['id'],row['ref'],spec['provider_ref'])
        revision=self.gui._event_state(source).get('revision') if not source.get('adapter') else None
        key=(*key,revision)
        token=self.keys.get(key)
        if token not in self.records:
            token='r'+self.nonce+'.'+uuid.uuid4().hex[:12];self.keys[key]=token
        safe={name:spec[name] for name in ('kind','mime_type','name','relation','scheme','size_bytes','width','height',
            'duration_seconds','loaded','stream_only','generated','evidence') if name in spec}
        adapter=source.get('adapter')
        rendered=not adapter or getattr(adapter,'supports_resource_capture',False) or spec.get('generated')=='canvas'
        descriptor={'resource_ref':token,'control_ref':row['ref'],'source':source['id'],**safe,
            'availability':{'metadata':True,'source_bytes':bool(spec.get('source_readable')),'rendered':bool(rendered),
                            'verification':'advertised; bytes not yet read'},
            'original_quality_verified':False,'business_meaning_verified':False,'metadata_is_observation':True}
        old=self.records.get(token)
        self.records[token]={'descriptor':descriptor,'key':key,'provider_ref':spec['provider_ref'],
            'source':source['id'],'ref':row['ref'],'row':dict(row),'created':time.monotonic(),
            'artifacts':old['artifacts'] if old else [],'native_revision':revision}
        self.records.move_to_end(token)
        while len(self.records)>2048:
            old,item=self.records.popitem(last=False);self.keys.pop(item['key'],None)
        return descriptor
    def list(self, source=None, ref=None, kind=None, refresh=False, limit=100, offset=0):
        if (source is None)==(ref is None):raise Fault('invalid_arguments','Choose exactly one source or ref')
        if kind is not None and (not isinstance(kind,str) or kind not in KINDS):raise Fault('invalid_arguments','Unsupported resource kind')
        if type(refresh) is not bool or type(limit) is not int or not 1<=limit<=1000 or type(offset) is not int or offset<0:
            raise Fault('invalid_arguments','refresh boolean, limit 1..1000 and nonnegative offset required')
        if ref is not None:
            record=self.gui._record(ref);bound=self.gui._source(record['source'])
            current=self.gui._observe_bound(ref)
            if current is None:raise Fault('stale_ref','The original resource control is no longer observable')
            rows=[current]
        else:
            bound=self.gui._source(source)
            self.gui._find({'source':source,'refresh':refresh,'limit':1})
            rows=[self.gui.refs[r['ref']]['row'] for r in bound['collection']['rows'].values() if r['ref'] in self.gui.refs]
        entries=[]
        for row in rows:
            for spec in self._specs(row):
                if kind is None or spec['kind']==kind:entries.append((row,spec))
        # Bind the returned page only. Registering an entire long media list
        # could evict its first page before the caller receives those refs.
        page=[self._register(bound,row,spec) for row,spec in entries[offset:offset+limit]]
        return {'source':bound['id'],'resources':page,'matched':len(entries),
            'has_more':offset+limit<len(entries),'bytes_read':0,'coverage':'provider resource declarations and image/canvas roles only',
            'zero_matches_proves_absence':False}
    def _record(self, token):
        if not isinstance(token,str):raise Fault('invalid_arguments','resource_ref must be a string')
        record=self.records.get(token)
        if not record or time.monotonic()-record['created']>600:raise Fault('stale_resource','List current resources before reading')
        return record
    def _ensure(self, record):
        source=self.gui._source(record['source'])
        if not source.get('adapter') and record['native_revision'] is not None and self.gui._event_state(source).get('revision')!=record['native_revision']:
            raise Fault('stale_resource','The resource window changed; list current resources again')
        try:row=self.gui._observe_bound(record['ref'])
        except Exception as exc:raise Fault('stale_resource','The original resource control is unavailable') from exc
        if row is None or not self.gui._same_bound_identity(record['row'],row):
            raise Fault('stale_resource','The resource control identity changed')
        current=next((s for s in self._specs(row) if s['provider_ref']==record['provider_ref']),None)
        if current is None:raise Fault('stale_resource','The resource source changed; list resources again')
        return source,row,current
    def describe(self, resource_ref):
        record=self._record(resource_ref);source,row,spec=self._ensure(record)
        return {**self._register(source,row,spec),'metadata_checked_live':True,'bytes_read':0}
    def _guards(self, source, refs):
        if not isinstance(refs,list) or len(refs)>4 or any(not isinstance(ref,str) for ref in refs):
            raise Fault('invalid_arguments','guard_refs must contain at most four live control refs')
        for ref in refs:
            record=self.gui._record(ref)
            if record['source']!=source['id']:raise Fault('invalid_target','Resource guards belong to another source')
            current=self.gui._observe_bound(ref)
            if current is None or record['row'].get('name')!=current.get('name') or not self.gui._same_bound_identity(record['row'],current):
                raise Fault('context_changed','Resource guard changed; no content was returned')
    def read(self, resource_ref, representation='source', max_bytes=16777216, max_pixels=1000000,
             max_seconds=20, delivery='reference', guard_refs=None):
        if representation not in ('source','rendered') or delivery not in ('reference','native','text'):
            raise Fault('invalid_arguments','Choose source/rendered and reference/native/text delivery')
        if type(max_bytes) is not int or not 1<=max_bytes<=MAX_BYTES or type(max_pixels) is not int or not 1<=max_pixels<=16777216:
            raise Fault('invalid_arguments','Invalid resource byte or pixel budget')
        if type(max_seconds) not in (int,float) or not 1<=max_seconds<=30:raise Fault('invalid_arguments','max_seconds must be 1..30')
        record=self._record(resource_ref);source,row,spec=self._ensure(record)
        guards=[] if guard_refs is None else guard_refs;self._guards(source,guards)
        adapter=source.get('adapter');stream=None;temporary=None;artifact=None
        started=time.monotonic();digest=hashlib.sha256();size=0;prefix=b'';details={};declared=spec.get('mime_type')
        try:
            if representation=='rendered' and not adapter:
                from resource_capture import capture_control
                capture=self.capture or capture_control
                encoded,details=capture(self.gui,source,row,max_pixels)
                if len(encoded)>max_bytes:raise Fault('resource_too_large','Rendered resource exceeds the byte budget')
                parts=iter([encoded]);declared='image/png'
            elif representation=='rendered' and (getattr(adapter,'supports_resource_capture',False) or spec.get('generated')!='canvas'):
                if not getattr(adapter,'supports_resource_capture',False):raise Fault('resource_capture_unavailable','This transport has no verified rendered-resource reader')
                response=adapter.request({'op':'resource_capture','resource_ref':record['provider_ref'],'max_bytes':max_bytes,'max_pixels':max_pixels})
                self.gui._runtime_error(response)
                encoded=self._decode(response.get('data_base64',''),max_bytes)
                parts=iter([encoded]);declared=response.get('mime_type');details={key:response[key] for key in
                    ('representation','visible_only','clipped','scale') if key in response}
            else:
                if not adapter:raise Fault('resource_source_unavailable','The native provider exposes no source bytes; rendered capture is explicit')
                opened=adapter.request({'op':'resource_open','resource_ref':record['provider_ref'],'representation':representation,
                    'max_bytes':max_bytes,'max_pixels':max_pixels,'timeout_ms':int(max_seconds*1000)})
                self.gui._runtime_error(opened);stream=opened.get('stream')
                if not isinstance(stream,str):raise Fault('resource_unavailable','Provider did not open a resource stream')
                declared=opened.get('mime_type');details={'representation':opened.get('representation','source_bytes'),
                    **{key:opened[key] for key in ('scale','visible_only') if key in opened}}
                def chunks():
                    empty=0
                    while True:
                        if time.monotonic()-started>max_seconds:raise Fault('resource_timeout','Resource transfer exceeded its deadline')
                        part=adapter.request({'op':'resource_chunk','stream':stream,'max_bytes':524288})
                        self.gui._runtime_error(part)
                        data=self._decode(part.get('data_base64',''),524288)
                        if data:empty=0;yield data
                        else:empty+=1
                        if part.get('done') is True:return
                        if empty>3:raise Fault('resource_unavailable','Resource stream made no progress')
                parts=chunks()
            temporary=self.store.temporary();path=Path(temporary.name)
            for part in parts:
                size+=len(part)
                if size>max_bytes:raise Fault('resource_too_large','Resource exceeded the byte budget')
                digest.update(part);prefix=(prefix+part)[:4096];temporary.write(part)
            temporary.close();temporary=None
            self._ensure(record);self._guards(source,guards)
            actual,verification=media_info(prefix,declared)
            artifact=self.store.commit(path,digest.hexdigest(),size,actual,{'kind':resource_kind(actual,spec['kind']),
                'name':str(spec.get('name','')).replace('\\','/').rsplit('/',1)[-1][:256],
                'provider_metadata':{key:spec[key] for key in ('width','height','duration_seconds','relation') if key in spec},
                'declared_mime_type':declared,'format_verification':verification,**details})
            record['artifacts'].append(artifact['artifact_id'])
            result={'resource_ref':resource_ref,'control_ref':record['ref'],'kind':artifact['kind'],'artifact':artifact,
                'delivery_requested':delivery,'source_checked_live':True,'automatic_retries':0,
                'model_understanding_verified':False,'resource_content_is_untrusted':True}
            if delivery=='text':
                if artifact['kind']=='text':
                    raw=path if path.exists() else Path(artifact['path'])
                    with raw.open('rb') as handle:data=handle.read(65537)
                    result['text']={'value':data[:65536].decode('utf-8',errors='replace'),'truncated':len(data)>65536,
                        'encoding':'utf-8','decoding_errors':'replace','source':'resource_bytes','label_only':False}
                else:result['feedback']={'native':False,'reason':'text_extraction_not_provided','next_action':'use_a_document_or_media_decoder'}
            return result
        finally:
            if stream is not None:
                try:adapter.request({'op':'resource_close','stream':stream})
                except Exception:pass
            if temporary is not None:temporary.close()
            if 'path' in locals() and path.exists() and artifact is None:
                if path.resolve().parent==self.store.directory:path.unlink()
    @staticmethod
    def _decode(encoded, maximum):
        if not isinstance(encoded,str) or len(encoded)>(maximum+2)//3*4:raise Fault('resource_too_large','Resource response exceeds its byte budget')
        try:
            data=base64.b64decode(encoded,validate=True)
            if len(data)>maximum:raise Fault('resource_too_large','Decoded response exceeds its byte budget')
            return data
        except (ValueError,TypeError) as exc:raise Fault('resource_unavailable','Provider returned invalid binary data') from exc
    def release(self, resource_ref=None, delete_artifacts=False, artifact_id=None):
        if type(delete_artifacts) is not bool:raise Fault('invalid_arguments','delete_artifacts must be boolean')
        if (resource_ref is None)==(artifact_id is None):raise Fault('invalid_arguments','Choose resource_ref or artifact_id')
        if artifact_id is not None:
            if not isinstance(artifact_id,str):raise Fault('invalid_arguments','artifact_id must be a string')
            if artifact_id not in self.store.artifacts:raise Fault('stale_resource','Artifact is not owned by this resource session')
            return {'released':True,'artifacts_deleted':int(self.store.delete(artifact_id)) if delete_artifacts else 0,
                    'artifact_files_retained':not delete_artifacts}
        if not isinstance(resource_ref,str):raise Fault('invalid_arguments','resource_ref must be a string')
        record=self.records.get(resource_ref)
        if record is None:raise Fault('stale_resource','Resource is not owned by this session')
        deleted=0
        if delete_artifacts:
            for token in record['artifacts']:deleted+=self.store.delete(token)
        self.records.pop(resource_ref,None);self.keys.pop(record['key'],None)
        return {'released':True,'artifacts_deleted':deleted,'artifact_files_retained':not delete_artifacts}
    def release_source(self, source):
        for token,record in list(self.records.items()):
            if record['source']==source:self.records.pop(token);self.keys.pop(record['key'],None)
    def close(self):self.records.clear();self.keys.clear()
