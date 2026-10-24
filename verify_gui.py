"""Cross-app verification of the generic facade; owned fixtures for click effects."""
import argparse
import asyncio
from collections import Counter
import hashlib
import json
from pathlib import Path
import sys
import time
import traceback
import uuid

from gui_api import GuiSession

ROOT = Path(__file__).resolve().parent
REPORT = ROOT / 'reports/gui-surface-20261024'


def require(result):
    if not result.get('ok'): raise AssertionError(result.get('error'))
    return result['data']


def save(name, result):
    REPORT.mkdir(parents=True,exist_ok=True)
    (REPORT/(name+'.json')).write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'case':name,**result},ensure_ascii=False),flush=True)


def collect(gui, source, limit=100, include_hidden=False):
    rows, cursor, pages = [], None, 0
    while True:
        data = require(gui.request('scan', {'source':source,'limit':limit,'cursor':cursor,'include_hidden':include_hidden}))
        rows.extend(data['elements']); pages+=1
        cursor = data['cursor']
        if not cursor: return rows, {k:v for k,v in data.items() if k != 'elements'}, pages


def browser_case(app):
    from cdp_backend import targets
    from probe_universality import application_path, launch_owned, close_owned
    executable = application_path('chrome.exe' if app=='chrome' else 'msedge.exe')
    run = ROOT/'tests/runs'/('gui-'+app+'-'+uuid.uuid4().hex[:8])
    run.mkdir(parents=True)
    profile = run/'profile'
    fixture = ROOT/'tests/fixtures/gui_surface.html'
    owned = launch_owned([executable,'--headless=new','--remote-debugging-address=127.0.0.1',
        '--remote-debugging-port=0','--user-data-dir='+str(profile),'--no-first-run','--disable-extensions',
        '--disable-background-networking','--disable-component-update',fixture.as_uri()])
    report = {'screenshots':0,'feature_profiles':0,'scope':'owned local browser fixture, separate profile','ok':False}
    try:
        port_file = profile/'DevToolsActivePort'
        deadline = time.monotonic()+15
        while not port_file.exists() and time.monotonic()<deadline and owned.poll() is None: time.sleep(.1)
        endpoint = 'http://127.0.0.1:'+port_file.read_text().splitlines()[0]
        page = next(p for p in targets(endpoint) if p.get('url')==fixture.as_uri())
        with GuiSession() as gui:
            source = require(gui.request('attach',{'runtime':'cdp-dom','endpoint':endpoint,'target':page['id']}))['source']
            rows, data, pages = collect(gui,source,limit=7)
            assert len({r['ref'] for r in rows})==len(rows) and pages>1 and not data['truncated']
            by_id = {r['automation_id']:r for r in rows if r.get('automation_id')}
            def call(key,method,**params):
                return require(gui.request('call',{'ref':by_id[key]['ref'],'method':method,'arguments':params}))
            schemas = require(gui.request('describe',{'ref':by_id['body']['ref']}))
            assert 'set' in schemas['methods']
            marker='Generic API \" literal ${notCode} 中文\nsecond line'
            assert call('body','set',value=marker,expected='')['status']=='verified'
            assert call('body','get')['value']==marker
            assert call('submit','invoke')['status']=='executed_unverified'
            # A formerly empty output may acquire layout only after the action.
            updated, _, _ = collect(gui,source,limit=7)
            by_id.update({r['automation_id']:r for r in updated if r.get('automation_id')})
            assert call('echo','get')['value']==marker and call('count','get')['value']=='1'
            conflict = gui.request('call',{'ref':by_id['body']['ref'],'method':'set','arguments':{'value':'bad','expected':''}})
            assert conflict['error']['code']=='value_conflict' and call('body','get')['value']==marker
            assert call('check','toggle-on')['state'] and not call('check','toggle-off')['state']
            assert call('radio','select')['selected']
            assert call('choice','set',value='b')['status']=='verified'
            assert 'set' not in by_id['editorParagraph']['methods']
            assert call('editor','set',value='Editable host',expected='Existing')['status']=='verified'
            assert call('details','expand')['expanded'] and not call('details','collapse')['expanded']
            assert call('scroll','scroll',direction='down',amount=100)['status']=='verified'
            assert call('delegated','invoke')['status']=='executed_unverified' and call('delegatedCount','get')['value']=='1'
            assert call('shadowInput','set',value='shadow')['status']=='verified'
            assert call('frameInput','set',value='frame')['status']=='verified'
            assert not by_id['password']['methods']
            assert gui.request('call',{'ref':by_id['password']['ref'],'method':'get'})['error']['code']=='protected_control'
            assert gui.request('call',{'ref':by_id['disabled']['ref'],'method':'set','arguments':{'value':'x'}})['error']['code']=='disabled_control'
            assert 'hidden' not in by_id
            assert any(g['kind']=='pixel_region' for g in data['gaps'])
            call('remove','invoke')
            assert gui.request('call',{'ref':by_id['removable']['ref'],'method':'get'})['error']['code']=='stale_ref'
            hidden_rows, _, _ = collect(gui,source,include_hidden=True)
            assert any(r['automation_id']=='hidden' for r in hidden_rows)
            truncated = require(gui.request('scan',{'source':source,'max_nodes':3}))
            assert truncated['truncated']
            adapter = gui.sources[source]['adapter']
            conn,_ = adapter.backend._connection(adapter.context)
            conn.rpc('Page.navigate',{'url':fixture.as_uri()+'?navigation-test'})
            time.sleep(.3)
            result=gui.request('call',{'ref':by_id['body']['ref'],'method':'get'})
            assert result['error']['code']=='stale_ref',result
            require(gui.request('scan',{'source':source}))
            assert gui.request('call',{'ref':by_id['body']['ref'],'method':'get'})['error']['code']=='stale_ref'
            report.update(ok=True,nodes=len(rows),pages=pages,methods=sorted({m for r in rows for m in r['methods']}),
                checks=['text + input-event state + submit echo','literal arguments','compare-and-set conflict',
                    'checkbox','radio select','select value','editable host and inherited paragraph','details','scroll','delegated div click','open shadow','same-origin iframe',
                    'disabled/password','hidden opt-in','pixel gap','detached ref','navigation ref','bounded scan'],
                cleanup=require(gui.request('detach',{'source':source}))['cleanup'])
    except Exception as exc:
        report.update(error_type=type(exc).__name__,error=str(exc),traceback=traceback.format_exc())
    finally:
        report['process_cleanup']=close_owned(owned)
    save(app,report)
    return report


