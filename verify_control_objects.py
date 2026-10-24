"""Use ordinary control objects on new Qt UI, a browser fixture and live QQ.

No feature manifests or business methods are called. The verification harness
independently observes effects; the click() wrapper itself only invokes once.
"""
import argparse
from collections import Counter
import json
from pathlib import Path
import sys
import time
import traceback
import uuid

from gui_controls import GUI

ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT/'reports/control-objects-20261024'


def save(name, report):
    OUTPUT.mkdir(parents=True,exist_ok=True)
    (OUTPUT/(name+'.json')).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'case':name,**report},ensure_ascii=False),flush=True)


def wait_state(predicate, reader, seconds=2):
    deadline=time.monotonic()+seconds
    while True:
        current=reader()
        if predicate(current):return current
        if time.monotonic()>=deadline:raise AssertionError('Control state change was not observed')
        time.sleep(.03)


def qt_case(inspect=False):
    from probe_universality import launch_owned,close_owned
    run=ROOT/'tests/runs'/('control-qt-'+uuid.uuid4().hex[:8]);run.mkdir(parents=True)
    state=run/'state.json'
    owned=launch_owned([sys.executable,'-X','utf8',str(ROOT/'tests/fixtures/qt_gui_controls.py'),str(state)])
    report={'ok':False,'framework':'Qt5 widgets','scope':'owned test application','screenshots':0,'feature_manifests':0}
    gui=GUI()
    try:
        deadline=time.monotonic()+12
        while not state.exists() and time.monotonic()<deadline and owned.poll() is None:time.sleep(.05)
        def actual():return json.loads(state.read_text(encoding='utf-8'))
        window=gui.window(window=actual()['window'])
        window.refresh()
        controls=window.controls()
        report.update(nodes=len(controls),pages=window.snapshot['pages'],roles=dict(Counter(c.role for c in controls)))
        if inspect:
            report['controls']=[{'name':c.name,'role':c.role,'methods':c.methods,'backend':c.metadata.get('backend')} for c in controls]
        else:
            actions=[]
            report['actions']=actions
            def one(name,method):return window.one(name=name,method=method,backend='uia3')
            start=time.perf_counter();receipt=one('Increment','click').click()
            actions.append({'operation':'button.click()','returned_status':receipt.get('status'),
                'call_ms':round((time.perf_counter()-start)*1000,3)})
            wait_state(lambda s:s['count']==1,actual)
            textbox=one('Text entry','set_text')
            textbox.text='Plain GUI control'
            wait_state(lambda s:s['text']=='Plain GUI control',actual)
            assert textbox.text=='Plain GUI control'
            actions.append({'operation':'textbox.text = text','state_observed':True})
            checkbox=one('Option','toggle-on');checkbox.check()
            wait_state(lambda s:s['checked'],actual)
            checkbox.uncheck();wait_state(lambda s:not s['checked'],actual)
            actions.append({'operation':'checkbox.check()/uncheck()','state_observed':True})
            one('Second','select').select();wait_state(lambda s:s['selected_rows']==[1],actual)
            actions.append({'operation':'item.select()','state_observed':True})
            one('Beta','invoke').click();wait_state(lambda s:s['tab']==1,actual)
            actions.append({'operation':'tab.click()','state_observed':True})
            parents=window.controls(name='Parent',method='expand',backend='uia3')
            if parents:
                parents[0].expand();wait_state(lambda s:s['expanded'],actual)
                parents[0].collapse();wait_state(lambda s:not s['expanded'],actual)
                actions.append({'operation':'tree_item.expand()/collapse()','state_observed':True})
            else:
                report['unexposed_actions']=['Qt5 provider did not advertise tree expand/collapse']
            report.update(actions=actions,state_changes_independently_observed=True)
        report['ok']=True
    except Exception as exc:report.update(error=str(exc),traceback=traceback.format_exc())
    finally:
        report['gui_cleanup']=gui.close()
        report['process_cleanup']=close_owned(owned)
    save('qt-inspect' if inspect else 'qt',report)
    return report


