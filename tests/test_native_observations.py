import json
import struct

import pytest

from bridges.observation_agent import ObservationAdapter
from native_metadata import Asar
from object_bridge import Fault, ObjectHost
from object_explorer import explore


def observed_host():
    source = {
        'pid': 123,
        'objects': [
            {'id': 'a', 'type': 'Service', 'children': ['b'], 'parent': None},
            {'id': 'b', 'type': 'Child', 'children': [], 'parent': 'a'},
        ],
        'classes': [
            {'type': 'Service', 'base_type': None,
             'properties': [{'name': 'text', 'type': 'QString', 'native_readable': True}],
             'methods': [{'name': 'send', 'signature': 'send(QString)', 'parameters': ['QString'], 'returns': 'void'}]},
            {'type': 'Child', 'base_type': 'Service', 'properties': [], 'methods': []},
        ],
    }
    adapter = ObservationAdapter(source, 'test-observation', 'qt')
    host = ObjectHost({'service': adapter.objects['a']}, adapter)
    adapter.host = host
    return host


class LocalSession:
    def __init__(self, host):
        self.host = host
        self.operations = []

    def request(self, op, args):
        self.operations.append(op)
        return {'ok': True, 'data': self.host.request(op, args)}


def test_native_observation_relationships_traverse_cycles_without_getters_or_calls():
    session = LocalSession(observed_host())
    data = explore(session, target='anything', query='send')['data']
    assert len(data['nodes']) == 2 and len(data['edges']) == 2
    assert session.operations == ['list', 'describe', 'describe']
    assert all(e['evidence'] == 'metadata_relationship' for e in data['edges'])
    assert len(data['matches']) == 2
    assert all(n['observation_only'] and n['source_pid'] == 123 for n in data['nodes'])
    assert not data['business_semantics_verified']


def test_observed_native_members_cannot_be_invoked_written_or_subscribed():
    host = observed_host()
    ref = host.request('list', {})['objects'][0]['ref']
    desc = host.request('describe', {'ref': ref})
    assert desc['properties'][0]['readable'] is False
    assert desc['methods'][0]['callable'] is False
    with pytest.raises(Fault) as error:
        host.request('call', {'ref': ref, 'member': 'send(QString)', 'arguments': [{'kind': 'value', 'value': 'text'}]})
    assert error.value.code == 'observation_only'
    with pytest.raises(Fault):
        host.request('get', {'ref': ref, 'member': 'text'})
    with pytest.raises(Fault):
        host.request('set', {'ref': ref, 'member': 'text', 'value': {'kind': 'value', 'value': 'text'}})


def test_native_capture_limits_remain_visible_after_unified_exploration():
    host = observed_host()
    host.adapter.source['stopped_by'] = ['object_budget']
    data = explore(LocalSession(host), target='x')['data']
    assert data['observation_only'] is True
    assert data['status'] == 'partial' and 'source_object_budget' in data['stopped_by']


@pytest.mark.parametrize('limit,reason', [({'max_depth': 0}, 'max_depth'), ({'max_objects': 1}, 'max_objects')])
def test_metadata_relationships_respect_depth_and_object_budgets(limit, reason):
    data = explore(LocalSession(observed_host()), target='x', **limit)['data']
    assert len(data['nodes']) == 1 and reason in data['stopped_by']
    assert data['statistics']['requests'] == 2


def test_asar_reader_checks_bounds_and_does_not_extract_files(tmp_path):
    content = b'program code'
    metadata = json.dumps({'files': {'code.js': {'size': len(content), 'offset': '0'}}}).encode()
    padded = metadata + b'\0' * ((4 - len(metadata) % 4) % 4)
    header_size = len(padded) + 8
    path = tmp_path / 'program.asar'
    path.write_bytes(struct.pack('<IIII', 4, header_size, len(padded) + 4, len(metadata)) + padded + content)
    archive = Asar(path)
    assert archive.read('code.js') == content
    archive.entries['code.js']['offset'] = '999999'
    with pytest.raises(ValueError, match='outside'):
        archive.read('code.js')
    assert {p.name for p in tmp_path.iterdir()} == {'program.asar'}
