"""Owned real DOM resources and MCP image/audio/file delivery, without models."""
import asyncio
import base64
from functools import partial
import hashlib
from http.server import SimpleHTTPRequestHandler,ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import shutil
import sys
import threading
import time
import traceback
import wave

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from gui_api import GuiSession
from resource_capture import png_rgb
from probe_universality import launch_owned,close_owned
from cdp_backend import targets
from verify_discovery import data


def browser_path():
    candidates=[shutil.which('chrome.exe'),shutil.which('msedge.exe')]
    for variable in ('ProgramFiles','ProgramFiles(x86)','LOCALAPPDATA'):
        if os.environ.get(variable):candidates.extend(str(Path(os.environ[variable])/relative) for relative in
            ('Google/Chrome/Application/chrome.exe','Microsoft/Edge/Application/msedge.exe'))
    return next((path for path in candidates if path and Path(path).is_file()),None)


def pdf_fixture():
    """A complete one-page PDF, including object offsets and xref table."""
    text=b'BT /F1 12 Tf 36 100 Td (Owned GUI2API resource) Tj ET'
    objects=[b'<< /Type /Catalog /Pages 2 0 R >>',
        b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
        b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 240 160] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>',
        b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
        b'<< /Length '+str(len(text)).encode()+b' >>\nstream\n'+text+b'\nendstream']
    pdf=bytearray(b'%PDF-1.4\n');offsets=[]
    for index,obj in enumerate(objects,1):
        offsets.append(len(pdf));pdf.extend(str(index).encode()+b' 0 obj\n'+obj+b'\nendobj\n')
    xref=len(pdf);pdf.extend(b'xref\n0 6\n0000000000 65535 f \n')
    for offset in offsets:pdf.extend(f'{offset:010d} 00000 n \n'.encode())
    pdf.extend(b'trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n'+str(xref).encode()+b'\n%%EOF\n')
    return bytes(pdf)


async def mcp_case(output,endpoint,page_id):
    from mcp import ClientSession,StdioServerParameters
    from mcp.client.stdio import stdio_client
    params=StdioServerParameters(command=sys.executable,args=['-X','utf8',str(ROOT/'appctl_mcp.py')],cwd=str(ROOT),
        env={**os.environ,'GUI2API_RESOURCE_ROOT':str(output/'mcp-artifacts'),'GUI2API_DISCOVERY_CACHE':str(output/'mcp-cache.json'),
             'PYTHONUTF8':'1','PYTHONIOENCODING':'utf-8'})
    with (output/'mcp-stderr.log').open('w',encoding='utf-8') as errors:
        async with stdio_client(params,errlog=errors) as (read,write):
            async with ClientSession(read,write) as client:
                await client.initialize()
                tools=await client.list_tools();assert len(tools.tools)==48
                async def call(name,args):
                    result=await client.call_tool(name,args)
                    value=result.structuredContent or json.loads(result.content[0].text)
                    assert value['ok'],value
                    return result,value['data']
                _,bound=await call('gui_attach',{'runtime':'cdp-dom','endpoint':endpoint,'target':page_id})
                source=bound['source'];observed={}
                for element,expected in [('photo','image'),('voice','audio'),('clip','resource_link'),('document','resource_link')]:
                    _,node=await call('gui_find',{'source':source,'automation_id':element})
                    _,found=await call('gui_resources',{'ref':node['elements'][0]['ref']})
                    result,resource=await call('gui_resource_read',{'resource_ref':found['resources'][0]['resource_ref'],'delivery':'native'})
                    kinds=[block.type for block in result.content]
                    assert expected in kinds and 'resource_link' in kinds,(element,kinds)
                    artifact=resource['artifact'];record=await client.read_resource(artifact['uri'])
                    decoded=base64.b64decode(record.contents[0].blob)
                    assert hashlib.sha256(decoded).hexdigest()==artifact['sha256']
                    part=await client.read_resource(artifact['uri']+'/bytes/0/8')
                    assert base64.b64decode(part.contents[0].blob)==decoded[:8]
                    observed[element]={'native_blocks':kinds,'mime_type':artifact['mime_type'],'bytes':artifact['bytes'],'hash_verified':True}
                return {'ok':True,'tools':len(tools.tools),'modalities':observed,'artifact_uris_readable':True,'byte_ranges_verified':True}