def live_cases():
    reports=[]
    with GuiSession() as gui:
        windows=require(gui.request('apps'))['windows']
        cases=[('qq','electron-dom','QQNT.dll'),('weixin','wechat-qt',None),('explorer','native',None),
               ('cloudmusic','native',None),('code','native',None),('wsrx-desktop','native',None),('ida','native',None)]
        for app,runtime,module in cases:
            matches=[w for w in windows if w['app'].casefold()==app]
            if not matches: continue
            selected=matches[0]
            report={'app':app,'runtime':runtime,'screenshots':0,'messages_sent':0,'feature_profiles':0,'ok':False,
                    'scope':'live current window; metadata enumeration; reversible empty-field edits on QQ/WeChat only'}
            source=None
            started=time.perf_counter()
            try:
                source=require(gui.request('attach',{'runtime':runtime,'window':selected['window'],'api_module':module}))['source']
                rows,data,pages=collect(gui,source,limit=50)
                report.update(nodes=len(rows),pages=pages,roles=dict(Counter(r['role'] for r in rows)),
                    method_counts=dict(Counter(m for r in rows for m in r['methods'])),truncated=data.get('truncated'),
                    gaps=data.get('gaps',[]),collection_ms=round((time.perf_counter()-started)*1000,2))
                # Reports contain no personal chat labels or field contents.
                if app in ('qq','weixin'):
                    candidates=[r for r in rows if 'set' in r['methods'] and
                        (r.get('tag') in ('input','textarea') or r.get('editable') or r['role']=='Edit')]
                    edits=[]
                    report['reversible_field_checks']=edits
                    for row in candidates[:4]:
                        before=require(gui.request('call',{'ref':row['ref'],'method':'get'}))
                        if before.get('value')!='' or before.get('label_only') or before.get('truncated'): continue
                        marker='generic-gui-check-'+uuid.uuid4().hex[:8]
                        restored=False
                        try:
                            written=require(gui.request('call',{'ref':row['ref'],'method':'set','arguments':{'value':marker,'expected':''}}))
                            observed=require(gui.request('call',{'ref':row['ref'],'method':'get'}))
                            assert observed['value']==marker
                            edits.append({'role':row['role'],'status':written.get('status'),'source':observed.get('source')})
                        finally:
                            current=require(gui.request('call',{'ref':row['ref'],'method':'get'}))
                            if current.get('value')==marker:
                                require(gui.request('call',{'ref':row['ref'],'method':'set','arguments':{'value':'','expected':marker}}))
                            restored=require(gui.request('call',{'ref':row['ref'],'method':'get'})).get('value')==''
                            assert restored,'Empty-field restoration failed'
                        edits[-1]['restored']=restored
                report['ok']=bool(rows)
            except Exception as exc: report.update(error_type=type(exc).__name__,error=str(exc))
            finally:
                if source:
                    detached=gui.request('detach',{'source':source})
                    report['detach']=detached
                    if not detached.get('ok'):report['ok']=False
            save(app,report)
            reports.append(report)
    return reports


