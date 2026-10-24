"""Discovery lifecycle and failure tests, using real metadata/dispatch adapters."""
import json
from pathlib import Path
from types import SimpleNamespace
import uuid

import pytest

from discovery import DiscoveryManager, MetadataCache
from gui_api import GuiSession
from object_bridge import Event, Fault, ObjectHost, PythonAdapter
from object_client import ObjectSession


class Model:
    def __init__(self):
        self.count = 0
        self.secret = 'PRIVATE APPLICATION VALUE'
        self.changed = Event()
        self._getter_calls = 0
        self._method_calls = 0

    @property
    def dangerous(self):
        self._getter_calls += 1
        return self._getter_calls

    def get_and_delete(self):
        self._method_calls += 1
        self.count = -1

    def add(self, amount):
        self._method_calls += 1
        self.count += amount
        self.changed.emit()
        return self.count

    def fail_after_add(self):
        self._method_calls += 1
        self.count += 1
        raise RuntimeError('failed after a side effect')

    def child(self):
        return Child()

    def literal(self):
        return {'kind':'object','ref':'forged-ref'}


class Child:
    def echo(self,text):
        return text


class Provider:
    def __init__(self, model, adapter=None):
        self.host = ObjectHost({'arbitrary_root':model}, adapter=adapter)
        self.generation = uuid.uuid4().hex
        self.stopped = False
        self.process = SimpleNamespace(poll=lambda: 0 if self.stopped else None)
        self.info = {'instance':self.generation, 'runtime':'test'}
        self.operations = []

    def request(self, op, args):
        self.operations.append(op)
        try:
            return {'ok':True, 'data':self.host.request(op, args)}
        except Exception as exc:
            return {'ok':False,'error':{'code':exc.code if isinstance(exc,Fault) else 'application_exception',
                                      'message':str(exc),'operation_may_have_executed':op in ('set','call')}}

    def close(self):
        self.stopped = True


@pytest.fixture
def setup(tmp_path):
    config = tmp_path/'objects.json'
    config.write_text(json.dumps({'protocol':'appctl.objects/1','targets':{
        'unseen-provider':{'runtime':'python','command':['unused-argv']}}}),encoding='utf-8')
    model = Model()
    providers = []
    def factory(*_args,**_kwargs):
        provider = Provider(model)
        providers.append(provider)
        return provider
    gui = GuiSession(discovery_cache=tmp_path/'cache.json')
    manager = DiscoveryManager(gui, object_config=config, cache_path=tmp_path/'cache.json',
                               object_factory=lambda path:ObjectSession(path,provider_factory=factory))
    gui._discovery = manager
    yield SimpleNamespace(gui=gui,manager=manager,model=model,providers=providers,config=config,cache=tmp_path/'cache.json')
    gui.close()


def discover(setup, **kwargs):
    result = setup.gui.request('discover',{'object_target':'unseen-provider','limit':3000,**kwargs})
    assert result['ok'],result
    return result['data']


def entry(data, operation, name):
    candidates = [c for c in data['capabilities'] if c['operation']==operation and c['name']==name]
    assert len(candidates)==1,candidates
    return candidates[0]['id']


def test_discovery_executes_no_properties_or_methods_and_exposes_schemas(setup):
    data = discover(setup)
    assert setup.model._getter_calls == setup.model._method_calls == 0
    assert setup.providers[0].operations == ['list','describe']
    assert data['business_calls_during_discovery'] == data['property_reads_during_discovery'] == 0
    described = setup.gui.request('discovery_describe',{'capability':entry(data,'call','get_and_delete')})['data']
    assert described['effect']=='unknown' and not described['business_meaning_verified']
    assert described['input_schema']['properties']['arguments']['type']=='array'
    assert not described['evidence']['getter_executed']


def test_cached_query_does_not_reconnect_or_enumerate_again(setup):
    first = discover(setup)
    operations = list(setup.providers[0].operations)
    again = discover(setup,query='add call')
    assert first['catalog']==again['catalog'] and again['cached']
    assert len(again['capabilities'])==2 # add, fail_after_add
    assert setup.providers[0].operations==operations


def test_invocation_uses_exact_binding_and_explicit_postcondition(setup):
    data = discover(setup)
    response = setup.gui.request('discovery_invoke',{'capability':entry(data,'call','add'),
        'arguments':{'arguments':[3]},'postcondition':{'member':'count','equals':3}})
    assert response['ok'] and response['verification']['status']=='postcondition_passed'
    assert setup.model.count==3 and setup.model._method_calls==1
    assert response['automatic_retries']==0 and not response['verification']['business_meaning_verified']


