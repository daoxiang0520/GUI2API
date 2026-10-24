"""Use-based registration on a separate owned headless Chromium page."""
import json
from pathlib import Path
import sys
import time
import uuid

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from cdp_backend import targets
from gui_api import GuiSession
from probe_universality import application_path,launch_owned,close_owned
from verify_discovery import data


def main(output):
    output.mkdir(parents=True,exist_ok=True); run=output/('dom-'+uuid.uuid4().hex[:6]);run.mkdir()
    profile=run/'profile'; executable=application_path('chrome.exe') or application_path('msedge.exe')
    assert executable,'An installed Chromium is required'
    owned=launch_owned([executable,'--headless=new','--remote-debugging-address=127.0.0.1','--remote-debugging-port=0',
        '--user-data-dir='+str(profile),'--no-first-run','--disable-extensions','--disable-background-networking',
        (ROOT/'tests/fixtures/learning_surface.html').as_uri()])
    report={'ok':False,'scope':'owned local DOM and separate browser profile','screenshots':0,'messages_sent':0}
    try:
        port=profile/'DevToolsActivePort';deadline=time.monotonic()+15
        while not port.exists() and time.monotonic()<deadline and owned.poll() is None:time.sleep(.05)
        endpoint='http://127.0.0.1:'+port.read_text().splitlines()[0]
        page=None;deadline=time.monotonic()+12
        while time.monotonic()<deadline and owned.poll() is None:
            pages=[p for p in targets(endpoint) if p.get('title')=='AppCtl incremental learning DOM']
            if len(pages)==1:page=pages[0];break
            time.sleep(.05)
        assert page,'Owned page did not load'
        options={'discovery_cache':run/'metadata.json','learning_cache':run/'learning.json'}
        with GuiSession(**options) as gui:
            source=data(gui.request('attach',{'runtime':'cdp-dom','endpoint':endpoint,'target':page['id']}))['source']
            first=data(gui.request('find',{'source':source,'automation_id':'open'}));opener=first['elements'][0]['ref']
            assert gui.request('call',{'ref':opener,'method':'invoke'})['ok']
            learned=data(gui.request('learning_graph',{'source':source,'relation':'transitions'}))
            assert len(learned['edges'])==1 and learned['edges'][0]['structure_changed'],learned
            closer=data(gui.request('find',{'source':source,'automation_id':'close'}))['elements'][0]['ref']
            assert gui.request('call',{'ref':closer,'method':'invoke'})['ok']
            learned=data(gui.request('learning_graph',{'source':source,'relation':'transitions'}))
            assert len(learned['edges'])==2,learned
            adapter=gui.sources[source]['adapter'];conn,_=adapter.backend._connection(adapter.context)
            observed=conn.rpc('Runtime.evaluate',{'expression':'JSON.stringify(window.fixture)','returnByValue':True})
            observed=json.loads(observed['result']['value'])
            assert observed=={'opens':1,'closes':1,'open':False},observed
        with GuiSession(**options) as gui:
            source=data(gui.request('attach',{'runtime':'cdp-dom','endpoint':endpoint,'target':page['id']}))['source']
            rebound=data(gui.request('learning_graph',{'source':source,'relation':'transitions','query':'Open details'}))
            assert len(rebound['edges'])==1,rebound
            candidates=[r for n in rebound['nodes'] for r in n['live_matches'] if r['name']=='Open details']
            assert len(candidates)==1 and candidates[0]['ref']!=opener
            assert opener not in Path(options['learning_cache']).read_text(encoding='utf-8')
        report.update(ok=True,normal_gui_actions=2,automatic_exploration_actions=0,learned_transitions=2,
                      independent_dom_state_readback=True,persisted_history_reused=True,new_session_refs_resolved=True,
                      graph_query_did_not_replay=True)
    finally:
        report['owned_process_cleanup']=close_owned(owned)
        (output/'learning-dom.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(report,ensure_ascii=False))


if __name__=='__main__':main(Path(sys.argv[1]).resolve())
