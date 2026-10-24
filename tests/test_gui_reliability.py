"""Failure-sensitive contracts for 0.5 catalogs, postconditions and registration."""
import copy
import json
from types import SimpleNamespace

import pytest

from discovery import digest
from gui_api import GuiSession
from gui_identity import annotate,group_controls
from gui_input import NativeInput,chord_events
from object_bridge import Fault


def row(ref,role,name='',methods=('get',),parent=None,**extra):
    return dict(ref=ref,role=role,name=name,parent=parent,actions=list(methods),**{'enabled':True,'offscreen':False,**extra})


class Surface:
    def __init__(self):
        self.rows=[row('root','Pane'),row('one','Button','一',('get','invoke'),'root'),
                   row('plus','Button','加',('get','invoke'),'root'),row('edit','Edit','搜索',('get','set'),'root')]
        self.values={'edit':''}; self.writes=[]; self.scans=0; self.noop=False; self.label_only=False; self.truncated=False
        self.ancestor=False
    def request(self,request):
        if request['op']=='scan':
            self.scans+=1
            return {'elements':copy.deepcopy(self.rows),'snapshot':str(self.scans),'cursor':None,'truncated':self.truncated}
        method,ref=request['method'],request['ref']
        if method=='get': return {'value':self.values.get(ref,''),'label_only':self.label_only,'truncated':False}
        self.writes.append((ref,method))
        if not self.noop:
            if method=='set': self.values[ref]=request['arguments']['value']
            if ref=='one' and method=='invoke' and not any(r['ref']=='details' for r in self.rows):
                self.rows.append(row('details','Group','Details',('get',),'root'))
        return {'status':'executed_unverified',**({'action_target':{'kind':'ancestor','matches_requested':False}} if self.ancestor else {})}
    def close(self): return {}


def make(tmp_path,surface=None):
    gui=GuiSession(discovery_cache=tmp_path/'discovery.json')
    gui.sources['s']={'id':'s','runtime':'fixture','target':{},'adapter':surface or Surface(),
        'bootstrap':None,'lease':None,'snapshot':None,'discovery_info':{'class_name':'ReliabilityFixture'}}
    return gui,gui.sources['s']['adapter']


def data(response):
    assert response['ok'],response
    return response['data']


def discover(gui,**options): return data(gui.request('discover',{'source':'s',**options}))
def cap(catalog,name,method): return next(e['id'] for e in catalog['capabilities'] if e['name']==name and e['operation']==method)


def test_63_anonymous_siblings_have_distinct_structural_identity(tmp_path):
    gui,surface=make(tmp_path)
    surface.rows=[row('root','Window')]+[row(str(i),'Group',parent='root') for i in range(63)]
    try:
        found=data(gui.request('find',{'source':'s','role':'Group'}))
        assert len({r['fingerprint'] for r in found['elements']})==63
        assert all(r['same_name_role_count']==63 and r['identity_confidence']=='medium' for r in found['elements'])
        for current in found['elements']:
            result=data(gui.request('rebind',{'source':'s','node_id':current['fingerprint']}))
            assert result['status']=='resolved' and result['candidate_count']==1 and result['ref']==current['ref']
        assert not surface.writes
    finally: gui.close()


def test_grouped_catalog_keeps_all_original_method_bindings(tmp_path):
    gui,surface=make(tmp_path)
    rect={'x':10,'y':20,'width':200,'height':30}
    surface.rows=[row('a','Edit','Entry',('get','set'),backend='msaa',hwnd=33,rect=rect),
                  row('b','Edit','',('get','focus'),backend='win32',hwnd=33,rect=rect,class_name='RichEditD2DPT')]
    try:
        catalog=discover(gui)
        assert catalog['control_count']==catalog['capability_count']==1
        assert catalog['readable_control_count']==catalog['writable_control_count']==1
        assert catalog['operation_count']==4
        control=catalog['controls'][0]
        assert control['binding_count']==2 and control['methods']==['focus','get','set']
        assert control['rect']==rect and {r['ref'] for r in control['bindings']}=={'s/a','s/b'}
        assert data(gui.request('discovery_invoke',{'capability':control['id'],'method':'set','arguments':{'text':'literal'}}))
        assert surface.writes==[('a','set')]
    finally: gui.close()


