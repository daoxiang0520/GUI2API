"""Owned Windows fixture: compare cache performance without mouse/keyboard input."""
import ctypes
import json
import os
from pathlib import Path
import statistics
import sys
import time
import traceback

ROOT=Path(os.environ.get('GUI2API_BENCH_ROOT',Path(__file__).resolve().parents[1])).resolve()
sys.path.insert(0,str(ROOT))
from gui_api import GuiSession
from probe_universality import launch_owned,close_owned
from verify_discovery import build_fixture,data,wait_state


def main(output, samples=12, backend='uia3'):
    output.mkdir(parents=True,exist_ok=True)
    owned=launch_owned([build_fixture('Build-CacheFixture.ps1'),output/'state.json'])
    report={'ok':False,'model_calls':0,'screenshots':0,'production_app_writes':0,'mouse_keyboard_input':0,'samples':samples,
            'backend':backend,'scope':'Owned WinForms window. End-to-end framework time, excluding model and DSH scheduling.',
            'timings':{},'foreground_changes':[]}
    user=ctypes.WinDLL('user32');user.GetForegroundWindow.restype=ctypes.c_void_p
    foreground=lambda:int(user.GetForegroundWindow() or 0)
    try:
        initial=wait_state(output/'state.json',lambda r:r['pid']==owned.pid,process=owned)
        with GuiSession(discovery_cache=output/'metadata.json',learning_cache=output/'learning.json') as gui:
            counts={'full_scans':0,'bound_reads':0,'provider_calls':0}
            desktop=gui.desktop
            request=desktop.request
            def observed(op,args=None,*extra,**kw):
                counts['provider_calls']+=1
                if op=='inspect':counts['full_scans']+=1
                if op=='observe':counts['bound_reads']+=1
                return request(op,args,*extra,**kw)
            desktop.request=observed
            started=time.perf_counter()
            source=data(gui.request('attach',{'window':initial['window'],'runtime':'native','backend':backend}))['source']
            catalog=data(gui.request('discover',{'source':source,'limit':3000}))
            report['attach_and_initial_discovery_ms']=round((time.perf_counter()-started)*1000,3)
            entry=data(gui.request('find',{'source':source,'role':'Edit'}))['elements'][0]
            increment=next(c for c in catalog['controls'] if c['name']=='Increment' and c['role']=='Button')
            def measure(label, operation):
                timings=[];before=dict(counts)
                for i in range(samples):
                    front=foreground();begin=time.perf_counter()
                    result=operation(i)
                    timings.append(round((time.perf_counter()-begin)*1000,3))
                    if foreground()!=front:report['foreground_changes'].append({'operation':label,'sample':i})
                    if not result['ok']:
                        reference=gui.refs.get(entry['ref'])
                        report['failure_context']={'operation':label,'sample':i,'reference_present':reference is not None,
                            'reference_age_ms':round((time.monotonic()-reference['seen'])*1000,3) if reference else None,
                            'reference_count':len(gui.refs),'nodes_in_snapshot':len(gui.sources[source].get('collection',{}).get('rows',{}))}
                    assert result['ok'],result
                report['timings'][label]={'median_ms':round(statistics.median(timings),3),'min_ms':min(timings),
                    'max_ms':max(timings),'samples_ms':timings,'counts':{k:counts[k]-before[k] for k in counts}}
            measure('cached_find',lambda i:gui.request('find',{'source':source,'role':'Edit'}))
            measure('rebind',lambda i:gui.request('rebind',{'source':source,'node_id':entry['fingerprint'],'method':'set'}))
            # Rebinding can legitimately return a new provider reference. Start
            # the next workload with the latest observed entry, not the old ref.
            entry=data(gui.request('find',{'source':source,'role':'Edit'}))['elements'][0]
            measure('set_with_value_postcondition',lambda i:gui.request('call',{'ref':entry['ref'],'method':'set',
                'arguments':{'value':f'owned-value-{i}'},'postcondition':{'ref':entry['ref'],'field':'value','equals':f'owned-value-{i}','timeout_ms':1000}}))
            current=data(gui.request('discover',{'source':source,'limit':3000}))
            increment=next(c for c in current['controls'] if c['name']=='Increment' and c['role']=='Button')
            measure('catalog_invoke',lambda i:gui.request('discovery_invoke',{'capability':increment['id'],'method':'invoke',
                'arguments':{'allow_unreliable':True}}))
            state=wait_state(output/'state.json',lambda r:r['count']==samples and r['text']==f'owned-value-{samples-1}',process=owned)
            assert state['writes']==samples,state
            report.update(ok=True,controls=catalog['control_count'],operation_count=catalog['operation_count'],counts=counts,
                fixture_result={'count':state['count'],'value_writes':state['writes'],'expected_count':samples,'exactly_once':True})
    except Exception as exc:
        report['error']={'type':type(exc).__name__,'message':str(exc),'traceback':traceback.format_exc()}
    finally:
        report['cleanup']=close_owned(owned)
        (output/'benchmark.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps({**report,'timings':{k:{f:v for f,v in row.items() if f!='samples_ms'} for k,row in report['timings'].items()}},ensure_ascii=False))
    return 0 if report['ok'] and report['cleanup']['closed'] else 1


if __name__=='__main__':raise SystemExit(main(Path(sys.argv[1]).resolve(),int(sys.argv[2]) if len(sys.argv)>2 else 12,sys.argv[3] if len(sys.argv)>3 else 'uia3'))
