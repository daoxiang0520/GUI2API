"""Boundary tests for explicit runtime bootstrap; no personal app access."""
from contextlib import contextmanager
import ctypes
from pathlib import Path
from types import SimpleNamespace

import pytest

import qt_accessibility as qt
from messenger_api import UiaFields, TextField, ProcessLease
from object_bridge import Fault


@pytest.mark.parametrize('code_rva', [0x1000, 0x3000])
def test_gate_scan_uses_signed_rip_and_writable_data(monkeypatch, code_rva):
    import struct
    data_rva = 0x2000
    code = b'\x48\x85\xc9\x0f\x84\0\0\0\0\x80\x3d' + struct.pack('<i', data_rva - code_rva - 16) + b'\0\x0f\x84'
    pe = SimpleNamespace(pointer_size=8, data=code, sections=[
        {'name': '.text', 'rva': code_rva, 'raw': 0, 'size': len(code), 'virtual_size': len(code), 'characteristics': 0x20000000},
        {'name': '.data', 'rva': data_rva, 'raw': 100, 'size': 0, 'virtual_size': 32, 'characteristics': 0x80000000}])
    @contextmanager
    def image(_):
        yield pe
    monkeypatch.setattr(qt, 'pe_file', image)
    assert qt.gate_candidates('fixture')[0]['rva'] == data_rva
    pe.sections[1]['characteristics'] = 0
    assert qt.gate_candidates('fixture') == []


class Function:
    def __init__(self, fn): self.fn = fn
    def __call__(self, *args): return self.fn(*args)


@pytest.fixture
def fake_gate(monkeypatch):
    candidate = {'rva': qt.PROFILES['4.1.15.13'], 'instruction_rva': 0x1000,
                 'instruction_bytes': '4885c9', 'section': '.data'}
    state = SimpleNamespace(value=b'\0', writes=[], closed=0, opens=0, version='4.1.15.13', instruction=b'\x48\x85\xc9')
    class Memory:
        def __init__(self, _):
            self.modules = [{'name': 'Weixin.dll', 'path': str(Path('fixture') / state.version / 'Weixin.dll'), 'base': 0x10000000}]
            def write(_handle, _address, value, _size, written):
                state.value = ctypes.string_at(value, 1)
                state.writes.append(state.value)
                written._obj.value = 1
                return 1
            def open_process(*_):
                state.opens += 1
                return 123
            self.kernel = SimpleNamespace(OpenProcess=Function(open_process),
                WriteProcessMemory=Function(write), CloseHandle=Function(lambda _: 1))
        def read(self, address, _size):
            return state.instruction if address == 0x10001000 else state.value
        def close(self): state.closed += 1
    monkeypatch.setattr(qt, 'RemoteMemory', Memory)
    monkeypatch.setattr(qt, 'gate_candidates', lambda _: [candidate])
    monkeypatch.setattr(qt.psutil, 'Process', lambda _: SimpleNamespace(create_time=lambda: 123, exe=lambda: 'fixture.exe'))
    return state


def test_gate_restored_even_on_failed_control_action(fake_gate):
    with pytest.raises(RuntimeError, match='provider failed'):
        with qt.QtAccessibilityLease(1, 123) as lease:
            assert lease.enable()['active'] is True
            raise RuntimeError('provider failed')
    assert fake_gate.writes == [b'\1', b'\0']
    assert fake_gate.value == b'\0' and fake_gate.closed == 1


def test_gate_rejects_concurrent_change_before_mutation(fake_gate):
    with qt.QtAccessibilityLease(1, 123) as lease:
        fake_gate.value = b'\1'
        with pytest.raises(RuntimeError, match='concurrently'):
            lease.enable()
    assert fake_gate.writes == []


def test_already_active_gate_is_not_changed_or_disabled(fake_gate):
    fake_gate.value = b'\1'
    with qt.QtAccessibilityLease(1, 123) as lease:
        lease.enable()
    assert fake_gate.writes == [] and fake_gate.opens == 0


@pytest.mark.parametrize('change', ['version', 'instruction', 'value'])
def test_unsupported_image_or_data_is_rejected_before_open_write_handle(fake_gate, change):
    if change == 'version': fake_gate.version = 'unknown'
    elif change == 'instruction': fake_gate.instruction = b'bad'
    else: fake_gate.value = b'\x02'
    with pytest.raises(ValueError):
        qt.QtAccessibilityLease(1, 123)
    assert not fake_gate.writes and fake_gate.opens == 0 and fake_gate.closed == 1


def test_process_identity_is_checked_before_memory_access(fake_gate):
    with pytest.raises(ValueError, match='identity'):
        qt.QtAccessibilityLease(1, 321)
    assert fake_gate.opens == 0 and fake_gate.closed == 0


