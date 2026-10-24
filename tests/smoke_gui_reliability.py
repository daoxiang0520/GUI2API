"""Owned real-window verification. No production app input or profile writes."""
import json
from pathlib import Path
import sys
import uuid

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from gui_api import GuiSession
from probe_universality import launch_owned,close_owned
from verify_discovery import build_fixture,data,wait_state


def main(output):
    output.mkdir(parents=True,exist_ok=True)
    state=output/('state-'+uuid.uuid4().hex[:6]+'.json')
    owned=launch_owned([build_fixture('Build-ReliabilityFixture.ps1'),state])
    report={'ok':False,'screenshots':0,'production_app_writes':0,'profiles_modified':False,'automatic_retries':0}
    try:
        before=wait_state(state,lambda r:r['pid']==owned.pid,process=owned)
        with GuiSession(discovery_cache=output/'metadata.json',learning_cache=output/'learning.json') as gui:
            source=data(gui.request('attach',{'window':before['window'],'runtime':'native','backend':'auto'}))['source']
            catalog=data(gui.request('discover',{'source':source,'limit':3000}))
            def control(name,role):
                rows=[r for r in catalog['controls'] if r.get('name')==name and r.get('role')==role]
                assert len(rows)==1,[(r.get('name'),r.get('role'),r.get('binding_count')) for r in rows]
                return rows[0]
            a=control('Increment A','Button'); b=control('Increment B','Button')
            first=gui.request('discovery_invoke',{'capability':a['id'],'method':'invoke'})
            assert first['ok'],first
            wait_state(state,lambda r:r['count']==1,process=owned)
            second=gui.request('discovery_invoke',{'capability':b['id'],'method':'invoke'})
            assert second['ok'],second
            wait_state(state,lambda r:r['count']==11,process=owned)
            report['sequential_original_catalog_controls']=True
            native_edit=data(gui.request('find',{'source':source,'role':'Edit','method':'set'}))['elements']
            native_edit=[r for r in native_edit if 'Mutable entry'==r.get('name')]
            assert native_edit,native_edit
            entry=native_edit[0]
            set_result=gui.request('call',{'ref':entry['ref'],'method':'set_text','arguments':{'value':'owned validation'},
                'postcondition':{'selector':{'role':'Edit','backend':entry['backend']},'field':'value','equals':'owned validation','timeout_ms':1500}})
            assert set_result['ok'],set_result
            wait_state(state,lambda r:r['text']=='owned validation',process=owned)
            current=data(gui.request('find',{'source':source,'role':'Edit','backend':entry['backend']}))['elements'][0]
            assert current['fingerprint']==entry['fingerprint'] and current['name']=='',current
            rebound=data(gui.request('rebind',{'source':source,'node_id':entry['fingerprint'],'method':'set'}))
            assert rebound['status']=='resolved',rebound
            # Guard rejection is before SendInput, even with a valid editor.
            blocked=gui.request('call',{'ref':current['ref'],'method':'type_text','arguments':{'text':'MUST_NOT_TYPE','guard_refs':['s/forged']}})
            assert blocked['error']['code']=='stale_ref',blocked
            assert wait_state(state,process=owned)['text']=='owned validation'
            report.update(mutable_name_identity_stable=True,rebind_live_ref=True,postcondition=True,forged_guard_prevented_input=True)
            # Explicitly prepare focus once before the requested setter+Enter;
            # Windows may reject background activation. This is not a retry.
            clicked=gui.request('call',{'ref':current['ref'],'method':'click_input'})
            assert clicked['ok'],clicked
            report['focus_explicitly_requested']=True
            committed=gui.request('call',{'ref':current['ref'],'method':'set','arguments':{'text':'committed value','commit':'enter'}})
            assert committed['ok'],committed
            wait_state(state,lambda r:r['enters']==1 and r['text']=='committed value',process=owned)
            report['explicit_commit_one_enter']=True
            data(gui.desktop.request('window',{'window':before['window'],'pid':owned.pid,'state':'minimize'}))
            restored=data(gui.request('window_action',{'source':source,'action':'restore'}))
            assert restored['minimized'] is False,restored
            front=data(gui.request('window_action',{'source':source,'action':'bring_to_front'}))
            assert front['foreground'],front
            report['restore_and_bring_to_front']=True
            requested=data(gui.request('window_action',{'source':source,'action':'request_accessibility'}))
            assert requested['application_restarted'] is False and requested['global_settings_changed'] is False
            report['explicit_accessibility_request']=requested
            menu=data(gui.request('find',{'source':source,'name':'Open menu','role':'Button','backend':'win32'}))['elements']
            assert len(menu)==1
            opened=gui.request('call',{'ref':menu[0]['ref'],'method':'click_input'})
            assert opened['ok'],opened
            wait_state(state,lambda r:r['menu_visible'] and r['menus']==1,process=owned)
            cancelled=data(gui.request('window_action',{'source':source,'action':'dismiss_popup'}))
            if cancelled['status']=='no_standard_menu_observed':
                data(gui.request('window_action',{'source':source,'action':'escape'}))
            wait_state(state,lambda r:not r['menu_visible'],process=owned)
            report['popup_dismissed_without_editor_focus']=True
            terminal=data(gui.request('window_action',{'source':source,'action':'read_console'}))
            assert 'GUI2API_READONLY_CONSOLE_20261025' in terminal.get('value',''),terminal
            assert terminal['read_only'] and terminal['output_source']=='ReadConsoleOutputCharacterW',terminal
            report['isolated_console_output_read']=True
            report.update(catalog_counts={k:catalog[k] for k in ('control_count','operation_count','readable_control_count','writable_control_count')},
                multiple_methods_preserved=any(c['binding_count']>1 for c in catalog['controls']),
                final_state=wait_state(state,process=owned),ok=True)
    finally:
        report['cleanup']=close_owned(owned)
        (output/'reliability.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(report,ensure_ascii=False))


if __name__=='__main__':main(Path(sys.argv[1]).resolve())