def test_method_count_does_not_consume_4096_control_budget(tmp_path):
    gui,surface=make(tmp_path)
    surface.rows=[row(str(i),'Button',str(i),('get','invoke','focus','select','expand','collapse')) for i in range(710)]
    try:
        catalog=discover(gui,limit=1)
        assert catalog['control_count']==710 and catalog['operation_count']==4260
        assert not catalog['truncated'] and catalog['catalog_limits']['controls_per_catalog']==4096
    finally: gui.close()


def test_coincident_anonymous_containers_are_not_merged():
    rect={'x':0,'y':0,'width':100,'height':100}
    rows=[{**row('a','Group',backend='msaa',rect=rect)}, {**row('b','Group',backend='uia3',rect=rect)}]
    annotate(rows,77)
    assert len(group_controls(rows))==2


def test_deep_anonymous_branches_keep_ancestry_above_32_levels():
    rows=[row('root','Window')]
    for branch in ('a','b'):
        parent='root'
        for depth in range(45):
            ref=f'{branch}{depth}';rows.append(row(ref,'Group',parent=parent));parent=ref
    annotate(rows,77)
    assert len({r['fingerprint'] for r in rows})==len(rows)


def test_mutable_edit_label_preserves_registration_and_live_binding(tmp_path):
    gui,surface=make(tmp_path)
    try:
        first=data(gui.request('find',{'source':'s','role':'Edit'}))['elements'][0]
        surface.rows[-1]['name']=''
        second=data(gui.request('find',{'source':'s','refresh':True,'role':'Edit'}))['elements'][0]
        assert first['fingerprint']==second['fingerprint'] and first['ref']==second['ref']
        rebound=data(gui.request('rebind',{'source':'s','node_id':first['fingerprint'],'method':'set'}))
        assert rebound['ref']==second['ref'] and rebound['health']['resolution_success_rate']==1
        assert not rebound['historical_record_authorizes_execution']
    finally: gui.close()


def test_continuous_operations_reuse_catalog_ids_and_only_removed_controls_expire(tmp_path):
    gui,surface=make(tmp_path)
    try:
        first=discover(gui)
        one,plus=cap(first,'一','invoke'),cap(first,'加','invoke')
        assert gui.request('discovery_invoke',{'capability':one})['ok']
        assert gui.request('discovery_invoke',{'capability':plus})['ok']
        assert surface.writes==[('one','invoke'),('plus','invoke')]
        assert discover(gui)['catalog']==first['catalog']
        surface.rows=[r for r in surface.rows if r['ref']!='one']
        discover(gui,refresh=True)
        assert gui.request('discovery_invoke',{'capability':one})['error']['code']=='stale_capability'
        assert gui.request('discovery_invoke',{'capability':plus})['ok']
    finally: gui.close()


def test_exact_role_state_filters_and_relevance_resolve_chinese_noise(tmp_path):
    gui,surface=make(tmp_path)
    surface.rows += [row('noise','Button','上一行',('get','invoke')),row('hidden','Button','一',('get','invoke'),offscreen=True)]
    try:
        found=data(gui.request('find',{'source':'s','query':'一','role':'Button'}))
        assert found['elements'][0]['name']=='一'
        exact=data(gui.request('find',{'source':'s','query':'一','exact':True,'role':'Button','enabled':True,'onscreen':True}))
        assert exact['matched']==1 and exact['elements'][0]['ref']=='s/one'
        catalog=discover(gui,query='一',exact=True,role='Button',enabled=True,onscreen=True)
        assert catalog['matched_controls']==1
        absent=discover(gui,query='Uninstantiated page',exact=True)
        assert absent['match_status']=='not_observed' and absent['absence_proven'] is False
    finally: gui.close()


def test_explicit_gui_postcondition_observes_once_dispatched_action(tmp_path):
    gui,surface=make(tmp_path)
    try:
        catalog=discover(gui)
        result=gui.request('discovery_invoke',{'capability':cap(catalog,'一','invoke'),
            'postcondition':{'selector':{'name':'Details','role':'Group','exact':True},'field':'exists','equals':True,'timeout_ms':0}})
        assert result['ok'] and result['data']['status']=='postcondition_passed'
        assert result['data']['postcondition']['passed']
        assert surface.writes==[('one','invoke')]
        assert not result['data']['postcondition']['causality_verified']
        assert not result['data']['suggested_verification']['business_completion_proven']
    finally: gui.close()