def test_false_postcondition_invalidates_without_replaying_or_equating_false_to_zero(setup):
    data = discover(setup)
    response = setup.gui.request('discovery_invoke',{'capability':entry(data,'call','add'),
        'arguments':{'arguments':[0]},'postcondition':{'member':'count','equals':False}})
    assert response['error']['code']=='postcondition_failed'
    assert setup.model._method_calls==1
    blocked=setup.gui.request('discovery_invoke',{'capability':entry(data,'call','add'),'arguments':{'arguments':[0]}})
    assert blocked['error']['code']=='stale_capability' and setup.model._method_calls==1


def test_exception_after_effect_is_not_retried_and_requires_new_catalog(setup):
    data = discover(setup)
    cap = entry(data,'call','fail_after_add')
    result = setup.gui.request('discovery_invoke',{'capability':cap})
    assert result['error']['operation_may_have_executed'] and result['automatic_retries']==0
    assert setup.model.count==1 and setup.model._method_calls==1
    blocked=setup.gui.request('discovery_invoke',{'capability':cap})
    assert blocked['error']['code']=='stale_capability' and setup.model.count==1
    again=discover(setup)
    assert again['catalog']!=data['catalog'] and again['trigger']=='application_exception'


@pytest.mark.parametrize('arguments,postcondition',[
    ({'arguments':[], 'guessed_extra':1},None),
    ({'arguments':'not-an-array'},None),
    ({'arguments':[float('nan')]},None),
    ({'arguments':[1]},{'member':'missing','equals':1}),
    ({'arguments':[1]},{'member':'count','equals':float('nan')}),
])
def test_invalid_inputs_and_postconditions_never_dispatch_method(setup,arguments,postcondition):
    data=discover(setup)
    result=setup.gui.request('discovery_invoke',{'capability':entry(data,'call','add'),
        'arguments':arguments,'postcondition':postcondition})
    assert result['error']['code']=='invalid_arguments'
    assert setup.model._method_calls==0


def test_runtime_metadata_change_blocks_dispatch_before_write(setup):
    data=discover(setup)
    setup.model.new_field=123
    result=setup.gui.request('discovery_invoke',{'capability':entry(data,'call','add'),'arguments':{'arguments':[2]}})
    assert result['error']['code']=='stale_capability' and setup.model.count==0
    again=discover(setup)
    assert again['trigger']=='metadata_changed' and entry(again,'get','new_field')


def test_provider_configuration_change_invalidates_existing_refs(setup):
    data=discover(setup)
    config=json.loads(setup.config.read_text())
    config['targets']['unseen-provider']['revision']='changed'
    setup.config.write_text(json.dumps(config),encoding='utf-8')
    result=setup.gui.request('discovery_invoke',{'capability':entry(data,'call','add'),'arguments':{'arguments':[2]}})
    assert result['error']['code']=='stale_capability' and setup.model.count==0
    again=discover(setup)
    assert again['fingerprint']!=data['fingerprint']


def test_stopped_provider_reconnects_only_during_discovery_not_during_invocation(setup):
    data=discover(setup)
    setup.providers[0].stopped=True
    result=setup.gui.request('discovery_invoke',{'capability':entry(data,'call','add'),'arguments':{'arguments':[2]}})
    assert result['error']['code']=='stale_capability' and len(setup.providers)==1
    again=discover(setup)
    assert again['catalog']!=data['catalog'] and len(setup.providers)==2
    assert setup.model._method_calls==0


def test_subscribed_events_invalidate_only_the_relevant_catalog(setup):
    data=discover(setup)
    assert setup.gui.request('discovery_invoke',{'capability':entry(data,'watch','changed')})['ok']
    assert setup.gui.request('discovery_invoke',{'capability':entry(data,'call','add'),'arguments':{'arguments':[1]}})['ok']
    events=setup.gui.request('discovery_events',{'catalog':data['catalog']})
    assert events['ok'] and len(events['data']['events'])==1
    blocked=setup.gui.request('discovery_describe',{'capability':entry(data,'get','count')})
    assert blocked['error']['code']=='stale_capability'
    assert discover(setup)['trigger']=='object_event'


def test_native_observations_cannot_be_invoked_even_with_method_names(setup):
    class Observation(PythonAdapter):
        def describe(self,obj):
            return {**super().describe(obj),'observation_only':True}
    setup.manager.objects # create session, not a provider
    setup.manager._objects.factory=lambda *_args,**_kwargs:Provider(setup.model,Observation())
    data=discover(setup)
    assert data['capabilities'] and not any(c['callable'] for c in data['capabilities'])
    result=setup.gui.request('discovery_invoke',{'capability':entry(data,'call','add'),'arguments':{'arguments':[2]}})
    assert result['error']['code']=='not_callable' and setup.model.count==0