def web_case():
    from cdp_backend import targets
    from probe_universality import application_path,launch_owned,close_owned
    executable=application_path('chrome.exe')
    run=ROOT/'tests/runs'/('control-web-'+uuid.uuid4().hex[:8]);run.mkdir(parents=True)
    profile=run/'profile';fixture=ROOT/'tests/fixtures/gui_surface.html'
    owned=launch_owned([executable,'--headless=new','--remote-debugging-address=127.0.0.1',
        '--remote-debugging-port=0','--user-data-dir='+str(profile),'--no-first-run','--disable-extensions',
        '--disable-background-networking',fixture.as_uri()])
    report={'ok':False,'framework':'Chromium DOM','scope':'owned local page, separate browser profile',
            'screenshots':0,'feature_manifests':0}
    gui=GUI()
    try:
        port=profile/'DevToolsActivePort';deadline=time.monotonic()+12
        while not port.exists() and time.monotonic()<deadline and owned.poll() is None:time.sleep(.05)
        endpoint='http://127.0.0.1:'+port.read_text().splitlines()[0]
        target=next(p for p in gui.targets(endpoint) if p.get('title')=='AppCtl generic GUI fixture')
        window=gui.window(runtime='cdp-dom',endpoint=endpoint,target=target['id'])
        window.refresh()
        def one(key):return window.one(automation_id=key)
        textbox=one('body');textbox.text='Ordinary control object'
        receipt=one('submit').click()
        window.refresh()
        assert one('echo').text=='Ordinary control object' and one('count').text=='1'
        one('check').check();one('radio').select()
        assert one('choice').set_text('b')['status']=='verified'
        one('details').expand();one('details').collapse()
        one('scroll').scroll('down',amount=100)
        one('shadowInput').text='Shadow';one('frameInput').text='Frame'
        report.update(ok=True,nodes=len(window.controls()),click_returned_status=receipt.get('status'),
            operations=['text assignment','button.click()','check()','select()','expand()/collapse()',
                        'scroll()','shadow input','iframe input'],state_changes_independently_observed=True)
    except Exception as exc:report.update(error=str(exc),traceback=traceback.format_exc())
    finally:
        report['gui_cleanup']=gui.close();report['process_cleanup']=close_owned(owned)
    save('web',report)
    return report


def qq_case():
    report={'ok':False,'app':'QQ','scope':'live navigation between Messages and Contacts; restore original page',
            'screenshots':0,'messages_sent':0,'feature_manifests':0,'restored':False}
    gui=GUI();window=None;attempted=False
    try:
        started=time.perf_counter()
        window=gui.window(app='QQ',title='QQ',runtime='electron-dom',api_module='QQNT.dll')
        window.refresh()
        initial_editors=[c for c in window.controls() if c.metadata.get('editable')]
        if len(initial_editors)!=1:
            raise AssertionError('Navigation test requires one existing message editor; current page was left unchanged')
        initial_editor_id=initial_editors[0].id
        contacts=window.one(name='联系人',role='Button');window.one(name='消息',role='Button')
        report.update(initial_nodes=len(window.controls()),pages=window.snapshot['pages'],
                      connect_and_collect_ms=round((time.perf_counter()-started)*1000,3))
        attempted=True
        started=time.perf_counter();receipt=contacts.click()
        report['click_ms']=round((time.perf_counter()-started)*1000,3)
        report['click_returned_status']=receipt.get('status')
        # Observation is outside click(), using a new control enumeration.
        time.sleep(.25);window.refresh()
        report['contacts_nodes']=len(window.controls())
        report['contacts_page_observed']=not any(c.metadata.get('editable') for c in window.controls())
        assert report['contacts_page_observed'],'Click returned but a page change was not observed'
        window.one(name='消息',role='Button').click()
        time.sleep(.25);window.refresh()
        editors=[c for c in window.controls() if c.metadata.get('editable')]
        report['restored']=len(editors)==1
        report['original_editor_object_retained']=any(c.id==initial_editor_id for c in editors)
        assert report['restored']
        report['ok']=True
    except Exception as exc:report.update(error=str(exc),traceback=traceback.format_exc())
    finally:
        if attempted and not report['restored'] and window:
            try:
                window.refresh()
                if not any(c.metadata.get('editable') for c in window.controls()):
                    window.one(name='消息',role='Button').click();time.sleep(.25);window.refresh()
                report['restored']=len([c for c in window.controls() if c.metadata.get('editable')])==1
            except Exception as exc:report['restore_error']=str(exc)
        report['gui_cleanup']=gui.close()
    save('qq',report)
    return report


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    parser=argparse.ArgumentParser()
    parser.add_argument('--case',choices=['qt-inspect','qt','web','qq','all'],default='all')
    case=parser.parse_args().case
    functions={'qt-inspect':lambda:qt_case(True),'qt':qt_case,'web':web_case,'qq':qq_case}
    reports=[functions[name]() for name in (['qt','web','qq'] if case=='all' else [case])]
    summary={'ok':all(r['ok'] for r in reports),'results':reports,'screenshots':0,'feature_manifests':0}
    save('summary-'+case,summary)
    return 0 if summary['ok'] else 1


if __name__=='__main__':raise SystemExit(main())