@pytest.mark.parametrize('condition',[{'ref':'s/forged','equals':'x'}, {'selector':{},'field':'name','equals':'x'},
    {'ref':'s/one','field':'value','equals':float('nan')},{'ref':'s/one','field':'enabled','equals':'true'},
    {'ref':'s/one','field':'value','equals':'x','timeout_ms':6000}])
def test_invalid_postcondition_blocks_before_dispatch(tmp_path,condition):
    gui,surface=make(tmp_path)
    try:
        discover(gui)
        result=gui.request('call',{'ref':'s/one','method':'invoke','postcondition':condition})
        assert not result['ok'] and not surface.writes and result['next_action']
    finally: gui.close()


def test_failed_postcondition_quarantines_operation_without_replay(tmp_path):
    gui,surface=make(tmp_path); surface.noop=True
    try:
        catalog=discover(gui); operation=cap(catalog,'一','invoke')
        result=gui.request('discovery_invoke',{'capability':operation,'postcondition':{
            'selector':{'name':'Details'},'field':'exists','equals':True,'timeout_ms':120,'interval_ms':50}})
        assert result['error']['code']=='postcondition_failed'
        assert result['data']['postcondition']['read_attempts']>=2
        assert result['next_action']=='observe_actual_state_do_not_replay'
        blocked=gui.request('discovery_invoke',{'capability':operation})
        assert blocked['error']['code']=='stale_capability' and len(surface.writes)==1
        assert gui.request('discovery_invoke',{'capability':cap(catalog,'加','invoke')})['ok']
        assert surface.writes[-1]==('plus','invoke')
        # Revalidation caused by another control cannot re-arm the failed one.
        discover(gui)
        assert gui.request('discovery_invoke',{'capability':operation})['error']['code']=='stale_capability'
        assert len(surface.writes)==2
        refreshed=discover(gui,refresh=True)
        replacement=cap(refreshed,'一','invoke')
        assert replacement!=operation
        assert gui.request('discovery_invoke',{'capability':operation})['error']['code']=='stale_capability'
    finally: gui.close()


def test_label_only_is_not_value_postcondition_evidence(tmp_path):
    gui,surface=make(tmp_path); surface.label_only=True
    try:
        discover(gui)
        result=gui.request('call',{'ref':'s/edit','method':'set','arguments':{'text':'value'},
            'postcondition':{'ref':'s/edit','field':'value','equals':'value','timeout_ms':0}})
        assert result['error']['code']=='postcondition_failed'
        assert len(surface.writes)==1 and not result['data']['postcondition']['observable']
    finally: gui.close()


def test_reported_ancestor_recipient_is_failed_without_fallback(tmp_path):
    gui,surface=make(tmp_path); surface.ancestor=True
    try:
        discover(gui)
        result=gui.request('call',{'ref':'s/one','method':'invoke'})
        assert result['error']['code']=='invoke_target_mismatch'
        assert result['data']['invocation']['ancestor_bubbling']=='unknown'
        assert surface.writes==[('one','invoke')]
    finally: gui.close()


def test_legacy_default_action_is_blocked_without_postcondition_before_dispatch(tmp_path):
    gui,surface=make(tmp_path)
    surface.rows[1]['backend']='msaa'
    try:
        discover(gui)
        blocked=gui.request('call',{'ref':'s/one','method':'invoke'})
        assert blocked['error']['code']=='unreliable_invoke' and not surface.writes
        assert blocked['next_action']=='use_click_input_or_postcondition'
        result=gui.request('call',{'ref':'s/one','method':'invoke','postcondition':{
            'selector':{'name':'Details'},'field':'exists','equals':True,'timeout_ms':0}})
        assert result['ok'] and len(surface.writes)==1
        assert result['data']['invocation']['reliability']=='unreliable'
    finally: gui.close()


