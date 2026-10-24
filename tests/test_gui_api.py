"""Failure boundaries for dynamic control invocation, without personal apps."""
from types import SimpleNamespace
import time

import pytest

from gui_api import GuiSession
from guictl import dispatch_line


class Adapter:
    def __init__(self): self.calls = []
    def request(self, request):
        self.calls.append(request)
        if request['op'] == 'scan':
            return {'elements':[{'ref':'element-1','parent':None,'name':'field','role':'Edit',
                                 'actions':['get','set']}], 'cursor':None,'truncated':False}
        return {'status':'executed_unverified'}
    def close(self): return {'closed':True}


@pytest.fixture
def gui():
    value = GuiSession()
    for key in ('a', 'b'):
        value.sources[key] = {'id':key,'runtime':'fixture','target':{},'adapter':Adapter(),
                              'lease':None,'bootstrap':None,'snapshot':None}
    yield value
    value.close()


def node(gui, source='a'):
    return gui.request('scan', {'source':source})['data']['elements'][0]['ref']


@pytest.mark.parametrize('arguments', [{}, {'value':'x','extra':'y'}, {'value':True}, {'value':float('nan')}, {'value':'x'*100001}])
def test_invalid_arguments_never_reach_the_runtime(gui, arguments):
    ref = node(gui)
    result = gui.request('call', {'ref':ref,'method':'set','arguments':arguments})
    assert result['error']['code'] == 'invalid_arguments'
    assert len(gui.sources['a']['adapter'].calls) == 1


def test_dynamic_node_methods_are_described_without_feature_registration(gui):
    ref = node(gui)
    data = gui.request('describe', {'ref':ref})['data']
    assert set(data['methods']) == {'get','set'}
    assert data['methods']['set']['inputSchema']['required'] == ['value']
    assert gui.request('schema')['data']['app_feature_registration_required'] is False


def test_cli_shortcuts_call_the_same_ref_without_business_validation(gui):
    ref = node(gui)
    result = gui.request('set_text', {'ref':ref, 'text':'hello'})
    assert result['ok']
    request = gui.sources['a']['adapter'].calls[-1]
    assert request['method'] == 'set' and request['arguments'] == {'value':'hello'}
    assert gui.request('read_text', {'ref':ref})['ok']
    assert gui.sources['a']['adapter'].calls[-1]['method'] == 'get'
    before = len(gui.sources['a']['adapter'].calls)
    assert gui.request('click', {'ref':ref})['error']['code'] == 'unsupported'
    assert len(gui.sources['a']['adapter'].calls) == before


def test_original_source_is_used_and_unknown_outcome_is_not_replayed(gui):
    ref = node(gui, 'a')
    node(gui, 'b')
    original = gui.sources['a']['adapter']
    def failed(request):
        original.calls.append(request)
        return {'error':'outcome_unknown','message':'disconnected after invocation'}
    original.request = failed
    result = gui.request('call', {'ref':ref,'method':'set','arguments':{'value':'x'}})
    assert result['error']['code'] == 'outcome_unknown'
    assert len(original.calls) == 2
    assert len(gui.sources['b']['adapter'].calls) == 1


def test_expired_and_detached_refs_cannot_call_a_replacement_source(gui):
    ref = node(gui)
    gui.refs[ref]['seen'] = time.monotonic()-601
    assert gui.request('call', {'ref':ref,'method':'get'})['error']['code'] == 'stale_ref'
    gui.request('detach', {'source':'a'})
    assert gui.request('describe', {'ref':ref})['error']['code'] == 'stale_ref'


def test_unadvertised_methods_and_foreign_subtree_are_rejected(gui):
    ref = node(gui)
    assert gui.request('call', {'ref':ref,'method':'invoke'})['error']['code'] == 'unsupported'
    assert gui.request('scan', {'source':'b','root_ref':ref})['error']['code'] == 'invalid_target'


def test_native_pagination_keeps_one_snapshot_and_rejects_old_cursor():
    calls = []
    def request(op, args):
        calls.append((op,args))
        return {'ok':True,'data':{'elements':[{'ref':str(i),'actions':['get'],'parent':None} for i in range(5)],'truncated':False}}
    with GuiSession(desktop_factory=lambda:SimpleNamespace(request=request,close=lambda:None)) as gui:
        gui.sources['native'] = {'id':'native','runtime':'native','target':{'window':10},'adapter':None,
                                 'lease':None,'bootstrap':None,'snapshot':None}
        first = gui.request('scan', {'source':'native','limit':2})['data']
        second = gui.request('scan', {'source':'native','limit':2,'cursor':first['cursor']})['data']
        assert len(calls) == 1 and first['elements'][0]['ref'] != second['elements'][0]['ref']
        gui.request('scan', {'source':'native'})
        assert gui.request('scan', {'source':'native','cursor':first['cursor']})['error']['code'] == 'stale_cursor'