@pytest.fixture
def fields():
    class Desktop:
        value = ''
        writes = 0
        reads = 0
        label_only = False
        ambiguous = False
        lie = False
        activation_ok = True
        focus_changes_value = False
        def request(self, op, args):
            if op == 'window':
                return {'ok': True, 'data': {'status': 'verified' if self.activation_ok else 'executed_unverified'}}
            if op == 'focus':
                if self.focus_changes_value: self.value = 'concurrent edit'
                return {'ok': True, 'data': {'status': 'verified'}}
            if op == 'inspect':
                self.reads += 1
                rows = [{'ref': str(self.reads), 'class_name': 'TestSearch', 'name': 'Search',
                         'role': 'Edit', 'enabled': True, 'actions': ['get', 'set']}]
                return {'ok': True, 'data': {'elements': rows * (2 if self.ambiguous else 1)}}
            if op == 'get':
                return {'ok': True, 'data': {'value': self.value, 'source': 'UIA3.ValuePattern', 'label_only': self.label_only}}
            if op == 'set':
                self.writes += 1
                if not self.lie: self.value = args['value']
                return {'ok': True, 'data': {'status': 'verified'}}
            raise AssertionError(op)
    runtime = object.__new__(UiaFields)
    runtime.desktop = Desktop()
    runtime.target = {'window': 1, 'pid': 2}
    runtime.profile = {'slots': {'search': {'roles': ['Edit'], 'classes': ['TestSearch'], 'names': ['Search']}}}
    return runtime


def test_field_compare_and_set_prevents_overwriting_user_edit(fields):
    fields.desktop.value = 'user draft'
    with pytest.raises(Fault) as err:
        fields.replace('search', 'test', '')
    assert err.value.code == 'value_conflict' and fields.desktop.writes == 0


def test_field_rebinds_for_fresh_readback(fields):
    result = fields.replace('search', 'test', '')
    assert result['value'] == 'test' and fields.desktop.reads == 2 and fields.desktop.writes == 1


def test_field_label_is_not_accepted_as_text(fields):
    fields.desktop.label_only = True
    with pytest.raises(Fault) as err:
        fields.replace('search', 'test', '')
    assert err.value.code == 'text_read_unavailable' and fields.desktop.writes == 0


def test_ambiguous_field_is_not_written(fields):
    fields.desktop.ambiguous = True
    with pytest.raises(Fault) as err:
        fields.replace('search', 'test', '')
    assert err.value.code == 'ambiguous_field' and fields.desktop.writes == 0


def test_failed_postcondition_is_not_retried(fields):
    fields.desktop.lie = True
    with pytest.raises(Fault) as err:
        fields.replace('search', 'test', '')
    assert err.value.code == 'postcondition_failed' and fields.desktop.writes == 1


def test_failed_activation_stops_before_text_write(fields):
    fields.profile['slots']['search']['focus_before_replace'] = True
    fields.desktop.activation_ok = False
    with pytest.raises(Fault) as err:
        fields.replace('search', 'test', '')
    assert err.value.code == 'window_activation_unverified' and fields.desktop.writes == 0


def test_focus_change_rechecks_expected_value_before_text_write(fields):
    fields.profile['slots']['search']['focus_before_replace'] = True
    fields.desktop.focus_changes_value = True
    with pytest.raises(Fault) as err:
        fields.replace('search', 'test', '')
    assert err.value.code == 'value_conflict' and fields.desktop.writes == 0


def test_single_line_contract_checked_before_runtime_call():
    session = SimpleNamespace(_profile={'slots': {'search': {}}}, _request=lambda *_: pytest.fail('Runtime must not be called'))
    field = TextField(session, 'search')
    with pytest.raises(Fault): field.replace('one\ntwo', '')


def test_process_lease_blocks_other_session_and_releases(tmp_path):
    lease = ProcessLease(123, 456, tmp_path)
    try:
        with pytest.raises(Fault) as err:
            ProcessLease(123, 456, tmp_path)
        assert err.value.code == 'application_busy'
        other = ProcessLease(124, 456, tmp_path)
        other.close()
    finally:
        lease.close()
    replacement = ProcessLease(123, 456, tmp_path)
    replacement.close()


def test_object_contract_records_binding_digest_and_hides_cleanup():
    from messenger_api import MessengerSession
    from bridges.messenger_agent import MessengerAdapter
    session = object.__new__(MessengerSession)
    session._profile_hash = 'profile-one'
    session._profile = {'slots': {'search': {}, 'draft': {'write_enabled': False}}}
    session.search = TextField(session, 'search')
    session.draft = TextField(session, 'draft')
    adapter = MessengerAdapter()
    first = adapter.describe(session)
    assert [m['name'] for m in first['methods']] == ['status']
    assert all(not p['writable'] for p in first['properties'])
    session._profile_hash = 'profile-two'
    second = adapter.describe(session)
    assert first['properties'][0]['binding_profile_digest'] != second['properties'][0]['binding_profile_digest']
    assert adapter.describe(session.search)['methods'][0]['binding_profile_digest'] == 'profile-two'
    assert next(m for m in adapter.describe(session.draft)['methods'] if m['name'] == 'replace')['callable'] is False


def test_electron_rich_drafts_are_rejected_before_editor_mutation():
    import json
    import subprocess
    import sys
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(['node', str(root / 'tests/fixtures/check_electron_fields.cjs'),
                             str(root / 'bridges/electron_fields.js')], capture_output=True,
                            encoding='utf-8', timeout=8, check=True)
    proof = json.loads(result.stdout)
    assert proof['protected_cases'] == 3 and proof['editable_surface_mutations'] == 0
