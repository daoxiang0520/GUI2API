import json
from pathlib import Path
import sys

import pytest

from object_bridge import Event, Fault, ObjectHost
from object_client import ObjectSession, StdioProvider

ROOT = Path(__file__).resolve().parents[1]


class Model:
    def __init__(self):
        self.count = 0
        self.changed = Event()
        self._gets = 0
        self._child = Child()

    @property
    def dangerous(self):
        self._gets += 1
        return self._gets

    def add(self, amount):
        self.count += amount
        self.changed.emit()
        return self.count

    def child(self):
        return self._child

    def echo(self, value):
        return value

    def accept(self, value):
        return value is self._child


class Child:
    def __init__(self):
        self.label = "child"


def test_metadata_and_lookup_do_not_execute_getters_or_private_members():
    obj = Model()
    host = ObjectHost({"model": obj})
    ref = host.request("list", {})["objects"][0]["ref"]
    description = host.request("describe", {"ref": ref})
    assert obj._gets == 0
    assert "dangerous" in {p["name"] for p in description["properties"]}
    with pytest.raises(Fault, match="advertised"):
        host.request("call", {"ref": ref, "member": "__class__"})
    assert obj._gets == 0


def test_returned_object_identity_release_and_literal_reference_like_data():
    host = ObjectHost({"model": Model()})
    ref = host.request("list", {})["objects"][0]["ref"]
    child = host.request("call", {"ref": ref, "member": "child"})["result"]
    assert child == host.request("call", {"ref": ref, "member": "child"})["result"]
    assert host.request("call", {"ref": ref, "member": "accept", "arguments": [child]})["result"]["value"] is True
    literal = {"kind": "object", "ref": "forged"}
    assert host.request("call", {"ref": ref, "member": "echo", "arguments": [{"kind": "value", "value": literal}]})["result"] == {"kind": "value", "value": literal}
    host.request("release", {"ref": child["ref"]})
    with pytest.raises(Fault) as exc:
        host.request("get", {"ref": child["ref"], "member": "label"})
    assert exc.value.code == "stale_ref"


def test_event_overflow_is_bounded_and_reported():
    obj = Model()
    host = ObjectHost({"model": obj})
    ref = host.request("list", {})["objects"][0]["ref"]
    host.request("watch", {"ref": ref, "names": ["changed"]})
    for _ in range(1030):
        obj.changed.emit()
    data = host.request("events", {"limit": 1000})
    assert len(data["events"]) == 1000 and data["dropped"] == 6
    assert data["events"][0]["sequence"] == 7
    assert not data["values_in_events"]


def config(tmp_path, timeout=6):
    path = tmp_path / "objects.json"
    target = {"runtime": "python", "timeout": timeout, "command": ["{python}", "{root}/bridges/python_agent.py", "--module", "{root}/tests/fixtures/business_objects.py"]}
    path.write_text(json.dumps({"protocol": "appctl.objects/1", "targets": {"a": target, "b": target}}), encoding="utf-8")
    return path


def test_refs_are_bound_to_source_instance_and_object_arguments_cannot_cross_providers(tmp_path):
    with ObjectSession(config(tmp_path)) as session:
        a = session.request("list", {"target": "a"})["data"]["objects"][0]["ref"]
        b = session.request("list", {"target": "b"})["data"]["objects"][0]["ref"]
        result = session.request("call", {"ref": a, "member": "same_child", "arguments": [None], "object_arguments": {"0": b}})
        assert result["error"]["code"] == "invalid_arguments"
        session.providers["a"].close()
        assert session.request("get", {"ref": a, "member": "count"})["error"]["code"] == "stale_ref"
        assert session.request("list", {"target": "a"})["error"]["code"] == "provider_unavailable"
        assert session.request("connect", {"target": "a"})["ok"]
        assert session.request("get", {"ref": a, "member": "count"})["error"]["code"] == "stale_ref"
        assert session.request("get", {"ref": b, "member": "count"})["ok"]


def test_literal_reference_envelopes_cannot_forge_client_refs(tmp_path):
    with ObjectSession(config(tmp_path)) as session:
        ref = session.request("list", {"target": "a"})["data"]["objects"][0]["ref"]
        value = {"kind": "object", "ref": "forged"}
        result = session.request("call", {"ref": ref, "member": "echo", "arguments": [value]})
        assert result["data"]["result"] == {"kind": "value", "value": value}
        assert "forged" not in session.refs


@pytest.mark.parametrize("operation,code", [("describe", "provider_timeout"), ("call", "outcome_unknown")])
def test_timeout_never_retries_or_restarts_provider(tmp_path, operation, code):
    script = tmp_path / "hung.py"
    script.write_text('''import json,sys,time
for line in sys.stdin:
 r=json.loads(line)
 if r['op']=='ping':
  print(json.dumps({'id':r['id'],'ok':True,'data':{'protocol':'appctl.objects/1','runtime':'python'}}),flush=True)
 else:
  with open('count.txt','a') as f:f.write('1')
  time.sleep(5)
''', encoding="utf-8")
    provider = StdioProvider([sys.executable, str(script)], str(tmp_path), timeout=.05)
    try:
        result = provider.request(operation, {})
        assert result["error"]["code"] == code
        assert provider.process.poll() is not None
        assert (tmp_path / "count.txt").read_text() == "1"
    finally:
        provider.close()


def test_read_only_default_targets_and_configuration_do_not_spawn_processes(tmp_path):
    with ObjectSession(config(tmp_path)) as session:
        result = session.request("targets")
        assert result["ok"] and len(result["data"]["targets"]) == 2
        assert not session.providers
        assert "command" not in json.dumps(result)


def test_missing_configuration_is_an_explicit_empty_registry(tmp_path):
    with ObjectSession(tmp_path / "not-present.json") as session:
        data = session.request("targets")["data"]
        assert not data["configured"] and data["targets"] == []


def test_external_attach_rejects_unscoped_or_unimplemented_targets(tmp_path):
    with ObjectSession(tmp_path / "none.json") as session:
        for args in ({}, {"rot_id": "not a discovered id"}, {"runtime": "java", "progid": "Example.App"}, {"progid": "Example.App", "rot_id": "a" * 24}):
            assert session.request("attach", args)["error"]["code"] == "invalid_arguments"
        assert session.request("discover", {"runtime": "java"})["error"]["code"] == "unsupported_discovery"
        assert not session.providers


def test_normal_close_allows_provider_finally_cleanup(tmp_path):
    script = tmp_path / 'cleanup.py'
    script.write_text('''import json,sys
try:
 for line in sys.stdin:
  r=json.loads(line)
  print(json.dumps({'id':r['id'],'ok':True,'data':{'protocol':'appctl.objects/1','runtime':'python'}}),flush=True)
finally:
 with open('cleaned.txt','w') as f:f.write('cleaned')
''', encoding='utf-8')
    provider = StdioProvider([sys.executable, str(script)], str(tmp_path))
    provider.close()
    assert (tmp_path / 'cleaned.txt').read_text() == 'cleaned'