class PagedAdapter(Adapter):
    def __init__(self):
        super().__init__()
        self.epoch = 0
        self.rows = [
            {'ref':'root','parent':None,'name':'Panel','role':'Pane','actions':['get']},
            {'ref':'button','parent':'root','name':'Press','role':'Button','actions':['get','invoke'],'enabled':True},
            {'ref':'edit','parent':'root','name':'Entry','role':'Edit','actions':['get','set'],'enabled':True,'rect':{}},
            {'ref':'disabled','parent':None,'name':'Press','role':'Button','actions':['invoke'],'enabled':False}]
    def request(self, request):
        if request['op'] != 'scan': return super().request(request)
        self.calls.append(request)
        cursor = request.get('cursor')
        if not cursor:
            self.epoch += 1
            offset = 0
        else:
            epoch, offset = map(int, cursor.split('/'))
            if epoch != self.epoch: return {'error':'stale_cursor'}
        limit = request.get('limit', 300)
        end = offset + limit
        return {'elements':self.rows[offset:end], 'snapshot':str(self.epoch),
                'cursor':f'{self.epoch}/{end}' if end < len(self.rows) else None,'truncated':False}


def paged(gui):
    gui.sources['a']['adapter'] = adapter = PagedAdapter()
    return adapter


def test_find_completes_partial_pages_and_reuses_the_index(gui):
    adapter = paged(gui)
    assert gui.request('scan', {'source':'a','limit':1})['ok']
    data = gui.request('find', {'source':'a','role':'edit','method':'set_text'})['data']
    assert data['matched'] == 1 and data['nodes_indexed'] == 4 and not data['cached']
    assert data['elements'][0]['rect']=={} and data['elements'][0]['fingerprint']
    assert adapter.calls[-1]['limit'] == 300
    before = len(adapter.calls)
    data = gui.request('find', {'source':'a','query':'PRESS','enabled':True})['data']
    assert data['cached'] and data['elements'][0]['ref'] == 'a/button'
    assert len(adapter.calls) == before


def test_writes_invalidate_discovery_but_reads_keep_the_cached_index(gui):
    adapter = paged(gui)
    button = gui.request('find', {'source':'a','name':'Press','enabled':True})['data']['elements'][0]['ref']
    gui.request('call', {'ref':button,'method':'get'})
    assert gui.request('find', {'source':'a'})['data']['cached']
    gui.request('call', {'ref':button,'method':'invoke'})
    adapter.rows = [r for r in adapter.rows if r['ref'] != 'button']
    result = gui.request('find', {'source':'a','name':'Press'})['data']
    assert not result['cached'] and result['matched'] == 1
    assert result['elements'][0]['ref']=='a/disabled'


def test_invalid_write_does_not_invalidate_a_read_only_index(gui):
    paged(gui)
    ref = gui.request('find', {'source':'a','role':'Edit'})['data']['elements'][0]['ref']
    assert not gui.request('call', {'ref':ref,'method':'set','arguments':{}})['ok']
    assert gui.request('find', {'source':'a'})['data']['cached']


def test_native_input_routes_bound_guards_and_rejects_foreign_sources():
    calls=[]
    def request(op,args):
        calls.append((op,args))
        if op=='inspect': return {'ok':True,'data':{'elements':[{'ref':'edit','parent':None,'name':'Entry','role':'Edit','backend':'msaa','actions':['get','set','focus'],'rect':{'width':100,'height':20}}]}}
        return {'ok':True,'data':{'status':'executed_unverified'}}
    with GuiSession(desktop_factory=lambda:SimpleNamespace(request=request,close=lambda:None)) as s:
        for key in ('a','b'): s.sources[key]={'id':key,'runtime':'native','target':{'window':10},'adapter':None,'lease':None,'bootstrap':None,'snapshot':None}
        ref=s.request('find',{'source':'a'})['data']['elements'][0]['ref']
        foreign=s.request('find',{'source':'b'})['data']['elements'][0]['ref']
        result=s.request('call',{'ref':ref,'method':'type_text','arguments':{'text':'probe','guard_refs':[foreign]}})
        assert result['error']['code']=='invalid_target'
        assert not any(op=='input' for op,args in calls)
        result=s.request('call',{'ref':ref,'method':'type_text','arguments':{'text':'probe','guard_refs':[ref]}})
        assert result['ok'] and calls[-1]==('input',{'ref':'edit','method':'type_text','arguments':{'text':'probe'},'guard_refs':['edit']})


