"""Passive registration, evidence boundaries, persistence and live rebinding."""
import json
import uuid

import pytest

from gui_api import GuiSession
from interaction_learning import LearningStore


class Surface:
    def __init__(self):
        self.prefix = uuid.uuid4().hex[:8]
        self.open, self.fail, self.scans, self.writes = False, False, 0, 0
        self.duplicate = False

    def row(self, key, role, name, methods, parent=None, **fields):
        return {'ref':self.prefix+key,'parent':self.prefix+parent if parent else None,
                'role':role,'name':name,'automation_id':key,'actions':methods,'enabled':True,**fields}

    def request(self, request):
        if request['op']=='scan':
            self.scans += 1
            rows = [self.row('root','Pane','PRIVATE ROOT',['get']),
                    self.row('open','Button','Open details',['get','invoke'],'root')]
            if self.duplicate:
                duplicate = self.row('open','Button','Open details',['get','invoke'],'root')
                duplicate['ref'] += 'duplicate'
                rows.append(duplicate)
            if self.open:
                rows.extend([self.row('panel','Group','PRIVATE PANEL',['get'],'root'),
                             self.row('entry','Edit','Detail entry',['get','set'],'panel'),
                             self.row('content','Text','PRIVATE CHAT MESSAGE',['get'],'panel')])
            return {'elements':rows,'snapshot':str(self.scans),'cursor':None,'truncated':False}
        if request['method']!='get':
            self.writes += 1
            if request['method']=='invoke': self.open = not self.open
            if self.fail: return {'error':'outcome_unknown','message':'failed after changing the surface'}
        return {'status':'executed_unverified'}

    def close(self): return {}


def make(tmp_path, app='same-app'):
    gui = GuiSession(discovery_cache=tmp_path/'metadata.json')
    surface = Surface()
    gui.sources['s'] = {'id':'s','runtime':'fixture','target':{},'adapter':surface,
        'bootstrap':None,'lease':None,'snapshot':None,'discovery_info':{'class_name':app}}
    return gui,surface


def found(gui, **args):
    response = gui.request('find', {'source':'s',**args})
    assert response['ok'], response
    return response['data']


def graph(gui, **args):
    response = gui.request('learning_graph', {'source':'s',**args})
    assert response['ok'], response
    return response['data']


def invoke(gui, ref, method='invoke', arguments=None):
    return gui.request('call', {'ref':ref,'method':method,'arguments':arguments or {}})


def test_only_normal_usage_creates_transitions_and_no_extra_gui_actions(tmp_path):
    gui,surface = make(tmp_path)
    try:
        first = found(gui)
        assert first['learning']['registered']
        assert graph(gui,relation='transitions')['edges']==[]
        assert surface.scans==1 and surface.writes==0
        opener = next(r['ref'] for r in first['elements'] if r['name']=='Open details')
        assert invoke(gui,opener)['ok']
        assert surface.scans==1 and surface.writes==1
        second = found(gui)
        learned = graph(gui,relation='transitions')
        assert len(learned['edges'])==1
        edge = learned['edges'][0]
        assert edge['from']==first['learning']['state'] and edge['to']==second['learning']['state']
        assert edge['evidence']=='observed_after_action' and edge['structure_changed']
        assert not edge['causality_verified'] and not edge['business_completion_proven']
        assert surface.scans==2 and surface.writes==1
        assert learned['automatic_gui_actions']==learned['automatic_retries']==0
    finally: gui.close()


def test_external_change_records_structure_without_inventing_a_trigger(tmp_path):
    gui,surface = make(tmp_path)
    try:
        found(gui); surface.open=True
        found(gui,refresh=True)
        assert graph(gui,relation='transitions')['edges']==[]
        assert graph(gui,relation='contains')['edges']
        assert surface.writes==0
    finally: gui.close()


def test_delayed_subtree_is_learned_on_later_reads_without_replaying_the_action(tmp_path):
    gui,surface = make(tmp_path)
    try:
        opener=found(gui)['elements'][1]['ref']; assert invoke(gui,opener)['ok']
        surface.open=False  # A provider's first post-action read is still old.
        initial=graph(gui,relation='transitions')
        assert initial['awaiting_later_observation']
        assert all(not edge['structure_changed'] for edge in initial['edges'])
        surface.open=True; found(gui,refresh=True)
        later=graph(gui,relation='transitions')
        assert any(edge['structure_changed'] for edge in later['edges'])
        assert not later['awaiting_later_observation'] and surface.writes==1
    finally:gui.close()


