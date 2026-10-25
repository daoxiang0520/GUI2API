"""Cache reuse must keep live target checks, bounded age and no action replay."""
import copy

import pytest

from test_gui_reliability import Surface,make,data,discover,cap


class BoundSurface(Surface):
    supports_bound_observation=True
    def __init__(self):
        super().__init__()
        self.observations=0
        self.reads=0
    def request(self,request):
        if request['op']=='observe':
            self.observations+=1
            rows=[row for row in self.rows if row['ref']==request['ref']]
            if len(rows)!=1:return {'error':'stale_ref'}
            return {'element':copy.deepcopy(rows[0]),'fresh':True,'scope':'bound_control'}
        if request.get('method')=='get':self.reads+=1
        return super().request(request)


def session(tmp_path):
    gui,surface=make(tmp_path,BoundSurface())
    state={'revision':0,'structure_revision':0,'watching':True}
    gui._event_state=lambda source,after=None,timeout_ms=0:{**state,'changed':after is not None and state['revision']!=after}
    return gui,surface,state


def test_rebind_reuses_structure_but_reads_the_exact_object_each_time(tmp_path):
    gui,surface,state=session(tmp_path)
    try:
        entry=data(gui.request('find',{'source':'s','role':'Edit'}))['elements'][0]
        for _ in range(4):
            result=data(gui.request('rebind',{'source':'s','node_id':entry['fingerprint'],'method':'set'}))
            assert result['status']=='resolved' and result['cache_hit']
            assert result['resolution_observation']=='bound_control'
            assert result['uniqueness_scope']=='cached_topology_and_live_candidates'
            assert 'same_name_role_count' not in result['candidates'][0]
            assert not result['historical_record_authorizes_execution']
        assert surface.scans==1 and surface.observations==4 and not surface.writes
    finally:gui.close()


def test_ref_value_postcondition_uses_fresh_value_without_rescanning(tmp_path):
    gui,surface,state=session(tmp_path)
    try:
        discover(gui)
        result=gui.request('call',{'ref':'s/edit','method':'set','arguments':{'value':'new'},
            'postcondition':{'ref':'s/edit','field':'value','equals':'new','timeout_ms':0}})
        assert result['ok'] and result['data']['postcondition']['read_scope']=='bound_control'
        assert result['data']['postcondition']['cached_value_used'] is False
        assert surface.scans==1 and surface.observations==0 and surface.reads==1
        assert surface.writes==[('edit','set')]
        surface.noop=True
        result=gui.request('call',{'ref':'s/edit','method':'set','arguments':{'value':'different'},
            'postcondition':{'ref':'s/edit','field':'value','equals':'different','timeout_ms':0}})
        assert result['error']['code']=='postcondition_failed'
        assert surface.reads==2 and len(surface.writes)==2 and surface.scans==1
    finally:gui.close()


@pytest.mark.parametrize('field,equals',[('enabled',True),('offscreen',False),('name','After')])
def test_dynamic_postcondition_fields_are_not_read_from_metadata_cache(tmp_path,field,equals):
    gui,surface,state=session(tmp_path)
    try:
        discover(gui)
        surface.rows[-1][field]=equals
        verification=gui._verify_postcondition(gui.sources['s'],{'ref':'s/edit','field':field,'equals':equals,'timeout_ms':0,'interval_ms':100})
        assert verification['passed'] and verification['read_scope']=='bound_control'
        assert surface.scans==1 and not surface.writes
    finally:gui.close()


def test_catalog_sequence_reuses_only_live_bindings_after_property_events(tmp_path):
    gui,surface,state=session(tmp_path)
    try:
        catalog=discover(gui);operation=cap(catalog,'加','invoke')
        for _ in range(4):
            state['revision']+=1
            assert gui.request('discovery_invoke',{'capability':operation})['ok']
        assert surface.scans==1 and surface.observations==4
        assert surface.writes==[('plus','invoke')]*4
        # A full metadata search is still dirty; exact-object checks did not
        # pretend that unrelated controls or whole-window state were refreshed.
        assert not data(gui.request('find',{'source':'s'}))['cached']
        assert surface.scans==2
    finally:gui.close()


@pytest.mark.parametrize('change',[{'name':'Delete'}, {'role':'MenuItem'}, {'actions':['get']}, {'password':True}])
def test_changed_target_is_reobserved_and_old_operation_cannot_execute(tmp_path,change):
    gui,surface,state=session(tmp_path)
    try:
        operation=cap(discover(gui),'加','invoke')
        surface.rows[2].update(change)
        result=gui.request('discovery_invoke',{'capability':operation})
        assert not result['ok'] and not surface.writes and surface.scans==2
    finally:gui.close()


