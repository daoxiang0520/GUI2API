"""Multimodal resource contracts, failure boundaries and artifact integrity."""
import base64
import copy
import hashlib
import io
import json
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest

from gui_api import GuiSession
from gui_resources import media_info
from object_bridge import Fault
from resource_capture import png_rgb

PNG=png_rgb(2,1,b'\xff\0\0\0\xff\0')
sound=io.BytesIO()
with wave.open(sound,'wb') as wav:wav.setparams((1,2,8000,0,'NONE','not compressed'));wav.writeframes(b'\0\0'*80)
DATA={'image':PNG,'audio':sound.getvalue(),'video':b'\x1aE\xdf\xa3test-webm-bytes',
      'document':b'%PDF-1.4\n% owned test document\n%%EOF','file':b'\x01\x02\x03owned-binary','text':'owned text 文本'.encode()}
MIME={'image':'image/png','audio':'audio/wav','video':'video/webm','document':'application/pdf','file':'application/octet-stream','text':'text/plain'}


class Adapter:
    supports_bound_observation=True
    supports_resources=True
    supports_resource_capture=False
    def __init__(self):
        self.rows=[];self.calls=[];self.streams={};self.changed=False;self.excess=False;self.no_progress=False
        for kind in DATA:
            self.rows.append({'ref':kind,'parent':None,'role':'Image' if kind=='image' else 'Pane',
                'name':kind,'actions':['get'],'resources':[{'provider_ref':kind+'-source','kind':kind,'mime_type':MIME[kind],
                    'name':kind,'relation':'embedded','source_readable':True,'size_bytes':len(DATA[kind])}]})
    def request(self,args):
        self.calls.append(args);op=args['op']
        if op=='scan':return {'elements':copy.deepcopy(self.rows),'snapshot':'owned','cursor':None,'truncated':False}
        if op=='observe':
            row=next((r for r in self.rows if r['ref']==args['ref']),None)
            return {'fresh':True,'element':copy.deepcopy(row)} if row else {'error':'stale_ref'}
        if op=='resource_open':
            kind=args['resource_ref'].split('-')[0];self.streams[kind]=False
            return {'stream':kind,'mime_type':MIME[kind],'representation':'source_bytes'}
        if op=='resource_chunk':
            if self.no_progress:return {'data_base64':'','done':False}
            done=self.streams[args['stream']];self.streams[args['stream']]=True
            payload=b'' if done else DATA[args['stream']]
            if self.changed:self.rows[0]['resources'][0]['provider_ref']='replaced-source'
            return {'data_base64':base64.b64encode(payload).decode(),'done':not self.excess}
        if op=='resource_close':self.streams.pop(args['stream'],None);return {'closed':True}
        raise AssertionError('Unexpected operation '+op)
    def close(self):return {}


@pytest.fixture
def context(tmp_path):
    adapter=Adapter();gui=GuiSession(discovery_cache=tmp_path/'cache.json',learning_cache=tmp_path/'learning.json',resource_root=tmp_path/'artifacts')
    gui.sources['s']={'id':'s','runtime':'fixture','target':{},'adapter':adapter,'snapshot':None,'bootstrap':None,'lease':None}
    try:yield gui,adapter,tmp_path
    finally:gui.close()


def data(result):assert result['ok'],result;return result['data']
def listing(gui,**args):return data(gui.request('resources',{'source':'s',**args}))['resources']


def test_discovery_preserves_all_resource_kinds_without_reading_bytes_or_leaking_provider_handles(context):
    gui,adapter,_=context
    found=listing(gui)
    assert {row['kind'] for row in found}==set(DATA)
    assert all('provider_ref' not in row and 'uri' not in row for row in found)
    assert all(row['availability']['source_bytes'] and not row['original_quality_verified'] for row in found)
    assert not any(call['op'].startswith('resource_') for call in adapter.calls)
    controls=data(gui.request('find',{'source':'s'}))['elements']
    assert all('resources' not in row and '_resources' not in row for row in controls)
    assert controls[0]['resource_kinds']==['image']