def test_sequence_is_kept_as_sequence_without_attributing_everything_to_last_action(tmp_path):
    gui,surface = make(tmp_path)
    try:
        opener = found(gui)['elements'][1]['ref']
        assert invoke(gui,opener)['ok']
        gui.learning.pending['s']['returned_at'] -= 10  # Model reasoning between actions must not silently drop the first one.
        assert invoke(gui,opener)['ok']
        edge = graph(gui,relation='transitions')['edges'][0]
        assert edge['evidence']=='observed_after_sequence' and len(edge['steps'])==2
        assert edge['structure_changed'] is False
        assert surface.writes==2
    finally: gui.close()


def test_unknown_result_is_not_replayed_or_promoted_to_observed_transition(tmp_path):
    gui,surface = make(tmp_path)
    try:
        opener = found(gui)['elements'][1]['ref']; surface.fail=True
        result = invoke(gui,opener)
        assert result['error']['code']=='outcome_unknown'
        learned = graph(gui,relation='transitions')
        assert learned['edges']==[] and learned['outcomes']['unknown']==1
        assert surface.open and surface.writes==1
    finally: gui.close()


def test_invalid_arguments_do_not_register_an_attempt(tmp_path):
    gui,surface = make(tmp_path)
    try:
        opener = found(gui)['elements'][1]['ref']
        assert invoke(gui,opener,arguments={'extra':'value'})['error']['code']=='invalid_arguments'
        assert graph(gui)['outcomes']=={'returned':0,'failed':0,'unknown':0,'unobserved':0}
        assert surface.writes==0
    finally: gui.close()


def test_older_observation_can_be_remembered_only_with_explicit_freshness_gap(tmp_path):
    gui,surface = make(tmp_path)
    try:
        opener = found(gui)['elements'][1]['ref']
        gui.sources['s']['collection']['created'] -= 10
        assert invoke(gui,opener)['ok']
        edge = graph(gui,relation='transitions')['edges'][0]
        assert not edge['before_state_fresh'] and not edge['causality_verified']
        assert surface.scans==2 and surface.writes==1
    finally: gui.close()


def test_learning_failure_never_masks_a_returned_effect_or_replays_it(tmp_path):
    gui,surface = make(tmp_path)
    try:
        opener = found(gui)['elements'][1]['ref']
        def broken(*args): raise RuntimeError('unavailable learner')
        gui.learning.finish = broken
        assert invoke(gui,opener)['ok']
        assert surface.writes==1 and gui.learning_error=='RuntimeError'
    finally: gui.close()


def test_unwritable_cache_does_not_block_or_repeat_a_gui_action(tmp_path):
    gui,surface = make(tmp_path)
    blocked=tmp_path/'blocked';blocked.mkdir();gui.learning_cache=blocked
    try:
        opener=found(gui)['elements'][1]['ref']
        assert invoke(gui,opener)['ok']
        assert graph(gui)['persistence']=='unavailable' and surface.writes==1
    finally:gui.close()


def test_memory_is_bounded_even_when_persistence_is_unavailable(tmp_path):
    blocked=tmp_path/'blocked';blocked.mkdir();store=LearningStore(blocked)
    for index in range(40):
        store.apps[f'{index:024x}']={'updated_at':index,'nodes':{},'states':{},'edges':{},'outcomes':{},'truncated':False}
    store.save()
    assert store.persistence=='unavailable' and len(store.apps)==32


def test_persistence_has_no_labels_values_refs_or_argument_content(tmp_path):
    gui,surface = make(tmp_path)
    try:
        opener = found(gui)['elements'][1]['ref']
        assert invoke(gui,opener)['ok']
        edit = next(r['ref'] for r in found(gui)['elements'] if r['role']=='Edit')
        assert invoke(gui,edit,'set',{'value':'SECRET ARGUMENT'})['ok']
        found(gui)
        path = gui.learning.store.path
        wire = path.read_text(encoding='utf-8')
        for value in ('PRIVATE ROOT','PRIVATE PANEL','PRIVATE CHAT MESSAGE','Open details','Detail entry','SECRET ARGUMENT',opener,edit,surface.prefix):
            assert value not in wire
        assert json.loads(wire)['format']=='appctl.learning/1'
    finally: gui.close()