def test_delayed_native_set_publication_is_read_back_without_repeating_the_setter():
    calls=[]
    def request(op,args):
        calls.append(op)
        if op=='inspect': return {'ok':True,'data':{'elements':[{'ref':'edit','parent':None,'role':'Edit','actions':['get','set']}]}}
        if op=='set': return {'ok':False,'data':{'status':'verification_failed','value':'old'},'error':{'code':'verification_failed','message':'publication pending'}}
        return {'ok':True,'data':{'value':'new','source':'IAccessible.accValue'}}
    with GuiSession(desktop_factory=lambda:SimpleNamespace(request=request,close=lambda:None)) as s:
        s.sources['a']={'id':'a','runtime':'native','target':{'window':10},'adapter':None,'lease':None,'bootstrap':None,'snapshot':None}
        ref=s.request('find',{'source':'a'})['data']['elements'][0]['ref']
        result=s.request('call',{'ref':ref,'method':'set','arguments':{'value':'new'}})
        assert result['ok'] and result['data']['status']=='verified'
        assert calls.count('set')==1 and calls.count('get')==1


def test_find_refresh_does_not_retain_removed_controls_or_overstate_partial_coverage(gui):
    adapter = paged(gui)
    assert gui.request('find', {'source':'a','name':'Press'})['data']['matched'] == 2
    adapter.rows = [r for r in adapter.rows if r['ref'] != 'button']
    data = gui.request('find', {'source':'a','name':'Press','refresh':True})['data']
    assert not data['cached'] and data['matched'] == 1
    assert data['elements'][0]['ref'] == 'a/disabled'
    gui.sources['a']['collection']['metadata']['truncated'] = True
    assert gui.request('find', {'source':'a'})['data']['collection_truncated']


def test_find_selectors_scope_and_filtered_pagination(gui):
    paged(gui)
    gui.request('find', {'source':'a'})
    data = gui.request('find', {'source':'a','within_ref':'a/root','name':'re','contains':True})['data']
    assert [r['ref'] for r in data['elements']] == ['a/button']
    data = gui.request('find', {'source':'a','method':'click','limit':1})['data']
    assert data['matched'] == 2 and data['has_more']
    data = gui.request('find', {'source':'a','method':'click','limit':1,'offset':1})['data']
    assert data['elements'][0]['ref'] == 'a/disabled' and not data['has_more']
    assert gui.request('find', {'source':'a','within_ref':'foreign/root'})['error']['code'] == 'stale_ref'


def test_call_accepts_friendly_names_without_replaying_actions(gui):
    adapter = paged(gui)
    gui.request('find', {'source':'a'})
    before = len(adapter.calls)
    assert gui.request('call', {'ref':'a/button','method':'click'})['ok']
    assert len(adapter.calls) == before+1 and adapter.calls[-1]['method'] == 'invoke'
    assert gui.request('call', {'ref':'a/edit','method':'set_text','arguments':{'text':'literal'}})['ok']
    assert adapter.calls[-1]['arguments'] == {'value':'literal'}
    assert gui.request('describe', {'ref':'a/button'})['data']['aliases']['click'] == 'invoke'


@pytest.mark.parametrize('op,args', [(None,{}), ([],{}), ('schema',[]), ('schema',False), ('schema','text')])
def test_invalid_envelopes_do_not_start_backends(op, args):
    with GuiSession(desktop_factory=lambda:pytest.fail('No worker should start')) as gui:
        assert gui.request(op, args)['error']['code'] == 'invalid_arguments'


def test_find_invalid_selectors_do_not_even_scan(gui):
    adapter = paged(gui)
    for args in ({'enabled':'yes'}, {'method':'unknown'}, {'limit':True}, {'name':[]}):
        assert gui.request('find', {'source':'a',**args})['error']['code'] == 'invalid_arguments'
    assert not adapter.calls


def test_cli_request_ids_validation_and_idempotent_cleanup():
    gui = GuiSession(desktop_factory=lambda:pytest.fail('No worker should start'))
    result = dispatch_line(gui, '{"id":0,"op":"close","args":[]}')
    assert result['id'] == 0 and result['error']['code'] == 'invalid_request' and not gui.closed
    assert dispatch_line(gui, '[]')['error']['code'] == 'invalid_request'
    assert dispatch_line(gui, 'not json')['error']['code'] == 'invalid_json'
    assert dispatch_line(gui, '{"id":"still-open","op":"schema"}')['ok']
    first = gui.request('close')
    assert first['ok'] and gui.request('close') == first and gui.close() == []
    assert gui.request('schema')['error']['code'] == 'session_closed'