def test_same_tick_refresh_still_revalidates_metadata_and_old_label_action_cannot_execute(tmp_path,monkeypatch):
    gui,surface=make(tmp_path)
    import gui_api
    monkeypatch.setattr(gui_api.time,'monotonic',lambda:100.)
    try:
        catalog=discover(gui); old=cap(catalog,'一','invoke')
        surface.rows[1]['name']='Delete something'
        discover(gui,refresh=True)
        assert gui.request('discovery_invoke',{'capability':old})['error']['code']=='stale_capability'
        assert not surface.writes
    finally: gui.close()


def test_export_import_candidates_rebind_new_session_and_health_no_private_values(tmp_path):
    gui,surface=make(tmp_path/'first')
    try:
        found=data(gui.request('find',{'source':'s','role':'Edit'}))['elements'][0]
        exported=data(gui.request('learning_export',{'source':'s'}))
    finally: gui.close()
    gui,surface=make(tmp_path/'second')
    surface.rows[-1]['ref']='new-edit'
    try:
        imported=data(gui.request('learning_import',{'data':exported}))
        assert imported['imported_as']=='candidates_only' and not imported['historical_record_authorizes_execution']
        rebound=data(gui.request('rebind',{'source':'s','node_id':found['fingerprint']}))
        assert rebound['status']=='resolved' and rebound['ref']=='s/new-edit'
        assert rebound['health']['attempts']==1 and rebound['health']['last_success_at']>0
        wire=json.dumps(data(gui.request('learning_export',{'source':'s'})))
        assert '搜索' not in wire and 's/new-edit' not in wire and not surface.writes
    finally: gui.close()


def test_version_migration_is_explicit_same_family_candidate_without_execution_ref(tmp_path):
    image=tmp_path/'sample.exe';image.write_bytes(b'version-one')
    gui,surface=make(tmp_path/'old');gui.sources['s']['target']['exe']=str(image)
    try:
        original=data(gui.request('find',{'source':'s','role':'Edit'}))['elements'][0]
        exported=data(gui.request('learning_export',{'source':'s'}))
    finally: gui.close()
    image.write_bytes(b'version-two-with-new-length')
    gui,surface=make(tmp_path/'new');gui.sources['s']['target']['exe']=str(image)
    try:
        data(gui.request('learning_import',{'data':exported}))
        blocked=gui.request('rebind',{'source':'s','app':exported['app'],'node_id':original['fingerprint']})
        assert blocked['error']['code']=='stale_capability'
        candidate=data(gui.request('rebind',{'source':'s','app':exported['app'],'node_id':original['fingerprint'],'allow_migration':True}))
        assert candidate['status']=='migration_candidate' and candidate['candidate_count']==1 and candidate['ref'] is None
        assert not candidate['migration_verified'] and not surface.writes
    finally: gui.close()


def test_cross_version_ambiguous_structural_candidates_do_not_choose_one(tmp_path):
    image=tmp_path/'sample.exe';image.write_bytes(b'one')
    gui,surface=make(tmp_path/'old');gui.sources['s']['target']['exe']=str(image)
    surface.rows=[row('one','Edit',backend='msaa')]
    try:
        original=data(gui.request('find',{'source':'s'}))['elements'][0]
        exported=data(gui.request('learning_export',{'source':'s'}))
    finally: gui.close()
    image.write_bytes(b'new-version')
    gui,surface=make(tmp_path/'new');gui.sources['s']['target']['exe']=str(image)
    surface.rows=[row('a','Edit',backend='msaa'),row('b','Edit',backend='win32')]
    try:
        data(gui.request('learning_import',{'data':exported}))
        result=data(gui.request('rebind',{'source':'s','app':exported['app'],'node_id':original['fingerprint'],'allow_migration':True}))
        assert result['status']=='ambiguous' and result['candidate_count']==2 and result['ref'] is None
        assert all('fingerprint' in c and 'rect' in c for c in result['candidates'])
        assert not surface.writes
    finally: gui.close()