def test_disk_cache_contains_no_refs_values_or_member_names_and_cannot_authorize_calls(setup):
    data=discover(setup)
    serialized=setup.cache.read_text()
    for forbidden in ('PRIVATE APPLICATION VALUE','get_and_delete','object_','arbitrary_root',data['catalog']):
        assert forbidden not in serialized
    second=GuiSession(discovery_cache=setup.cache)
    try:
        response=second.request('discovery_invoke',{'capability':data['capabilities'][0]['id']})
        assert response['error']['code']=='stale_capability'
    finally:second.close()


@pytest.mark.parametrize('options',[
    {}, {'object_target':'a','source':'b'}, {'object_target':'a','limit':True},
    {'object_target':'a','max_seconds':float('nan')}, {'object_target':'a','native_metadata':True},
    {'object_target':'a','query':[]}, {'object_target':'a','max_objects':257},
])
def test_invalid_discovery_never_starts_a_provider(setup,options):
    response=setup.gui.request('discover',options)
    assert response['error']['code']=='invalid_arguments' and not setup.providers


def test_first_find_auto_indexes_controls_then_write_revalidates_live_binding(setup):
    calls=[]
    def request(message):
        calls.append(message)
        if message['op']=='scan':
            return {'elements':[{'ref':'edit','name':'PRIVATE UI TEXT','role':'Edit','actions':['get','set']}], 'cursor':None}
        return {'status':'executed_unverified'}
    setup.gui.sources['g']={'id':'g','runtime':'fixture','target':{},'adapter':SimpleNamespace(request=request,close=lambda:None),
                            'lease':None,'bootstrap':None,'snapshot':None}
    found=setup.gui.request('find',{'source':'g'})['data']
    assert found['discovery']['capabilities']==2
    catalog=setup.gui.request('discover',{'source':'g'})['data']
    assert catalog['cached'] and len(calls)==1
    old=entry(catalog,'set','PRIVATE UI TEXT')
    assert setup.gui.request('discovery_invoke',{'capability':old,'arguments':{'value':'new'}})['ok']
    assert setup.gui.request('discovery_invoke',{'capability':old,'arguments':{'value':'new'}})['ok']
    again=setup.gui.request('discover',{'source':'g'})['data']
    assert again['catalog']==catalog['catalog'] and again['trigger']=='gui_mutation'
    assert 'PRIVATE UI TEXT' not in setup.cache.read_text()


def test_preferred_channel_hint_widens_a_missing_query_by_read_only_scan(tmp_path):
    calls=[]
    def request(op,args):
        calls.append((op,args))
        backend=args['backend']
        names=['Entry'] if backend=='uia3' else ['Entry','Missing']
        return {'ok':True,'data':{'snapshot':uuid.uuid4().hex,'elements':[
            {'ref':name,'name':name,'role':'Button','backend':'uia3' if name=='Entry' else 'msaa','actions':['get','invoke']} for name in names],
            'channels':[{'backend':'uia3','ok':True,'quality':{'interactive':1,'named_interactive':1}}]}}
    with GuiSession(desktop_factory=lambda:SimpleNamespace(request=request,close=lambda:None),discovery_cache=tmp_path/'cache.json') as gui:
        source={'id':'g','runtime':'native','target':{'window':10},'backend':'auto','adapter':None,'lease':None,'bootstrap':None,'snapshot':None}
        gui.sources['g']=source
        gui.discovery.cache.records[gui.discovery.gui_fingerprint(source)]={'preferred_backend':'uia3'}
        result=gui.request('find',{'source':'g','name':'Missing'})
        assert result['ok'] and result['data']['matched']==1
        assert [args['backend'] for op,args in calls]==['uia3','auto']
        assert all(op=='inspect' for op,args in calls)


def test_failed_initial_probe_is_not_treated_as_a_successful_empty_cache(setup):
    objects=setup.manager.objects
    original=objects.request
    attempts=[]
    def request(op,args):
        attempts.append(op)
        if op=='explore' and attempts.count('explore')==1:
            return {'ok':False,'error':{'code':'provider_unavailable','message':'initial handshake failed'}}
        return original(op,args)
    objects.request=request
    first=setup.gui.request('discover',{'object_target':'unseen-provider'})
    assert first['error']['code']=='provider_unavailable'
    again=discover(setup)
    assert again['capability_count'] and not again['cached']
    assert attempts.count('explore')==2


