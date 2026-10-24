"""External edits on owned windows, without screenshots or live personal apps."""
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from desktop import DesktopSession
from gui_api import GuiSession
from probe_universality import close_owned, launch_owned
from verify_discovery import build_fixture, data, wait_state


def main(output):
    output.mkdir(parents=True, exist_ok=True)
    owned, reports = [], []
    try:
        with GuiSession(discovery_cache=output/'cache.json') as gui, DesktopSession() as external:
            for framework in ('winforms', 'qt'):
                state = output/(framework+'-state.json')
                command = [build_fixture('Build-NativeFixture.ps1'), state, '--mutable-name'] if framework == 'winforms' else [
                    sys.executable, ROOT/'tests/fixtures/qt_gui_controls.py', state]
                process = launch_owned(command)
                owned.append(process)
                initial = wait_state(state, lambda row:row['pid']==process.pid, process=process)
                source = data(gui.request('attach', {'window':initial['window']}))['source']
                # Activate lazy accessibility bridges with a read, then let
                # startup notifications settle before establishing the baseline.
                data(gui.request('find', {'source':source}))
                time.sleep(.2)
                observed = data(gui.request('find', {'source':source, 'refresh':True}))
                assert observed['freshness']['watching'], observed['freshness']
                catalog = observed['discovery']['catalog']
                old_cap = next(c['id'] for c in gui.discovery.records[catalog]['entries'] if c['operation']=='invoke')
                label = '标准输入框' if framework == 'winforms' else 'Text entry'
                field = data(external.request('find', {'window':initial['window'], 'backend':'uia3',
                    'name':label, 'actionable':False, 'depth':20, 'limit':1000}))['elements']
                assert len(field)==1, field
                value = 'External '+framework+' change'
                start = time.perf_counter()
                effect = external.request('set', {'ref':field[0]['ref'], 'value':value})
                assert effect['ok'], effect
                wait_state(state, lambda row:row['standard_text' if framework=='winforms' else 'text']==value, process=process)
                deadline = time.monotonic()+3
                while True:
                    receipt = data(gui.request('discovery_events', {'catalog':catalog, 'timeout_ms':100}))
                    if receipt['events'] or time.monotonic() >= deadline:
                        break
                assert receipt['events'] and receipt['invalidated'], receipt
                rejected = gui.request('discovery_invoke', {'capability':old_cap})
                assert rejected['error']['code']=='stale_capability', rejected
                refreshed = data(gui.request('find', {'source':source}))
                assert not refreshed['cached'] and refreshed['discovery']['catalog']!=catalog
                if framework=='winforms':
                    assert any(row['name']=='已编辑的输入框' for row in refreshed['elements']), refreshed
                reports.append({'framework':framework, 'source':source, 'ok':True,
                    'external_edit_independently_observed':True, 'old_capability_rejected_before_effect':True,
                    'automatic_find_refresh':True, 'event_and_refresh_ms':round((time.perf_counter()-start)*1000,3),
                    'coalesced_events':len(receipt['events']), 'screenshots':0, 'mutation_retries':0})
            assert gui._events and len(gui._events.sources)==2
            thread = gui._events.thread
            for report in reports:
                data(gui.request('detach', {'source':report['source']}))
            assert gui._events.sources == {}
        assert not thread.is_alive(), 'Owned event thread was not stopped'
    finally:
        cleanup = [close_owned(process) for process in owned]
        result = {'ok':len(reports)==2 and all(row['closed'] for row in cleanup), 'cases':reports,
                  'owned_process_cleanup':cleanup, 'profile_mutations':False, 'messages_sent':0,
                  'scope':'owned WinForms and Qt windows; no live personal app writes'}
        (output/'events-validation.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main(Path(sys.argv[1]).resolve())