def test_ref_postcondition_beyond_first_result_page_is_not_mistaken_for_absence(tmp_path):
    gui,surface=make(tmp_path);surface.noop=True
    surface.rows=[row('one','Button','Action',('get','invoke'))]+[row(str(i),'Group') for i in range(3001)]+[row('late','Edit')]
    try:
        data(gui.request('find',{'source':'s','limit':1}))
        result=gui.request('call',{'ref':'s/one','method':'invoke','postcondition':{
            'ref':'s/late','field':'exists','equals':False,'timeout_ms':0}})
        assert result['error']['code']=='postcondition_failed',result
        assert result['data']['postcondition']['matches']==1 and len(surface.writes)==1
    finally: gui.close()


def test_partial_tree_cannot_verify_control_absence(tmp_path):
    gui,surface=make(tmp_path);surface.truncated=True;surface.noop=True
    try:
        discover(gui)
        result=gui.request('call',{'ref':'s/one','method':'invoke','postcondition':{
            'selector':{'name':'Unrendered feature'},'field':'exists','equals':False,'timeout_ms':0}})
        assert result['error']['code']=='postcondition_failed'
        assert result['data']['postcondition']['observable'] is False and len(surface.writes)==1
    finally: gui.close()


@pytest.mark.parametrize('key',['ESC','TAB','DELETE','BACKSPACE','F1','F12','LEFT','RIGHT','UP','DOWN','CTRL+SHIFT+F5'])
def test_named_keys_are_supported(key): assert chord_events(key)


def test_explicit_escape_does_not_need_editor_focus_or_hit_test_and_still_checks_foreground():
    packets=[]
    api=SimpleNamespace(foreground=lambda:10,modifiers_down=lambda:False,send=packets.append)
    driver=NativeInput(api)
    assert driver.window_escape(10,lambda:{'ok':True,'data':{'foreground':True}})['keys']=='ESC'
    assert len(packets)==1
    with pytest.raises(Fault): driver.window_escape(20,lambda:{'ok':True,'data':{'foreground':True}})
    assert len(packets)==1


def test_native_commit_sends_set_and_enter_once_and_marks_partial_failure(tmp_path):
    calls=[]
    def native(op,args):
        calls.append((op,args))
        if op=='inspect': return {'ok':True,'data':{'elements':[row('edit','Edit','Entry',('get','set','focus'),rect={'x':0,'y':0,'width':100,'height':20})]}}
        if op=='input': return {'ok':False,'error':{'code':'focus_unverified','message':'No focus'}}
        return {'ok':True,'data':{'status':'verified'}}
    gui=GuiSession(desktop_factory=lambda:SimpleNamespace(request=native,close=lambda:None),discovery_cache=tmp_path/'metadata.json')
    gui.sources['s']={'id':'s','runtime':'native','target':{'window':10},'adapter':None,'bootstrap':None,'lease':None,'snapshot':None}
    try:
        data(gui.request('find',{'source':'s'}))
        result=gui.request('call',{'ref':'s/edit','method':'set','arguments':{'text':'path','commit':'enter'}})
        assert result['error']['code']=='outcome_unknown' and result['data']['value_written']
        assert [op for op,args in calls].count('set')==[op for op,args in calls].count('input')==1
    finally: gui.close()


def test_successful_commit_registers_set_followed_by_enter_not_two_setters(tmp_path):
    calls=[]
    def native(op,args):
        calls.append((op,args))
        if op=='inspect': return {'ok':True,'data':{'elements':[row('edit','Edit','Entry',('get','set','focus'),rect={'x':0,'y':0,'width':100,'height':20})]}}
        return {'ok':True,'data':{'status':'verified'}}
    gui=GuiSession(desktop_factory=lambda:SimpleNamespace(request=native,close=lambda:None),discovery_cache=tmp_path/'metadata.json')
    gui.sources['s']={'id':'s','runtime':'native','target':{'window':10},'adapter':None,'bootstrap':None,'lease':None,'snapshot':None}
    try:
        data(gui.request('find',{'source':'s'}))
        result=gui.request('call',{'ref':'s/edit','method':'set','arguments':{'value':'entry','commit':'enter'}})
        assert result['ok'],result
        assert [step['method'] for step in gui.learning.pending['s']['steps']]==['set','press_keys']
        assert [op for op,args in calls].count('set')==1 and [op for op,args in calls].count('input')==1
    finally: gui.close()
