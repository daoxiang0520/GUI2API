"""Exercise one discovery layer across runtimes, owned GUIs and running apps.

Owned fixtures may be changed. Live application checks are metadata/read-only;
they send no messages and do not activate, restore or restart the applications.
"""
import argparse
from collections import Counter
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import traceback
import uuid

from gui_api import GuiSession

ROOT = Path(__file__).resolve().parent


def data(response):
    assert response.get('ok'), response
    return response['data']


def select(catalog, name, operation, kind=None, provider=None):
    matches = [c for c in catalog['capabilities'] if c['name']==name and c['operation']==operation
               and (not kind or c['kind']==kind) and (not provider or c['provider']==provider)]
    assert len(matches)==1, {'name':name,'operation':operation,'matches':matches}
    return matches[0]['id']


def timed(call):
    start=time.perf_counter(); value=call()
    return value, round((time.perf_counter()-start)*1000,3)


def wait_state(path, predicate=lambda _:True, timeout=12, process=None):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        try:
            value=json.loads(path.read_text(encoding='utf-8-sig'))
            if predicate(value): return value
        except (OSError,ValueError): pass
        if process and process.poll() is not None: raise AssertionError('Owned fixture stopped before observation')
        time.sleep(.03)
    raise AssertionError('Expected state was not independently observed')


def object_case(target, output):
    report={'case':target,'scope':'owned runtime object','screenshots':0,'feature_manifests':0}
    with GuiSession(object_config=ROOT/'objects.example.json',discovery_cache=output/'cache.json') as session:
        catalog,report['discovery_ms']=timed(lambda:data(session.request('discover',{'object_target':target,'limit':3000})))
        report.update(capabilities=catalog['capability_count'], callable=catalog['callable_count'], channels=catalog['channels'],
                      property_reads_during_discovery=catalog['property_reads_during_discovery'], business_calls_during_discovery=catalog['business_calls_during_discovery'])
        again,report['cached_query_ms']=timed(lambda:data(session.request('discover',{'object_target':target,'query':'add','limit':3000})))
        assert again['cached'] and again['catalog']==catalog['catalog']
        report['cache_reused']=True
        if target=='com-demo':
            result=session.request('discovery_invoke',{'capability':select(catalog,'Add','call'),
                'arguments':{'arguments':['discovery-owned','literal value']},'postcondition':{'member':'Count','equals':1}})
            assert result['ok'] and result['verification']['status']=='postcondition_passed',result
            result=session.request('discovery_invoke',{'capability':select(catalog,'Exists','call'), 'arguments':{'arguments':['discovery-owned']}})
            assert data(result)['result']['value'] is True
            report['actual_com_idispatch']=True
        else:
            methods=[c for c in catalog['capabilities'] if c['operation']=='call' and c['name'].split('(')[0]=='add']
            assert len(methods)==1,methods
            result=session.request('discovery_invoke',{'capability':methods[0]['id'],'arguments':{'arguments':[4]},
                'postcondition':{'member':'count','equals':4}})
            assert result['ok'] and result['verification']['status']=='postcondition_passed',result
            if target!='qt-demo':
                result=session.request('discovery_invoke',{'capability':select(catalog,'probe','get')})
                assert data(result)['result']['value']==1,result
                report['getter_not_executed_by_discovery']=True
                child_members=[c for c in catalog['capabilities'] if c['operation']=='call' and c['name'].split('(')[0]=='child']
                assert len(child_members)==1,child_members
                child=data(session.request('discovery_invoke',{'capability':child_members[0]['id']}))
                assert child['result']['kind']=='object',child
                child_catalog=data(session.request('discover',{'object_target':target,'query':'Child echo','limit':3000}))
                assert len(child_catalog['capabilities'])==1,child_catalog
                echo=data(session.request('discovery_invoke',{'capability':child_catalog['capabilities'][0]['id'],
                    'arguments':{'arguments':['returned object literal']}}))
                assert echo['result']['value']=='returned object literal',echo
                report['returned_object_automatically_discovered_and_called']=True
            event=select(catalog,'changed','watch')
            assert session.request('discovery_invoke',{'capability':event})['ok']
            result=session.request('discovery_invoke',{'capability':methods[0]['id'],'arguments':{'arguments':[1]}})
            assert result['ok'],result
            events=data(session.request('discovery_events',{'catalog':catalog['catalog']}))
            assert len(events['events'])==1,events
            assert session.request('discovery_describe',{'capability':methods[0]['id']})['error']['code']=='stale_capability'
            refreshed=data(session.request('discover',{'object_target':target,'limit':3000}))
            assert refreshed['catalog']!=catalog['catalog'] and refreshed['trigger']=='object_event'
            report['event_invalidation_and_rediscovery']=True
        report.update(ok=True,method_with_state_postcondition=True,automatic_mutation_retries=0)
    return report


