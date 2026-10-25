"""Owned headless browser timing; no interaction with the user's browser."""
import json
import os
from pathlib import Path
import statistics
import shutil
import sys
import time
import traceback

ROOT=Path(os.environ.get('GUI2API_BENCH_ROOT',Path(__file__).resolve().parents[1])).resolve()
sys.path.insert(0,str(ROOT))
from gui_api import GuiSession
from probe_universality import launch_owned,close_owned
from cdp_backend import targets
from verify_discovery import data


def main(output,samples=12):
    output.mkdir(parents=True,exist_ok=True)
    candidates=[shutil.which('chrome.exe'),shutil.which('msedge.exe')]
    for variable in ('ProgramFiles','ProgramFiles(x86)','LOCALAPPDATA'):
        base=os.environ.get(variable)
        if base:
            candidates.extend(str(Path(base)/relative) for relative in (
                'Google/Chrome/Application/chrome.exe','Microsoft/Edge/Application/msedge.exe'))
    executable=next((p for p in candidates if p and Path(p).is_file()),None)
    assert executable,'An installed Chromium is required'
    profile=output/'profile'
    fixture=Path(__file__).resolve().parent/'fixtures/cache_surface.html'
    owned=launch_owned([executable,'--headless=new','--remote-debugging-address=127.0.0.1','--remote-debugging-port=0',
        '--user-data-dir='+str(profile),'--no-first-run','--disable-extensions','--disable-background-networking',fixture.as_uri()])
    report={'ok':False,'backend':'cdp-dom','samples':samples,'model_calls':0,'screenshots':0,'production_app_writes':0,'mouse_keyboard_input':0,
        'scope':'Owned local HTML page and isolated headless profile; excludes model and DSH scheduling.','timings':{}}
    try:
        port=profile/'DevToolsActivePort';deadline=time.monotonic()+15
        while not port.exists() and time.monotonic()<deadline and owned.poll() is None:time.sleep(.05)
        endpoint='http://127.0.0.1:'+port.read_text().splitlines()[0]
        deadline=time.monotonic()+12;page=None
        while time.monotonic()<deadline and owned.poll() is None:
            matches=[p for p in targets(endpoint) if p.get('title')=='GUI2API owned cache fixture']
            if len(matches)==1:page=matches[0];break
            time.sleep(.05)
        assert page,'Owned page did not load'
        with GuiSession(discovery_cache=output/'metadata.json',learning_cache=output/'learning.json') as gui:
            started=time.perf_counter()
            source=data(gui.request('attach',{'runtime':'cdp-dom','endpoint':endpoint,'target':page['id']}))['source']
            adapter=gui.sources[source]['adapter'];request=adapter.request
            counts={'full_scans':0,'bound_reads':0,'provider_calls':0}
            def observed(args):
                counts['provider_calls']+=1
                if args['op']=='scan':counts['full_scans']+=1
                if args['op']=='observe':counts['bound_reads']+=1
                return request(args)
            adapter.request=observed
            catalog=data(gui.request('discover',{'source':source,'limit':3000}))
            report['attach_and_initial_discovery_ms']=round((time.perf_counter()-started)*1000,3)
            entry=data(gui.request('find',{'source':source,'automation_id':'payload'}))['elements'][0]
            increment=next(c for c in catalog['controls'] if c.get('automation_id')=='increment')
            def measure(label,operation):
                times=[];before=dict(counts)
                for i in range(samples):
                    begin=time.perf_counter();result=operation(i);times.append(round((time.perf_counter()-begin)*1000,3))
                    assert result['ok'],result
                report['timings'][label]={'median_ms':round(statistics.median(times),3),'min_ms':min(times),'max_ms':max(times),
                    'samples_ms':times,'counts':{k:counts[k]-before[k] for k in counts}}
            measure('cached_find',lambda i:gui.request('find',{'source':source,'automation_id':'payload'}))
            measure('rebind',lambda i:gui.request('rebind',{'source':source,'node_id':entry['fingerprint'],'method':'set'}))
            measure('set_with_value_postcondition',lambda i:gui.request('call',{'ref':entry['ref'],'method':'set','arguments':{'value':f'owned-{i}'},
                'postcondition':{'ref':entry['ref'],'field':'value','equals':f'owned-{i}','timeout_ms':1000}}))
            catalog=data(gui.request('discover',{'source':source,'limit':3000}))
            increment=next(c for c in catalog['controls'] if c.get('automation_id')=='increment')
            measure('catalog_invoke',lambda i:gui.request('discovery_invoke',{'capability':increment['id'],'method':'invoke'}))
            def value(automation_id):
                node=data(gui.request('find',{'source':source,'automation_id':automation_id,'refresh':True}))['elements'][0]
                return data(gui.request('call',{'ref':node['ref'],'method':'get'}))['value']
            assert value('count')==str(samples) and value('writes')==str(samples)
            assert value('payload')==f'owned-{samples-1}'
            # Removal must reject the old operation before another input occurs.
            catalog=data(gui.request('discover',{'source':source,'limit':3000}))
            removed=next(c for c in catalog['controls'] if c.get('automation_id')=='removable')
            remover=next(c for c in catalog['controls'] if c.get('automation_id')=='remove')
            data(gui.request('discovery_invoke',{'capability':remover['id'],'method':'invoke'}))
            blocked=gui.request('discovery_invoke',{'capability':removed['id'],'method':'set','arguments':{'value':'MUST_NOT_SET'}})
            assert not blocked['ok'] and blocked['error']['code']=='stale_capability',blocked
            report.update(ok=True,controls=catalog['control_count'],counts=counts,removed_binding_rejected=True,
                fixture_result={'count':samples,'value_writes':samples,'exactly_once':True})
    except Exception as exc:
        report['error']={'type':type(exc).__name__,'message':str(exc),'traceback':traceback.format_exc()}
    finally:
        report['cleanup']=close_owned(owned)
        (output/'benchmark.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps({**report,'timings':{k:{f:v for f,v in row.items() if f!='samples_ms'} for k,row in report['timings'].items()}},ensure_ascii=False))
    return 0 if report['ok'] and report['cleanup']['closed'] else 1


if __name__=='__main__':raise SystemExit(main(Path(sys.argv[1]).resolve(),int(sys.argv[2]) if len(sys.argv)>2 else 12))