@pytest.mark.parametrize('kind',list(DATA))
def test_each_modality_returns_complete_verified_artifact(context,kind):
    gui,adapter,root=context
    ref=listing(gui,kind=kind)[0]['resource_ref']
    result=data(gui.request('resource_read',{'resource_ref':ref}))
    artifact=result['artifact'];payload=Path(artifact['path']).read_bytes()
    assert payload==DATA[kind] and artifact['sha256']==hashlib.sha256(payload).hexdigest()
    assert artifact['kind']==kind and artifact['mime_type']==MIME[kind] and artifact['content_complete']
    assert result['source_checked_live'] and not result['model_understanding_verified']
    assert not adapter.streams and not list((root/'artifacts').rglob('*.part'))
    assert 'data_base64' not in json.dumps(result)


def test_source_change_blocks_old_resource_before_content_read(context):
    gui,adapter,_=context;ref=listing(gui,kind='image')[0]['resource_ref']
    adapter.rows[0]['resources'][0]['provider_ref']='different-source'
    result=gui.request('resource_read',{'resource_ref':ref})
    assert result['error']['code']=='stale_resource'
    assert not any(call['op']=='resource_open' for call in adapter.calls)


def test_source_change_during_transfer_discards_partial_artifact_and_closes(context):
    gui,adapter,root=context;ref=listing(gui,kind='image')[0]['resource_ref'];adapter.changed=True
    result=gui.request('resource_read',{'resource_ref':ref})
    assert result['error']['code']=='stale_resource' and not adapter.streams
    assert not list((root/'artifacts').rglob('*.part')) and not gui.resources.store.artifacts


@pytest.mark.parametrize('arguments',[{'max_bytes':0},{'max_bytes':True},{'max_seconds':float('nan')},
    {'max_pixels':0},{'representation':'guess'},{'delivery':'assume_vision'},{'guard_refs':['forged']}])
def test_invalid_requests_do_not_fetch_or_capture(context,arguments):
    gui,adapter,_=context;ref=listing(gui)[0]['resource_ref']
    result=gui.request('resource_read',{'resource_ref':ref,**arguments})
    assert not result['ok'] and not any(call['op']=='resource_open' for call in adapter.calls)


def test_byte_limit_and_no_progress_close_streams_without_committing(context):
    gui,adapter,root=context;ref=listing(gui,kind='image')[0]['resource_ref']
    result=gui.request('resource_read',{'resource_ref':ref,'max_bytes':1})
    assert result['error']['code']=='resource_too_large' and not adapter.streams
    adapter.no_progress=True
    result=gui.request('resource_read',{'resource_ref':ref})
    assert result['error']['code']=='resource_unavailable' and not adapter.streams
    assert not gui.resources.store.artifacts and not list((root/'artifacts').rglob('*.part'))


def test_artifact_integrity_range_and_explicit_release(context):
    gui,adapter,_=context;ref=listing(gui,kind='audio')[0]['resource_ref']
    artifact=data(gui.request('resource_read',{'resource_ref':ref}))['artifact']
    assert gui.resources.store.read(artifact['artifact_id'],4,8)[1]==DATA['audio'][4:12]
    listing(gui)  # Re-listing must preserve ownership of materialized artifacts.
    released=data(gui.request('resource_release',{'resource_ref':ref}))
    assert released['artifact_files_retained'] and Path(artifact['path']).exists()
    deleted=data(gui.request('resource_release',{'artifact_id':artifact['artifact_id'],'delete_artifacts':True}))
    assert deleted['artifacts_deleted']==1 and not Path(artifact['path']).exists()


def test_resource_paging_does_not_evict_the_returned_page_before_it_can_be_read(context):
    gui,adapter,_=context;template=adapter.rows[0]
    adapter.rows=[{**copy.deepcopy(template),'ref':'image'+str(index)} for index in range(2050)]
    page=data(gui.request('resources',{'source':'s','limit':2}))
    assert page['matched']==2050 and len(gui.resources.records)==2
    first=page['resources'][0]['resource_ref']
    end=data(gui.request('resources',{'source':'s','offset':2049,'limit':1}))
    assert len(end['resources'])==1 and len(gui.resources.records)==3
    assert data(gui.request('resource_read',{'resource_ref':first}))['artifact']['mime_type']=='image/png'


