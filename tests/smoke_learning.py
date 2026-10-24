"""Learn on use across owned native/Qt windows, then rebind a new session."""
import json
from pathlib import Path
import sys
import uuid

ROOT=Path(__file__).resolve().parents[1]; sys.path.insert(0,str(ROOT))
from gui_api import GuiSession
from probe_universality import launch_owned,close_owned
from verify_discovery import build_fixture,data,wait_state


def run_case(framework, output):
    folder=output/(framework+'-'+uuid.uuid4().hex[:6]); folder.mkdir()
    state=folder/'state.json'
    command=[build_fixture('Build-LearningFixture.ps1'),state] if framework=='winforms' else [sys.executable,ROOT/'tests/fixtures/learning_qt.py',state]
    owned=launch_owned(command)
    report={'framework':framework,'scope':'owned GUI and independent state file','screenshots':0,'automatic_exploration_actions':0}
    options={'discovery_cache':folder/'metadata.json','learning_cache':folder/'learning.json'}
    try:
        original=wait_state(state,lambda row:row['pid']==owned.pid,process=owned)
        with GuiSession(**options) as gui:
            source=data(gui.request('attach',{'window':original['window'],'runtime':'native','backend':'uia3'}))['source']
            first=data(gui.request('find',{'source':source}))
            opener=next(r for r in first['elements'] if r['name']=='Open details' and r['role']=='Button')
            result=gui.request('call',{'ref':opener['ref'],'method':'invoke',
                'postcondition':{'selector':{'name':'Close details','role':'Button'},'field':'exists','equals':True,'timeout_ms':1500}})
            assert result['ok'],result
            wait_state(state,lambda row:row['open'] and row['opens']==1,process=owned)
            second=data(gui.request('find',{'source':source}))
            learned=data(gui.request('learning_graph',{'source':source,'relation':'transitions'}))
            changed=[e for e in learned['edges'] if e['structure_changed']]
            assert len(changed)==1,learned
            edge=changed[0]
            assert edge['from']==first['learning']['state'] and edge['to']==second['learning']['state'] and edge['structure_changed'],edge
            assert not edge['causality_verified'] and not edge['business_completion_proven']
            closer=next(r for r in second['elements'] if r['name']=='Close details' and r['role']=='Button')
            result=gui.request('call',{'ref':closer['ref'],'method':'invoke',
                'postcondition':{'selector':{'name':'Close details','role':'Button'},'field':'exists','equals':False,'timeout_ms':1500}})
            assert result['ok'],result
            wait_state(state,lambda row:not row['open'] and row['closes']==1,process=owned)
            final=data(gui.request('learning_graph',{'source':source,'relation':'transitions'}))
            assert len([e for e in final['edges'] if e['structure_changed']])==2,final
            report.update(learned_transitions=2,states=final['registered']['states'],normal_gui_actions=2,
                          open_and_close_independently_observed=True,causality_not_assumed=True)
        with GuiSession(**options) as gui:
            source=data(gui.request('attach',{'window':original['window'],'runtime':'native','backend':'uia3'}))['source']
            rebound=data(gui.request('learning_graph',{'source':source,'query':'Open details','relation':'transitions'}))
            assert len([e for e in rebound['edges'] if e['structure_changed']])==1,rebound
            current=[row for node in rebound['nodes'] for row in node['live_matches'] if row['name']=='Open details']
            assert len(current)==1 and current[0]['ref']!=opener['ref']
            assert all(not n['historical_record_authorizes_execution'] for n in rebound['nodes'])
            unchanged=wait_state(state,process=owned)
            assert unchanged['opens']==unchanged['closes']==1
            wire=Path(options['learning_cache']).read_text(encoding='utf-8')
            assert 'Open details' not in wire and opener['ref'] not in wire
            report.update(ok=True,persisted_history_reused=True,new_session_refs_resolved=True,
                          graph_query_did_not_replay=True,labels_and_refs_not_persisted=True)
    finally: report['owned_process_cleanup']=close_owned(owned)
    return report


if __name__=='__main__':
    output=Path(sys.argv[1]).resolve(); output.mkdir(parents=True,exist_ok=True)
    result={'ok':False,'cases':[],'production_app_writes':0,'profiles_modified':False}
    try:
        for name in ('winforms','qt'): result['cases'].append(run_case(name,output))
        result['ok']=all(row['ok'] and row['owned_process_cleanup']['closed'] for row in result['cases'])
    finally:
        (output/'learning-validation.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(result,ensure_ascii=False))
