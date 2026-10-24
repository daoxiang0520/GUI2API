import json

import pytest

from object_bridge import Fault, ObjectHost, exposed
from object_client import ObjectSession
from object_explorer import explore


class Child:
    def __init__(self, parent):
        self.parent = parent
        self.label = "private contents are not exploration metadata"


class Model:
    def __init__(self):
        self._gets = self._calls = 0
        self.child = Child(self)

    @property
    def dangerous(self):
        self._gets += 1
        raise RuntimeError("getter failed after a side effect")

    def get_and_delete(self):
        self._calls += 1

    @exposed(read_only=True)
    def child_read(self):
        self._calls += 1
        return self.child

    @exposed(read_only=True)
    def with_argument(self, argument):
        self._calls += 1

    @exposed(read_only=True)
    def literal(self):
        return {"kind": "object", "ref": "forged", "secret": "do not copy"}


class LocalSession:
    def __init__(self, model):
        self.host = ObjectHost({"model": model, "same_model": model})
        self.requests = []

    def request(self, op, args):
        self.requests.append((op, args))
        try:
            return {"ok": True, "data": self.host.request(op, args)}
        except Exception as exc:
            return {"ok": False, "error": {"code": exc.code if isinstance(exc, Fault) else "application_error", "message": str(exc)}}


def test_default_discovers_metadata_and_aliases_without_executing_application_members():
    obj = Model()
    session = LocalSession(obj)
    data = explore(session, target="arbitrary-name")["data"]
    assert obj._gets == obj._calls == 0
    assert len(data["nodes"]) == 1 and len(data["roots"]) == 2
    assert data["nodes"][0]["paths"] == [["model"], ["same_model"]]
    assert [op for op, _ in session.requests] == ["list", "describe"]
    assert not data["business_semantics_verified"] and not data["arbitrary_process_attach"]


def test_selected_properties_walk_shared_objects_and_cycles_once_without_saving_values():
    session = LocalSession(Model())
    data = explore(session, target="a", read_members=["child", "parent", "label"])["data"]
    assert len(data["nodes"]) == 2 and len(data["edges"]) == 3
    assert data["statistics"]["descriptions"] == 2
    assert data["edges"][1]["value_type"] == "str"
    assert data["edges"][2]["to"] == data["nodes"][0]["ref"]
    assert "private contents" not in json.dumps(data)


def test_opt_in_declared_read_traversal_does_not_guess_from_names_or_call_required_parameters():
    obj = Model()
    data = explore(LocalSession(obj), target="a", call_read_methods=True)["data"]
    assert obj._calls == 1 and obj._gets == 0
    assert len(data["nodes"]) == 2
    assert data["deferred"]["method_effect_not_declared_read"] == 1
    assert data["deferred"]["method_requires_arguments_or_signature_unknown"] == 1
    assert data["statistics"]["declared_read_calls"] == 2
    assert "forged" not in json.dumps(data) and "do not copy" not in json.dumps(data)
    assert any(edge.get("value_type") == "dict" for edge in data["edges"])


def test_failed_getter_is_attempted_once_and_stops_calls_after_unknown_state_change():
    obj = Model()
    data = explore(LocalSession(obj), target="a", read_members=["dangerous"], call_read_methods=True)["data"]
    assert obj._gets == 1 and obj._calls == 0
    assert data["status"] == "partial" and data["stopped_by"] == ["operation_failed"]
    assert len(data["errors"]) == 1 and data["statistics"]["requests"] == 3


@pytest.mark.parametrize("limits,reason", [
    ({"max_requests": 2}, "max_requests"),
    ({"max_depth": 0}, "max_depth"),
    ({"max_objects": 1}, "max_objects"),
    ({"max_members": 1}, "max_members"),
])
def test_exploration_limits_report_partial_coverage(limits, reason):
    data = explore(LocalSession(Model()), target="a", read_members=["child"], **limits)["data"]
    assert reason in data["stopped_by"] and data["status"] == "partial"
    assert data["statistics"]["requests"] <= limits.get("max_requests", 128)
    assert len(data["nodes"]) <= limits.get("max_objects", 32)


def test_query_matches_are_evidence_not_verified_business_semantics_and_fingerprints_are_stable():
    first = explore(LocalSession(Model()), target="a", query="delete")["data"]
    second = explore(LocalSession(Model()), target="a")["data"]
    assert first["matches"][0]["member"] == "get_and_delete"
    assert not first["matches"][0]["business_meaning_verified"]
    assert first["nodes"][0]["metadata_fingerprint"] == second["nodes"][0]["metadata_fingerprint"]
    assert first["nodes"][0]["ref"] != second["nodes"][0]["ref"]


def test_type_and_protocol_field_names_do_not_match_every_member():
    data = explore(LocalSession(Model()), target="a", query="Model getter_executed")["data"]
    assert len(data["matches"]) == 1 and data["matches"][0]["category"] == "object"


def test_unavailable_signature_is_not_treated_as_a_zero_argument_read_method():
    class UnknownSignature:
        def __init__(self):
            self._calls = 0

        @exposed(read_only=True)
        def read(self):
            self._calls += 1

        read.__signature__ = "not an inspect.Signature"

    obj = UnknownSignature()
    data = explore(LocalSession(obj), target="a", call_read_methods=True)["data"]
    assert not data["nodes"][0]["methods"][0]["signature_available"]
    assert obj._calls == 0 and data["statistics"]["declared_read_calls"] == 0
    assert data["deferred"]["method_requires_arguments_or_signature_unknown"] == 1


def test_deadline_is_checked_between_requests_without_sleeping_or_retrying(monkeypatch):
    import object_explorer
    clock = [0.0]
    monkeypatch.setattr(object_explorer.time, "monotonic", lambda: clock[0])

    class SlowSession(LocalSession):
        def request(self, op, args):
            result = super().request(op, args)
            clock[0] += 1
            return result

    session = SlowSession(Model())
    data = explore(session, target="a", max_seconds=.5)["data"]
    assert data["stopped_by"] == ["max_seconds"]
    assert data["statistics"]["requests"] == 1
    assert [op for op, _ in session.requests] == ["list"]


@pytest.mark.parametrize("options", [
    {"max_objects": True}, {"max_depth": -1}, {"max_requests": 0},
    {"max_seconds": float("nan")}, {"max_members": 0},
    {"call_read_methods": "false"}, {"read_members": "child"}, {"query": []},
])
def test_invalid_options_do_not_connect_or_call_a_provider(options):
    session = LocalSession(Model())
    with pytest.raises(ValueError):
        explore(session, target="a", **options)
    assert session.requests == []


def test_live_session_supports_target_exploration_and_existing_returned_refs():
    with ObjectSession("objects.example.json") as session:
        data = session.request("explore", {"target": "python-demo", "call_read_methods": True})["data"]
        child = next(node for node in data["nodes"] if node["type"].endswith(".Child"))
        again = session.request("explore", {"ref": child["ref"], "query": "echo"})
        assert again["ok"] and again["data"]["nodes"][0]["ref"] == child["ref"]
        assert again["data"]["matches"][0]["member"] == "echo"
        assert session.request("get", {"ref": child["ref"], "member": "label"})["ok"]