def main(output):
    output.mkdir(parents=True,exist_ok=True);web=output/'web';web.mkdir()
    assets={'tile.png':png_rgb(64,48,bytes([35,110,230])*64*48),
        'second.png':png_rgb(64,48,bytes([220,70,40])*64*48),
        'document.pdf':pdf_fixture(),'clip.webm':(ROOT/'tests/fixtures/resources.webm').read_bytes(),
        'payload.bin':bytes(range(256))*4096,'note.txt':'Owned resource text 文本\n'.encode('utf-8')}
    sound=io.BytesIO()
    with wave.open(sound,'wb') as wav:wav.setparams((1,2,8000,0,'NONE','not compressed'));wav.writeframes(b'\0\0'*800)
    assets['voice.wav']=sound.getvalue()
    for name,value in assets.items():(web/name).write_bytes(value)
    (web/'index.html').write_bytes((ROOT/'tests/fixtures/resources.html').read_bytes())
    counts={}
    class Handler(SimpleHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_GET(self):
            counts[self.path]=counts.get(self.path,0)+1
            if self.path=='/redirect':self.send_response(302);self.send_header('Location','/payload.bin');self.end_headers();return
            super().do_GET()
    server=ThreadingHTTPServer(('127.0.0.1',0),partial(Handler,directory=str(web)))
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    browser=browser_path();assert browser,'An installed Chromium is required'
    profile=output/'profile';owned=launch_owned([browser,'--headless=new','--remote-debugging-address=127.0.0.1','--remote-debugging-port=0',
        '--user-data-dir='+str(profile),'--no-first-run','--disable-extensions','--disable-background-networking',
        '--disable-background-timer-throttling','--disable-renderer-backgrounding','--autoplay-policy=no-user-gesture-required',
        'http://127.0.0.1:'+str(server.server_port)+'/index.html'])
    report={'ok':False,'model_calls':0,'screenshots_for_locating':0,'production_app_writes':0,'profiles_modified':False,'cases':{}}
    try:
        port=profile/'DevToolsActivePort';deadline=time.monotonic()+15
        while not port.exists() and time.monotonic()<deadline and owned.poll() is None:time.sleep(.05)
        endpoint='http://127.0.0.1:'+port.read_text().splitlines()[0]
        page=None;deadline=time.monotonic()+15
        while time.monotonic()<deadline:
            matches=[p for p in targets(endpoint) if p.get('title')=='GUI2API resource fixture']
            if len(matches)==1:page=matches[0];break
            time.sleep(.05)
        assert page,'Owned page did not load'
        with GuiSession(resource_root=output/'artifacts',discovery_cache=output/'cache.json',learning_cache=output/'learning.json') as gui:
            source=data(gui.request('attach',{'runtime':'cdp-dom','endpoint':endpoint,'target':page['id']}))['source']
            adapter=gui.sources[source]['adapter'];conn,_=adapter.backend._connection(adapter.context)
            deadline=time.monotonic()+10
            while time.monotonic()<deadline:
                fixture=conn.rpc('Runtime.evaluate',{'expression':'window.fixture','returnByValue':True})['result'].get('value',{})
                if fixture.get('ready') or fixture.get('error'):break
                time.sleep(.05)
            assert fixture.get('ready'),fixture
            assert fixture.get('videoDecoded'),fixture
            report['browser_decoded_video']={key:fixture[key] for key in ('videoWidth','videoHeight','videoDecoded')}
            before=dict(counts);found=data(gui.request('resources',{'source':source}))
            assert {r['kind'] for r in found['resources']}=={'image','audio','video','document','file','text'}
            assert all(counts.get('/'+name,0)==before.get('/'+name,0)==0 for name in ('document.pdf','payload.bin','note.txt'))
            assert 'data_base64' not in json.dumps(found) and 'provider_ref' not in json.dumps(found)
            report['metadata_only_discovery']=True
            def resource(element,kind=None):
                node=data(gui.request('find',{'source':source,'automation_id':element}))['elements'][0]
                entries=data(gui.request('resources',{'ref':node['ref']}))['resources']
                return next(r for r in entries if kind is None or r['kind']==kind)
            pairs=[('photo','tile.png'),('voice','voice.wav'),('document','document.pdf'),('binary','payload.bin'),('text','note.txt'),('selected',None),('clip','clip.webm')]
            for element,asset in pairs:
                item=resource(element);result=data(gui.request('resource_read',{'resource_ref':item['resource_ref']}))
                artifact=result['artifact'];payload=Path(artifact['path']).read_bytes()
                if asset:assert payload==assets[asset],element
                elif element=='selected':assert payload==bytes([11,22,33,44,55])
                report['cases'][element]={'kind':artifact['kind'],'mime_type':artifact['mime_type'],'bytes':len(payload),'source_bytes_verified':True}
            photo=resource('photo');preview=data(gui.request('resource_read',{'resource_ref':photo['resource_ref'],'representation':'rendered'}))['artifact']
            assert preview['representation']=='rendered_region' and not preview['original_quality_verified']
            assert preview['format_verification']['width']>0 and Path(preview['path']).read_bytes().startswith(b'\x89PNG')
            report['rendered_resource']={'artifact':preview,'explicit':True}
            canvas=resource('surface');preview_canvas=data(gui.request('resource_read',{'resource_ref':canvas['resource_ref'],'representation':'rendered'}))['artifact']
            assert preview_canvas['mime_type']=='image/png'
            live=resource('live');unavailable=gui.request('resource_read',{'resource_ref':live['resource_ref']})
            assert unavailable['error']['code']=='stream_unavailable';report['live_stream_not_recorded']=True
            redirect=resource('redirect');blocked=gui.request('resource_read',{'resource_ref':redirect['resource_ref']})
            assert blocked['error']['code']=='resource_unavailable';report['redirect_not_followed']=True
            mutable=resource('mutable')
            conn.rpc('Runtime.evaluate',{'expression':"document.getElementById('mutable').src='/second.png'"})
            stale=gui.request('resource_read',{'resource_ref':mutable['resource_ref']})
            assert stale['error']['code']=='stale_resource';report['changed_resource_rejected']=True
            report['mcp']=asyncio.run(asyncio.wait_for(mcp_case(output,endpoint,page['id']),60))
            report['ok']=True
    except Exception as exc:report['error']={'type':type(exc).__name__,'message':str(exc),'traceback':traceback.format_exc()}
    finally:
        report['cleanup']=close_owned(owned);server.shutdown();server.server_close();thread.join(2)
        (output/'resources.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(report,ensure_ascii=True))
    return 0 if report['ok'] and report['cleanup']['closed'] else 1


if __name__=='__main__':raise SystemExit(main(Path(sys.argv[1]).resolve()))
