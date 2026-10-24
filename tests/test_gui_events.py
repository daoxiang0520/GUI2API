"""Event routing, stale-catalog rejection, and conservative cache boundaries."""
import pytest

from gui_api import GuiSession
from gui_events import WindowEvents


class EventAPI:
    def __init__(self):
        self.emit = None

    def run(self, emit, ready, stopping):
        self.emit = emit
        ready.set()
        stopping.wait()

    def identity(self, window):
        return (10, 100) if window in (10, 11) else (20, 200)

    def stop(self):
        pass


def test_one_hook_routes_multiple_sources_and_independent_cursors_without_values():
    api = EventAPI()
    events = WindowEvents(lambda:api)
    try:
        for source, window, pid in (('a',10,100), ('same',10,100), ('b',20,200), ('wrong-owner',10,999)):
            assert events.subscribe(source, window, pid)['watching']
        api.emit(0x800C, 11, -4)
        api.emit(0x800B, 11, -8)  # Caret blinking is not a control-tree mutation.
        first = events.read('a', after=0)
        assert first['revision'] == 1 and first['changed']
        assert events.read('a', after=1)['changed'] is False
        assert events.read('same', after=0)['changed']
        assert not events.read('b', after=0)['changed']
        assert not events.read('wrong-owner', after=0)['changed']
        assert 'window' not in first and first['contains_values'] is False
        events.unsubscribe('a')
        api.emit(0x8004, 11, -4)
        assert not events.read('a')['watching']
        assert events.read('same')['revision'] == 2
    finally:
        assert events.close()['closed']
        assert not events.thread.is_alive()


def test_failed_hook_reports_unavailable_and_closes_without_retrying():
    class Failed(EventAPI):
        def run(self, *args):
            raise OSError('no hook')
    events = WindowEvents(Failed)
    state = events.subscribe('a', 10, 100)
    assert not state['watching'] and state['reason'] == 'OSError'
    original = events.thread
    assert not events.subscribe('b', 20, 200)['watching']
    assert events.thread is original
    assert events.close()['closed']


class Surface:
    def __init__(self):
        self.name, self.scans, self.writes = 'Old button', 0, 0
        self.on_scan = lambda:None

    def request(self, request):
        if request['op'] == 'scan':
            self.scans += 1
            self.on_scan()
            return {'elements':[{'ref':'button', 'name':self.name, 'role':'Button', 'actions':['get','invoke']}],
                    'snapshot':str(self.scans), 'cursor':None, 'truncated':False}
        self.writes += request.get('method') != 'get'
        return {'status':'executed_unverified'}

    def close(self):
        return {}


@pytest.fixture
def gui(tmp_path):
    session = GuiSession(discovery_cache=tmp_path/'cache.json')
    surface = Surface()
    session.sources['a'] = {'id':'a', 'runtime':'fixture', 'target':{}, 'adapter':surface,
                            'bootstrap':None, 'lease':None, 'snapshot':None}
    state = {'revision':0, 'watching':True}
    session._event_state = lambda source, after=None, timeout_ms=0: {
        **state, 'provider':'fixture-events', 'changed':after is not None and after != state['revision']}
    try:
        yield session, surface, state
    finally:
        session.close()


def find(session):
    result = session.request('find', {'source':'a'})
    assert result['ok'], result
    return result['data']


def capability(session, found):
    record = session.discovery.records[found['discovery']['catalog']]
    return next(entry['id'] for entry in record['entries'] if entry['operation']=='invoke')


def test_external_change_rejects_old_catalog_before_effect_and_rebuilds_on_find(gui):
    session, surface, state = gui
    first = find(session)
    cap = capability(session, first)
    assert find(session)['cached']
    surface.name = 'New button'
    state['revision'] += 1
    result = session.request('discovery_invoke', {'capability':cap})
    assert result['error']['code'] == 'stale_capability'
    assert surface.writes == 0
    current = find(session)
    # The blocked invoke already refreshed the metadata, without dispatching.
    assert current['cached'] and current['elements'][0]['name'] == 'New button'
    assert current['discovery']['trigger'] == 'gui_event'
    assert surface.scans == 2


def test_events_are_coalesced_and_consumption_does_not_validate_an_old_index(gui):
    session, surface, state = gui
    first = find(session)
    catalog = first['discovery']['catalog']
    state['revision'] += 50
    args = {'catalog':catalog}
    event = session.request('discovery_events', args)['data']
    assert len(event['events']) == 1 and event['needs_revalidation'] and not event['invalidated']
    assert event['contains_values'] is False
    assert session.request('discovery_events', args)['data']['events'] == []
    assert not find(session)['cached'] and surface.scans == 2


def test_unchanged_control_catalog_survives_event_and_cursor_starts_at_observation(gui):
    session, surface, state = gui
    old = find(session)['discovery']['catalog']
    state['revision'] += 1
    new = find(session)['discovery']['catalog']
    assert new == old
    assert session.request('discovery_events', {'catalog':new})['data']['events'] == []


def test_observer_failure_invalidates_cache_and_enables_short_age_limit(gui):
    session, surface, state = gui
    first = find(session)
    assert first['freshness']['max_cache_age_ms'] == 30000
    state['watching'] = False
    new = find(session)
    assert not new['cached'] and new['discovery']['trigger'] == 'event_observer_lost'
    assert new['freshness']['max_cache_age_ms'] == 5000


def test_change_during_scan_is_not_recorded_as_a_fresh_snapshot(gui):
    session, surface, state = gui
    surface.on_scan = lambda:state.update(revision=state['revision']+1)
    find(session)
    surface.on_scan = lambda:None
    assert not find(session)['cached']
    assert surface.scans == 2


@pytest.mark.parametrize('watching,age', [(False,5.1), (True,30.1)])
def test_event_silence_never_extends_cache_for_ten_minutes(gui, watching, age):
    session, surface, state = gui
    state['watching'] = watching
    find(session)
    session.sources['a']['collection']['created'] -= age
    surface.name = 'Changed without an event'
    assert find(session)['elements'][0]['name'] == surface.name
    assert surface.scans == 2


@pytest.mark.parametrize('values', [{'timeout_ms':True}, {'timeout_ms':5001}, {'limit':0}, {'limit':True}])
def test_invalid_event_wait_does_not_reach_the_monitor(gui, values):
    session, surface, state = gui
    catalog = find(session)['discovery']['catalog']
    result = session.request('discovery_events', {'catalog':catalog, **values})
    assert result['error']['code'] == 'invalid_arguments'
    assert surface.scans == 1