def build_fixture(script):
    powershell=Path(os.environ.get('SystemRoot',r'C:\Windows'))/'System32/WindowsPowerShell/v1.0/powershell.exe'
    result=subprocess.run([str(powershell),'-NoProfile','-NonInteractive','-File',str(ROOT/'tests/fixtures'/script)],
                           capture_output=True,encoding='utf-8',timeout=25,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    assert result.returncode==0,result.stderr
    return result.stdout.strip().splitlines()[-1]


def owned_gui_case(framework, output):
    from probe_universality import launch_owned,close_owned
    run=output/(framework+'-'+uuid.uuid4().hex[:6]);run.mkdir()
    state=run/'state.json'
    if framework=='qt':
        command=[sys.executable,str(ROOT/'tests/fixtures/qt_gui_controls.py'),str(state)]
        button_name,edit_name,edit_state='Increment','Text entry','text'
    elif framework=='native-model':
        command=[build_fixture('Build-DiscoveryFixture.ps1'),str(state)]
        button_name,edit_name,edit_state='Increment model','Model entry','text'
    else:
        command=[build_fixture('Build-NativeFixture.ps1'),str(state)]
        button_name,edit_name,edit_state='自绘计数增加','自绘输入槽','custom_text'
    owned=launch_owned(command)
    report={'case':framework,'scope':'owned GUI','screenshots':0,'feature_manifests':0}
    try:
        original=wait_state(state,process=owned)
        with GuiSession(discovery_cache=output/'cache.json') as session:
            source=data(session.request('attach',{'window':original['window']}))['source']
            catalog,report['discovery_ms']=timed(lambda:data(session.request('discover',{'source':source,'limit':3000})))
            report.update(capabilities=catalog['capability_count'],callable=catalog['callable_count'],channels=catalog['channels'])
            again,report['cached_query_ms']=timed(lambda:data(session.request('discover',{'source':source,'limit':3000})))
            assert again['cached'] and again['catalog']==catalog['catalog']
            report['cache_reused']=True
            if framework=='native-model':
                cap=select(catalog,'Add','call',kind='object')
                receipt=session.request('discovery_invoke',{'capability':cap,'arguments':{'arguments':[7]},'postcondition':{'member':'Count','equals':7}})
                assert receipt['ok'],receipt
                wait_state(state,lambda s:s['count']==7,process=owned)
                report['native_model_discovered_without_configuration']=True
                report['business_method_changed_visible_model_state']=True
                expected_count=8
            else: expected_count=1
            candidates=[c for c in catalog['capabilities'] if c['name']==button_name and c['operation']=='invoke' and c['kind']=='control']
            for backend in ('uia3','msaa','win32'):
                chosen=[c for c in candidates if c['provider']==backend]
                if len(chosen)==1:break
            assert len(chosen)==1,candidates
            cap=chosen[0]['id']
            receipt=session.request('discovery_invoke',{'capability':cap})
            assert receipt['ok'],receipt
            wait_state(state,lambda s:s['count']==expected_count,process=owned)
            stale=session.request('discovery_invoke',{'capability':cap})
            assert stale['error']['code']=='stale_capability',stale
            catalog=data(session.request('discover',{'source':source,'limit':3000}))
            assert catalog['trigger']=='gui_mutation'
            candidates=[c for c in catalog['capabilities'] if c['name']==edit_name and c['operation']=='set' and c['kind']=='control']
            for backend in ('uia3','msaa','win32'):
                chosen=[c for c in candidates if c['provider']==backend]
                if len(chosen)==1:break
            assert len(chosen)==1,candidates
            receipt=session.request('discovery_invoke',{'capability':chosen[0]['id'],'arguments':{'value':'Discovery API literal text'}})
            assert receipt['ok'],receipt
            wait_state(state,lambda s:s[edit_state]=='Discovery API literal text',process=owned)
            report.update(ok=True,button_effect_independently_observed=True,text_effect_independently_observed=True,
                          stale_catalog_rejected=True,rediscovery_after_mutation=True,automatic_mutation_retries=0)
            if framework=='native-model':
                helpers=[p for key,p in session.discovery.objects.providers.items() if key.startswith('window-model:')]
                assert helpers,'No native-model helper was retained for lifecycle validation'
                data(session.request('detach',{'source':source}))
                assert all(p.process.poll() is not None for p in helpers),'Window detach did not stop its owned COM helper'
                report['detach_released_native_model_helper']=True
    finally:report['owned_process_cleanup']=close_owned(owned)
    return report


def dom_case(output):
    from probe_universality import application_path,launch_owned,close_owned
    from cdp_backend import targets
    run=output/('dom-'+uuid.uuid4().hex[:6]);run.mkdir()
    profile=run/'profile'
    executable=application_path('chrome.exe') or application_path('msedge.exe')
    assert executable,'No installed Chromium for the owned fixture'
    owned=launch_owned([executable,'--headless=new','--remote-debugging-address=127.0.0.1','--remote-debugging-port=0',
        '--user-data-dir='+str(profile),'--no-first-run','--disable-extensions','--disable-background-networking',
        (ROOT/'tests/fixtures/gui_surface.html').as_uri()])
    report={'case':'chromium-dom','scope':'owned local page and separate headless profile','screenshots':0,'feature_manifests':0}
    try:
        port=profile/'DevToolsActivePort';deadline=time.monotonic()+15
        while not port.exists() and time.monotonic()<deadline and owned.poll() is None:time.sleep(.05)
        endpoint='http://127.0.0.1:'+port.read_text().splitlines()[0]
        page=None; deadline=time.monotonic()+12
        while time.monotonic()<deadline and owned.poll() is None:
            matches=[p for p in targets(endpoint) if p.get('title')=='AppCtl generic GUI fixture']
            if len(matches)==1:
                page=matches[0];break
            time.sleep(.05)
        assert page,'Owned page did not finish loading before its deadline'
        with GuiSession(discovery_cache=output/'cache.json') as session:
            source=data(session.request('attach',{'runtime':'cdp-dom','endpoint':endpoint,'target':page['id']}))['source']
            catalog,report['discovery_ms']=timed(lambda:data(session.request('discover',{'source':source,'limit':3000})))
            report.update(capabilities=catalog['capability_count'],callable=catalog['callable_count'])
            def by_id(catalog,automation_id,operation):
                choices=[]
                for cap in catalog['capabilities']:
                    if cap['operation']!=operation:continue
                    description=data(session.request('discovery_describe',{'capability':cap['id']}))
                    if description['evidence'].get('automation_id')==automation_id:choices.append(cap['id'])
                assert len(choices)==1,choices
                return choices[0]
            assert session.request('discovery_invoke',{'capability':by_id(catalog,'body','set'),'arguments':{'value':'Discovered DOM'}})['ok']
            catalog=data(session.request('discover',{'source':source,'limit':3000}))
            assert session.request('discovery_invoke',{'capability':by_id(catalog,'submit','invoke')})['ok']
            catalog=data(session.request('discover',{'source':source,'limit':3000}))
            count=data(session.request('discovery_invoke',{'capability':by_id(catalog,'count','get')}))
            echo=data(session.request('discovery_invoke',{'capability':by_id(catalog,'echo','get')}))
            assert str(count.get('value'))=='1' and echo.get('value')=='Discovered DOM',(count,echo)
            # Navigation/DOM instance invalidation is enforced by the existing
            # runtime in addition to catalog invalidation on known mutations.
            report.update(ok=True,dom_state_readback=True,rediscovery_after_mutation=True,automatic_mutation_retries=0)
    finally:report['owned_process_cleanup']=close_owned(owned)
    return report


def live_cases(output, selected=None):
    # Application selection belongs only to this verification harness. The
    # discovery engine contains no QQ/WeChat/WPS/service/member selectors.
    requested=['QQ','Weixin','wps','cloudmusic','chrome','msedge','BaiduNetdisk','WindowsTerminal','explorer']
    reports=[]
    with GuiSession(discovery_cache=output/'cache.json') as session:
        windows=data(session.request('apps'))['windows']
        for app in requested:
            if selected and app!=selected:continue
            candidates=[w for w in windows if w.get('app','').casefold()==app.casefold()]
            if not candidates:
                reports.append({'case':app,'scope':'live application','status':'not_running','ok':None})
                continue
            report={'case':app,'scope':'live application metadata and one control read','screenshots':0,
                    'business_calls':0,'messages_sent':0,'foreground_changes':0,'runtime_injection':0}
            source=None
            try:
                window=candidates[0]
                source=data(session.request('attach',{'window':window['window']}))['source']
                record=session.sources[source]
                report['framework_hints']=record['discovery_info'].get('framework_hints',[])
                catalog,report['discovery_ms']=timed(lambda:data(session.request('discover',{
                    'source':source,'limit':3000,'native_metadata':app in ('QQ','Weixin')})))
                again,report['cached_query_ms']=timed(lambda:data(session.request('discover',{'source':source,'limit':3000})))
                assert again['cached'] and catalog['catalog']==again['catalog']
                report.update(capabilities=catalog['capability_count'],callable=catalog['callable_count'],truncated=catalog['truncated'],
                    channels=catalog['channels'],kinds=dict(Counter(c['kind'] for c in catalog['capabilities'])),cache_reused=True,
                    native_metadata_callable=any(c['callable'] for c in catalog['capabilities'] if c['kind']=='native_metadata'))
                content_found=any(channel.get('quality',{}).get('content_interactive',0) or channel.get('quality',{}).get('settable',0) for channel in catalog['channels'])
                report['discovery_status']='content_controls_observed' if content_found else 'structure_only'
                report['end_to_end_application_task_tested']=False
                reads=[c for c in catalog['capabilities'] if c['kind']=='control' and c['operation']=='get']
                if reads:
                    response=session.request('discovery_invoke',{'capability':reads[0]['id']})
                    report['control_read_returned']=bool(response.get('ok'))
                    if not response.get('ok'):report['read_error']=response.get('error',{})
                report['ok']=bool(catalog['callable_count'] and report.get('control_read_returned'))
                report['coverage']='exposed current controls only; no business function or complete GUI coverage claim'
            except Exception as exc:
                report.update(ok=False,error=str(exc))
            finally:
                if source:session.request('detach',{'source':source})
            reports.append(report)
            print(json.dumps(report,ensure_ascii=False),flush=True)
    return reports


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stage',choices=['objects','owned-gui','live','all'],default='all')
    parser.add_argument('--output',type=Path)
    parser.add_argument('--case',help='Run only this owned case or live application; useful for targeted rechecks')
    options=parser.parse_args()
    output=options.output or ROOT/'reports'/('discovery-'+datetime.now().strftime('%Y%m%d-%H%M%S'))
    output=output.resolve();output.mkdir(parents=True,exist_ok=True)
    report={'format':'appctl.discovery-validation/1','observed_at':datetime.now().astimezone().isoformat(),
            'cases':[],'production_dsh_installed_or_restarted':False,'screenshots':0,'messages_sent':0}
    def save():
        (output/'result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    cases=[]
    if options.stage in ('objects','all'):
        cases.extend((name,lambda name=name:object_case(name,output)) for name in ('python-demo','javascript-demo','dotnet-demo','qt-demo','com-demo'))
    if options.stage in ('owned-gui','all'):
        cases.extend((name,lambda name=name:owned_gui_case(name,output)) for name in ('native-model','winforms-msaa','qt'))
        cases.append(('chromium-dom',lambda:dom_case(output)))
    for name,check in cases:
        if options.case and name!=options.case:continue
        print(json.dumps({'starting':name}),flush=True)
        try:result=check()
        except Exception as exc:result={'case':name,'ok':False,'error':str(exc),'traceback':traceback.format_exc()}
        report['cases'].append(result);save()
        print(json.dumps(result,ensure_ascii=False),flush=True)
    if options.stage in ('live','all'):
        report['cases'].extend(live_cases(output,options.case));save()
    report['passed']=sum(c.get('ok') is True for c in report['cases'])
    report['failed']=sum(c.get('ok') is False for c in report['cases'])
    report['unavailable']=sum(c.get('ok') is None for c in report['cases'])
    report['ok']=report['failed']==0
    save()
    print(json.dumps({'path':str(output/'result.json'), 'ok':report['ok'], 'passed':report['passed'],'failed':report['failed'],'unavailable':report['unavailable']}),flush=True)
    return 0 if report['ok'] else 1


if __name__=='__main__':raise SystemExit(main())