@pytest.mark.parametrize('invalidate',['structure','observer_lost','age','missing'])
def test_cache_miss_refreshes_before_rebinding(tmp_path,invalidate):
    gui,surface,state=session(tmp_path)
    try:
        entry=data(gui.request('find',{'source':'s','role':'Edit'}))['elements'][0]
        if invalidate=='structure':state.update(revision=1,structure_revision=1)
        if invalidate=='observer_lost':state['watching']=False
        if invalidate=='age':gui.sources['s']['collection']['created']-=31
        if invalidate=='missing':surface.rows=surface.rows[:-1]
        result=data(gui.request('rebind',{'source':'s','node_id':entry['fingerprint']}))
        assert not result['cache_hit'] and surface.scans==2 and not surface.writes
        assert result['status']==('not_observed' if invalidate=='missing' else 'resolved')
    finally:gui.close()


def test_unsupported_observation_keeps_original_refresh_path(tmp_path):
    gui,surface=make(tmp_path)
    try:
        entry=data(gui.request('find',{'source':'s','role':'Edit'}))['elements'][0]
        result=data(gui.request('rebind',{'source':'s','node_id':entry['fingerprint']}))
        assert not result['cache_hit'] and surface.scans==2 and not surface.writes
    finally:gui.close()


def test_ambiguous_selector_and_negative_existence_still_use_full_observation(tmp_path):
    gui,surface,state=session(tmp_path)
    try:
        discover(gui)
        extra=copy.deepcopy(surface.rows[-1]);extra['ref']='another-edit';surface.rows.append(extra)
        result=gui.request('call',{'ref':'s/edit','method':'set','arguments':{'value':'new'},
            'postcondition':{'selector':{'role':'Edit'},'field':'value','equals':'new','timeout_ms':0}})
        assert result['error']['code']=='postcondition_failed' and result['data']['postcondition']['matches']==2
        assert result['data']['postcondition']['read_scope']=='window_snapshot'
        assert len(surface.writes)==1 and surface.scans==2
        result=gui._verify_postcondition(gui.sources['s'],{'ref':'s/plus','field':'exists','equals':True,'timeout_ms':0,'interval_ms':100})
        assert result['read_scope']=='window_snapshot' and surface.scans==3
    finally:gui.close()


def test_label_only_read_and_quarantine_survive_cache_hits(tmp_path):
    gui,surface,state=session(tmp_path)
    try:
        operation=cap(discover(gui),'加','invoke');surface.noop=True;surface.label_only=True
        result=gui.request('discovery_invoke',{'capability':operation,'postcondition':{
            'ref':'s/edit','field':'value','equals':'','timeout_ms':0}})
        assert result['error']['code']=='postcondition_failed' and not result['data']['postcondition']['observable']
        assert gui.request('discovery_invoke',{'capability':operation})['error']['code']=='stale_capability'
        assert surface.writes==[('plus','invoke')]
    finally:gui.close()


def test_structure_change_during_bound_observation_forces_refresh(tmp_path):
    gui,surface,state=session(tmp_path)
    try:
        entry=data(gui.request('find',{'source':'s','role':'Edit'}))['elements'][0]
        request=surface.request
        def change(request_args):
            result=request(request_args)
            if request_args['op']=='observe':state.update(revision=1,structure_revision=1)
            return result
        surface.request=change
        result=data(gui.request('rebind',{'source':'s','node_id':entry['fingerprint']}))
        assert not result['cache_hit'] and surface.scans==2 and not surface.writes
    finally:gui.close()


@pytest.mark.parametrize('read_result',[{}, {'value':None,'label_only':True},{'value':None,'truncated':True}])
def test_missing_label_only_or_truncated_values_never_satisfy_null_postcondition(tmp_path,read_result):
    gui,surface,state=session(tmp_path)
    try:
        discover(gui);request=surface.request
        surface.request=lambda args:read_result if args.get('method')=='get' else request(args)
        result=gui.request('call',{'ref':'s/edit','method':'set','arguments':{'value':'x'},
            'postcondition':{'ref':'s/edit','field':'value','equals':None,'timeout_ms':0}})
        assert result['error']['code']=='postcondition_failed' and not result['data']['postcondition']['observable']
        assert len(surface.writes)==1
    finally:gui.close()


def test_provider_reliability_downgrade_cannot_reuse_an_unguarded_invoke(tmp_path):
    gui,surface,state=session(tmp_path)
    try:
        operation=cap(discover(gui),'加','invoke')
        surface.rows[2]['invoke_reliability']='unreliable_legacy_default_action'
        blocked=gui.request('discovery_invoke',{'capability':operation})
        assert blocked['error']['code']=='unreliable_invoke' and not surface.writes
        assert surface.scans==2
    finally:gui.close()