def native_case():
    import subprocess
    from probe_universality import launch_owned,close_owned
    report={'ok':False,'screenshots':0,'feature_profiles':0,'scope':'owned WinForms app with standard controls and MSAA custom canvas'}
    executable=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-File',
        str(ROOT/'tests/fixtures/Build-NativeFixture.ps1')],capture_output=True,text=True,encoding='utf-8',check=True).stdout.strip().splitlines()[-1]
    run=ROOT/'tests/runs'/('gui-native-'+uuid.uuid4().hex[:8]);run.mkdir(parents=True)
    state=run/'state.json'
    owned=launch_owned([executable,str(state)])
    try:
        deadline=time.monotonic()+10
        while not state.exists() and time.monotonic()<deadline and owned.poll() is None:time.sleep(.1)
        def actual():return json.loads(state.read_text(encoding='utf-8-sig'))
        with GuiSession() as gui:
            source=require(gui.request('attach',{'window':actual()['window']}))['source']
            rows,data,pages=collect(gui,source,limit=20)
            def pick(label,method):
                matches=[r for r in rows if r['name']==label and method in r['methods']]
                assert matches,label
                # Source choice is explicit; the facade never retries on a different channel.
                return sorted(matches,key=lambda r:('uia3','msaa','win32').index(r['backend']))[0]['ref']
            def call(label,method,**params):return require(gui.request('call',{'ref':pick(label,method),'method':method,'arguments':params}))
            call('标准输入框','set',value='generic native text',expected='')
            assert actual()['standard_text']=='generic native text'
            call('计数增加','invoke')
            assert actual()['count']==1
            call('自绘输入槽','set',value='custom canvas text',expected='')
            assert actual()['custom_text']=='custom canvas text'
            call('自绘计数增加','invoke')
            assert actual()['count']==2
            call('自绘复选框','toggle-on')
            assert actual()['custom_checked']
            report.update(ok=True,nodes=len(rows),pages=pages,truncated=data.get('truncated'),
                verified=['standard text state','standard button event state','MSAA custom text state','MSAA custom button event state','MSAA custom toggle state'],
                cleanup=require(gui.request('detach',{'source':source}))['cleanup'])
    except Exception as exc:report.update(error=str(exc),traceback=traceback.format_exc())
    finally:report['process_cleanup']=close_owned(owned)
    save('native-fixture',report)
    return report


async def mcp_case():
    from mcp import ClientSession,StdioServerParameters
    from mcp.client.stdio import stdio_client
    params=StdioServerParameters(command=sys.executable,args=[str(ROOT/'appctl_mcp.py')],cwd=str(ROOT))
    async with stdio_client(params) as (read,write):
        async with ClientSession(read,write) as client:
            await client.initialize()
            names={t.name for t in (await client.list_tools()).tools}
            assert {'gui_schema','gui_attach','gui_scan','gui_describe','gui_invoke','gui_detach'}<=names
            async def invoke(name,args):
                result=await client.call_tool(name,args)
                return require(result.structuredContent or json.loads(result.content[0].text))
            schema=await invoke('gui_schema',{})
            assert not schema['app_feature_registration_required']
            windows=(await invoke('desktop_apps',{}))['windows']
            window=next(w for w in windows if w['app'].casefold()=='explorer')
            source=(await invoke('gui_attach',{'window':window['window']}))['source']
            rows=(await invoke('gui_scan',{'source':source,'limit':10}))['elements']
            assert rows
            described=await invoke('gui_describe',{'ref':rows[0]['ref']})
            assert 'get' in described['methods']
            await invoke('gui_invoke',{'ref':rows[0]['ref'],'method':'get'})
            await invoke('gui_detach',{'source':source})
            report={'ok':True,'tool_count':len(names),'actual_calls':['gui_schema','gui_attach','gui_scan','gui_describe','gui_invoke(get)','gui_detach']}
            save('mcp',report)
            return report


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--case',choices=['browsers','native','live','mcp','all'],default='all')
    args=parser.parse_args()
    results=[]
    if args.case in ('browsers','all'):results.extend(browser_case(app) for app in ('chrome','edge'))
    if args.case in ('native','all'):results.append(native_case())
    if args.case in ('live','all'):results.extend(live_cases())
    if args.case in ('mcp','all'):results.append(asyncio.run(mcp_case()))
    summary={'ok':all(r.get('ok') for r in results),'screenshots':0,'results':results,
             'engine_sha256':hashlib.sha256((ROOT/'bridges/gui_dom.js').read_bytes()).hexdigest()}
    save('summary-'+args.case,summary)
    return 0 if summary['ok'] else 1


if __name__=='__main__':raise SystemExit(main())