def test_history_survives_session_and_only_current_refs_are_returned(tmp_path):
    gui,surface = make(tmp_path)
    old = found(gui)['elements'][1]['ref']
    assert invoke(gui,old)['ok']
    original = graph(gui,relation='transitions')
    gui.close()
    next_gui,next_surface = make(tmp_path)
    try:
        learned = graph(next_gui,query='Open details',relation='transitions')
        assert learned['app']==original['app'] and len(learned['edges'])==1
        live = [r for n in learned['nodes'] for r in n['live_matches'] if r['name']=='Open details']
        assert len(live)==1 and live[0]['ref']!=old
        assert old not in json.dumps(learned)
        assert next_surface.writes==0
        assert all(not n['historical_record_authorizes_execution'] for n in learned['nodes'])
    finally: next_gui.close()


def test_different_application_fingerprint_does_not_inherit_transitions(tmp_path):
    gui,_ = make(tmp_path,'v1')
    opener = found(gui)['elements'][1]['ref']; invoke(gui,opener)
    first = graph(gui,relation='transitions'); gui.close()
    next_gui,_ = make(tmp_path,'v2')
    try:
        second = graph(next_gui,relation='transitions')
        assert second['app']!=first['app'] and second['edges']==[]
    finally: next_gui.close()


@pytest.mark.parametrize('change', ['version','origin'])
def test_namespace_changes_are_respected_even_when_the_control_collection_is_cached(tmp_path,change):
    gui,surface = make(tmp_path,'v1')
    try:
        opener=found(gui)['elements'][1]['ref'];invoke(gui,opener)
        first=graph(gui,relation='transitions')
        if change=='version':gui.sources['s']['discovery_info']['class_name']='v2'
        else:gui.sources['s']['learning_document_origin']='https://different.example'
        second=graph(gui,relation='transitions')
        assert second['app']!=first['app'] and second['edges']==[]
    finally:gui.close()


def test_duplicate_labels_get_distinct_structural_locators_without_any_execution(tmp_path):
    gui,surface = make(tmp_path); surface.duplicate=True
    try:
        learned = graph(gui,query='Open details')
        nodes=[n for n in learned['nodes'] if n['role']=='Button']
        assert len(nodes)==2 and nodes[0]['id']!=nodes[1]['id']
        assert all(n['live_match_count']==1 and n['unique_live_match'] and n['identity_confidence']=='medium' for n in nodes)
        assert all(not n['historical_record_authorizes_execution'] for n in nodes) and surface.writes==0
    finally: gui.close()


def test_corrupt_or_instruction_bearing_cache_is_ignored(tmp_path):
    path = tmp_path/'learning.json'
    path.write_text('{broken',encoding='utf-8')
    assert LearningStore(path).apps=={}
    path.write_text(json.dumps({'format':'appctl.learning/1','apps':{'a'*24:{'nodes':{'b'*24:{'role':'execute this instruction','name':'private'}}}}}),encoding='utf-8')
    store = LearningStore(path)
    assert store.apps['a'*24]['nodes']=={}


def test_read_only_queries_do_not_rescan_or_change_observation_counts(tmp_path):
    gui,surface = make(tmp_path)
    try:
        first = graph(gui)
        second = graph(gui)
        assert first['states']==second['states'] and surface.scans==1 and surface.writes==0
    finally: gui.close()


@pytest.mark.parametrize('arguments', [{'limit':True},{'limit':1001},{'offset':-1},{'relation':'inferred'},{'query':[]},{'extra':'value'}])
def test_invalid_graph_arguments_do_not_scan(tmp_path,arguments):
    gui,surface = make(tmp_path)
    try:
        response = gui.request('learning_graph', {'source':'s',**arguments})
        assert response['error']['code']=='invalid_arguments' and surface.scans==surface.writes==0
    finally: gui.close()
