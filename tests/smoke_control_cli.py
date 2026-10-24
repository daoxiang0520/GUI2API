"""Actual CLI control operations on an owned Qt window, no screenshots."""
import json
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
import traceback
import uuid

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from probe_universality import launch_owned,close_owned

run=ROOT/'tests/runs'/('control-cli-'+uuid.uuid4().hex[:8]);run.mkdir(parents=True)
state=run/'state.json'
owned=launch_owned([sys.executable,'-X','utf8',str(ROOT/'tests/fixtures/qt_gui_controls.py'),str(state)])
client=None
report={'ok':False,'screenshots':0,'feature_manifests':0,'scope':'owned Qt window, actual guictl.py stdio process'}
try:
    deadline=time.monotonic()+12
    while not state.exists() and time.monotonic()<deadline and owned.poll() is None:time.sleep(.05)
    def actual():return json.loads(state.read_text(encoding='utf-8'))
    def observe(predicate):
        deadline=time.monotonic()+2
        while True:
            current=actual()
            if predicate(current):return
            if time.monotonic()>=deadline:raise AssertionError('UI state was not observed after the call returned')
            time.sleep(.03)
    inspection=subprocess.run([sys.executable,'-X','utf8',str(ROOT/'guictl.py'),'inspect','--window',str(actual()['window']),
        '--role','Button','--method','click','--backend','uia3','--limit','2'],
        capture_output=True,text=True,encoding='utf-8',timeout=25,creationflags=subprocess.CREATE_NO_WINDOW)
    inspected=json.loads(inspection.stdout)
    assert inspection.returncode==0 and inspected['ok'] and inspected['data']['returned']==2,inspection.stdout
    assert inspected['data']['references_valid_after_exit'] is False
    client=subprocess.Popen([sys.executable,'-X','utf8',str(ROOT/'guictl.py'),'session'],
        stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding='utf-8',
        creationflags=subprocess.CREATE_NO_WINDOW)
    replies=queue.Queue()
    def read_lines():
        for line in client.stdout:replies.put(line)
        replies.put(None)
    threading.Thread(target=read_lines,daemon=True).start()
    count=0
    def request(op,**args):
        global count
        count+=1
        client.stdin.write(json.dumps({'id':count,'op':op,'args':args})+'\n');client.stdin.flush()
        line=replies.get(timeout=25)
        assert line is not None,'CLI exited before replying'
        result=json.loads(line)
        assert result.get('id')==count and result.get('ok'),result
        return result['data']
    assert 'click' in request('schema')['shortcuts']
    source=request('attach',window=actual()['window'])['source']
    request('scan',source=source,limit=1)
    cache_results=[];readonly_cache_results=[]
    def ref(name,method):
        data=request('find',source=source,name=name,method=method,backend='uia3')
        assert data['matched']==1,name
        cache_results.append(data['cached'])
        again=request('find',source=source,name=name,method=method,backend='uia3')
        readonly_cache_results.append(again['cached'])
        return data['elements'][0]['ref']
    receipt=request('click',ref=ref('Increment','invoke'))
    assert receipt['status']=='executed_unverified'
    observe(lambda s:s['count']==1)
    entry=ref('Text entry','set')
    request('set_text',ref=entry,text='CLI ordinary control')
    assert request('read_text',ref=entry)['value']=='CLI ordinary control'
    observe(lambda s:s['text']=='CLI ordinary control')
    request('select',ref=ref('Second','select'))
    observe(lambda s:s['selected_rows']==[1])
    request('check',ref=ref('Option','toggle-on'));observe(lambda s:s['checked'])
    request('uncheck',ref=ref('Option','toggle-off'));observe(lambda s:not s['checked'])
    request('close')
    client.stdin.close();client.wait(timeout=10)
    assert client.returncode==0
    assert cache_results==[False]*5 and all(readonly_cache_results),(cache_results,readonly_cache_results)
    report.update(ok=True,replies=count,operations=['inspect','find','click','set_text','read_text','select','check','uncheck'],
                  one_shot_inspection=True,find_cache_results=cache_results,readonly_find_cache_results=readonly_cache_results,application_state_observed=True)
except Exception as exc:
    report.update(error_type=type(exc).__name__,error=str(exc),traceback=traceback.format_exc())
finally:
    if client and client.poll() is None:
        if not client.stdin.closed:client.stdin.close()
        try:client.wait(timeout=10)
        except subprocess.TimeoutExpired:
            client.terminate();client.wait(timeout=5)
    report['process_cleanup']=close_owned(owned)
output=ROOT/'reports/control-objects-20261024';output.mkdir(parents=True,exist_ok=True)
(output/'cli.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(report,ensure_ascii=False),flush=True)
raise SystemExit(0 if report['ok'] else 1)