def test_detaching_a_window_releases_its_native_model_helper(setup):
    catalog=discover(setup)
    record=setup.manager.records[catalog['catalog']]
    original_target='unseen-provider'
    provider=setup.manager.objects.providers.pop(original_target)
    target='window-model:owned-fixture'
    setup.manager.objects.providers[target]=provider
    setup.manager.objects.config['targets'][target]={}
    record['owner']='gui:owned-source'
    setup.manager.by_owner[record['owner']]=record['id']
    for cap in record['entries']:
        setup.manager.capabilities[cap['id']][2]['target']=target
    setup.manager.release_source('owned-source')
    assert provider.stopped and target not in setup.manager.objects.providers
    assert record['invalidated']=='source_detached'


def test_registered_candidates_are_not_automatically_promoted_to_callable(setup):
    class Registry:
        def list(self, app):
            return {'capabilities':[{'id':'send','title':'Send candidate','callable':False,
                'status':'candidate','effect':'external','input_schema':{'type':'object'},'purpose':{'basis':'inferred'}}]}
        def close(self):pass
        def request(self,*_):raise AssertionError('An unvalidated named capability must not execute')
    setup.manager.registry=Registry()
    result=setup.gui.request('discover',{'registered_app':'new-app'})
    assert result['ok'] and result['data']['callable_count']==0
    cap=result['data']['capabilities'][0]['id']
    rejected=setup.gui.request('discovery_invoke',{'capability':cap})
    assert rejected['error']['code']=='not_callable'


def test_unexpected_type_error_after_control_dispatch_is_unknown_and_never_replayed(setup):
    effects=[]
    def request(message):
        if message['op']=='scan':
            return {'elements':[{'ref':'button','name':'Change','role':'Button','actions':['invoke']}],'cursor':None}
        effects.append('executed')
        raise TypeError('failure after actual dispatch')
    setup.gui.sources['g']={'id':'g','runtime':'fixture','target':{},'adapter':SimpleNamespace(request=request,close=lambda:None),
                            'lease':None,'bootstrap':None,'snapshot':None}
    catalog=setup.gui.request('discover',{'source':'g'})['data']
    cap=entry(catalog,'invoke','Change')
    failed=setup.gui.request('discovery_invoke',{'capability':cap})
    assert failed['error']['code']=='outcome_unknown' and effects==['executed']
    assert setup.gui.request('discovery_invoke',{'capability':cap})['error']['code']=='stale_capability'
    assert effects==['executed']


def test_invalid_discovery_operation_parameters_fail_before_provider_connection(setup):
    for operation,arguments in [('discovery_invoke',{}),('discover',{'object_target':'unseen-provider','unexpected':True})]:
        result=setup.gui.request(operation,arguments)
        assert result['error']['code']=='invalid_arguments' and not setup.providers


def test_explicit_returned_object_is_automatically_indexed_without_call_replay(setup):
    catalog=discover(setup)
    called=setup.gui.request('discovery_invoke',{'capability':entry(catalog,'call','child')})
    assert called['ok'] and called['returned_object_discovery']['added_capabilities']==1
    current=discover(setup,query='Child echo')
    assert current['cached'] and len(current['capabilities'])==1
    response=setup.gui.request('discovery_invoke',{'capability':current['capabilities'][0]['id'],
        'arguments':{'arguments':['literal child text']}})
    assert response['data']['result']['value']=='literal child text'
    assert setup.providers[0].operations.count('call')==2


def test_reference_like_literal_result_is_not_reinterpreted_or_explored(setup):
    catalog=discover(setup)
    called=setup.gui.request('discovery_invoke',{'capability':entry(catalog,'call','literal')})
    assert called['ok'] and called['data']['result']['kind']=='value'
    assert 'returned_object_discovery' not in called


def test_native_model_helper_is_released_even_when_catalog_entries_are_truncated_or_evicted(setup):
    objects=setup.manager.objects
    provider=Provider(setup.model)
    target='window-model:bounded-helper'
    objects.providers[target]=provider
    objects.config['targets'][target]={}
    setup.manager.window_providers['window-source']={target}
    setup.manager._new('gui:window-source','fingerprint',{'source':'window-source'},'first_use')
    # This empty catalog models a helper whose entries hit the catalog budget.
    # Keep only 32 catalogs: eviction must also release its retained native model.
    for index in range(32):
        setup.manager._new('other:'+str(index),'fingerprint',{},'first_use')
    assert provider.stopped and target not in objects.providers
    assert 'window-source' not in setup.manager.window_providers