def test_invalid_listing_and_release_arguments_return_contract_errors(context):
    gui,adapter,_=context
    assert gui.request('resources',{'source':'s','kind':[]})['error']['code']=='invalid_arguments'
    assert gui.request('resource_release',{'resource_ref':[]})['error']['code']=='invalid_arguments'
    assert gui.request('resource_release',{'artifact_id':'foreign'})['error']['code']=='stale_resource'


def test_modified_artifact_cannot_be_replayed_as_verified_bytes(context):
    gui,adapter,_=context;ref=listing(gui,kind='text')[0]['resource_ref']
    artifact=data(gui.request('resource_read',{'resource_ref':ref}))['artifact']
    Path(artifact['path']).write_bytes(b'changed')
    with pytest.raises(Fault,match='contents changed'):gui.resources.store.read(artifact['artifact_id'])


def test_label_is_never_used_as_resource_content_and_native_capture_is_explicit(tmp_path):
    row={'ref':'image','role':'Image','name':'only a label','actions':['get'],'backend':'msaa','rect':{'x':1,'y':2,'width':3,'height':4}}
    calls=[]
    def request(op,args=None):
        calls.append(op)
        if op=='inspect':return {'ok':True,'data':{'elements':[copy.deepcopy(row)],'truncated':False}}
        if op=='observe':return {'ok':True,'data':{'element':copy.deepcopy(row),'fresh':True}}
        raise AssertionError(op)
    captured=[]
    def capture(gui,source,current,budget):captured.append(current['ref']);return PNG,{'representation':'rendered_region','visible_only':True}
    with GuiSession(desktop_factory=lambda:SimpleNamespace(request=request,close=lambda:None),resource_capture=capture,
            discovery_cache=tmp_path/'cache.json',learning_cache=tmp_path/'learning.json',resource_root=tmp_path/'artifacts') as gui:
        gui.sources['s']={'id':'s','runtime':'native','target':{'window':10},'adapter':None,'snapshot':None,'bootstrap':None,'lease':None}
        ref=listing(gui)[0]['resource_ref']
        result=gui.request('resource_read',{'resource_ref':ref})
        assert result['error']['code']=='resource_source_unavailable' and not captured
        artifact=data(gui.request('resource_read',{'resource_ref':ref,'representation':'rendered'}))['artifact']
        assert captured and artifact['representation']=='rendered_region' and not artifact['original_quality_verified']
        assert Path(artifact['path']).read_bytes()==PNG and 'get' not in calls


def test_text_delivery_uses_resource_bytes_and_other_kinds_require_decoder(context):
    gui,adapter,_=context
    text=listing(gui,kind='text')[0]['resource_ref']
    result=data(gui.request('resource_read',{'resource_ref':text,'delivery':'text'}))
    assert result['text']['value']==DATA['text'].decode() and not result['text']['label_only']
    pdf=listing(gui,kind='document')[0]['resource_ref']
    result=data(gui.request('resource_read',{'resource_ref':pdf,'delivery':'text'}))
    assert 'text' not in result and result['feedback']['reason']=='text_extraction_not_provided'


def test_source_detach_invalidates_resources_but_does_not_delete_requested_artifacts(context):
    gui,adapter,_=context;ref=listing(gui,kind='image')[0]['resource_ref']
    artifact=data(gui.request('resource_read',{'resource_ref':ref}))['artifact']
    data(gui.request('detach',{'source':'s'}))
    assert gui.request('resource_read',{'resource_ref':ref})['error']['code']=='stale_resource'
    assert Path(artifact['path']).exists()


def test_mcp_native_content_is_modality_aware_and_retains_reference(context):
    from resource_feedback import mcp_resource_result
    gui,adapter,_=context
    for kind,block in [('image','image'),('audio','audio'),('video','resource_link'),('document','resource_link')]:
        ref=listing(gui,kind=kind)[0]['resource_ref']
        result=gui.request('resource_read',{'resource_ref':ref,'delivery':'native'})
        content=mcp_resource_result(gui.resources,result)
        assert block in [item.type for item in content.content]
        assert 'resource_link' in [item.type for item in content.content]
        assert not content.isError and content.structuredContent['ok']
