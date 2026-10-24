import copy
import json
from pathlib import Path

import pytest

from capability_registry import CapabilityRegistry, template, onboard
from object_bridge import Fault, ObjectHost


ROOT = Path(__file__).resolve().parents[1]


class Counter:
    def __init__(self):
        self.count, self.calls, self.lie = 0, 0, False

    def add(self, amount: int) -> int:
        self.calls += 1
        self.count += amount
        return 999 if self.lie else self.count


class LocalSession:
    def __init__(self, counter):
        self.counter = counter
        self.host = ObjectHost({"counter": counter})
        self.changed, self.observation = False, False

    def request(self, op, args):
        args = copy.deepcopy(args)
        if op == "call":
            args["arguments"] = [{"kind": "value", "value": a} for a in args.get("arguments", [])]
        if op == "set":
            args["value"] = {"kind": "value", "value": args["value"]}
        try:
            data = self.host.request(op, args)
            if op == "describe":
                if self.changed:
                    data["type"] += ".new_version"
                if self.observation:
                    data["observation_only"] = True
            return {"ok": True, "data": data}
        except Fault as exc:
            return {"ok": False, "error": {"code": exc.code, "message": str(exc)}}

    def close(self):
        pass


def setup_registry(tmp_path, transform=None):
    config = tmp_path / "objects.json"
    definition = {"protocol": "appctl.objects/1", "targets": {"fixture": {"runtime": "python", "command": ["unused"]}}}
    config.write_text(json.dumps(definition), encoding="utf-8")
    manifest = json.loads((ROOT / "capabilities/counter-demo.json").read_text(encoding="utf-8"))
    manifest["provider"] = {"config": "objects.json", "target": "fixture"}
    if transform:
        transform(manifest)
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    sessions, counter = [], Counter()

    def factory(_):
        session = LocalSession(counter)
        sessions.append(session)
        return session

    registry = CapabilityRegistry(tmp_path / "registry.json", session_factory=factory)
    assert registry.request("register", {"path": str(path)})["ok"]
    return registry, sessions, counter, config, path


def validate(registry, name="counter.increment", allow_write=True):
    return registry.request("validate", {"app_id": "counter-demo", "capability_id": name, "allow_write": allow_write})


def invoke(registry, arguments=None, name="counter.increment"):
    return registry.request("invoke", {"app_id": "counter-demo", "capability_id": name,
                                       "arguments": {"amount": 3} if arguments is None else arguments})


def test_registration_listing_and_candidate_calls_do_not_start_application(tmp_path):
    registry, sessions, counter, _, _ = setup_registry(tmp_path)
    assert len(registry.list()["capabilities"]) == 2
    assert all(not c["callable"] for c in registry.list()["capabilities"])
    assert invoke(registry)["error"]["code"] == "candidate_only"
    assert not sessions and counter.calls == 0


def test_validated_named_call_checks_application_state(tmp_path):
    registry, _, counter, _, _ = setup_registry(tmp_path)
    assert validate(registry)["ok"]
    result = invoke(registry)
    assert result["ok"] and result["data"]["result"] == counter.count == 5
    assert result["data"]["status"] == "postconditions_verified"


def test_bad_arguments_do_not_execute_or_revoke_existing_capability(tmp_path):
    registry, _, counter, _, _ = setup_registry(tmp_path)
    assert validate(registry)["ok"]
    before = counter.calls
    assert invoke(registry, {"amount": "3"})["error"]["code"] == "schema_mismatch"
    assert invoke(registry, {"amount": True})["error"]["code"] == "schema_mismatch"
    assert counter.calls == before
    assert registry.list()["capabilities"][1]["callable"]


def test_metadata_change_blocks_action_and_quarantines_binding(tmp_path):
    registry, sessions, counter, _, _ = setup_registry(tmp_path)
    assert validate(registry)["ok"]
    sessions[0].changed = True
    result = invoke(registry)
    assert result["error"]["code"] == "contract_changed"
    assert counter.calls == 1 and result["error"]["capability_quarantined"]
    assert not registry.list()["capabilities"][1]["callable"]


def test_false_function_return_is_detected_by_postcondition_without_retry(tmp_path):
    registry, _, counter, _, _ = setup_registry(tmp_path)
    assert validate(registry)["ok"]
    counter.lie = True
    result = invoke(registry)
    assert result["error"]["code"] == "postcondition_failed"
    assert result["error"]["operation_may_have_executed"] is True
    assert counter.calls == 2 and counter.count == 5
    assert not registry.list()["capabilities"][1]["callable"]


def test_provider_change_invalidates_but_an_unrelated_new_app_does_not(tmp_path):
    registry, _, counter, config, _ = setup_registry(tmp_path)
    assert validate(registry)["ok"]
    definition = json.loads(config.read_text())
    definition["targets"]["another-app"] = {"runtime": "python", "command": ["unused-new-app"]}
    config.write_text(json.dumps(definition))
    assert invoke(registry)["ok"]
    definition["targets"]["fixture"]["command"] = ["changed"]
    config.write_text(json.dumps(definition))
    before = counter.calls
    assert invoke(registry)["error"]["code"] == "provider_changed"
    assert counter.calls == before


def test_inferred_purpose_and_write_test_require_explicit_review(tmp_path):
    registry, sessions, _, _, _ = setup_registry(tmp_path, lambda m: m["capabilities"][1]["purpose"].update(basis="inferred"))
    assert validate(registry)["error"]["code"] == "purpose_unconfirmed"
    assert not sessions
    other = tmp_path / "other"
    other.mkdir()
    registry, sessions, _, _, _ = setup_registry(other)
    assert validate(registry, allow_write=False)["error"]["code"] == "explicit_test_required"
    assert not sessions


def test_failed_revalidation_removes_previous_validation(tmp_path):
    registry, _, counter, _, _ = setup_registry(tmp_path)
    assert validate(registry)["ok"]
    result = validate(registry)  # fixture is no longer in its initial test state
    assert result["error"]["code"] == "validation_failed"
    assert not registry.list()["capabilities"][1]["callable"]
    assert counter.calls == 2


def test_observation_snapshot_cannot_be_promoted_to_executable_api(tmp_path):
    registry, sessions, _, _, _ = setup_registry(tmp_path)
    def factory(_):
        session = LocalSession(Counter())
        session.observation = True
        sessions.append(session)
        return session
    registry.factory = factory
    assert validate(registry)["error"]["code"] == "observation_only"
    assert sessions[0].counter.calls == 0


def test_literal_parameter_data_is_not_reinterpreted_as_a_binding():
    value = {"$arg": "other", "ref": "forged"}
    assert template({"$arg": "payload"}, {"payload": value}) == value


def test_boolean_expected_result_cannot_validate_a_numeric_zero(tmp_path):
    def change(manifest):
        cap = manifest["capabilities"][0]
        cap["output_schema"] = {}
        cap["tests"][0]["expected"] = False
    registry, _, _, _, _ = setup_registry(tmp_path, change)
    assert validate(registry, name="counter.read", allow_write=False)["error"]["code"] == "validation_failed"


def test_non_finite_input_is_rejected_before_a_provider_is_opened(tmp_path):
    registry, sessions, _, _, _ = setup_registry(tmp_path)
    # Validate once, then use a fresh client with the saved registration.
    assert validate(registry)["ok"]
    other_sessions = []
    other = CapabilityRegistry(registry.path, session_factory=lambda _: other_sessions.append(True))
    assert invoke(other, {"amount": float("nan")})["error"]["code"] == "schema_mismatch"
    assert not other_sessions


def test_registration_does_not_fetch_remote_schema_references(tmp_path):
    registry, sessions, _, _, path = setup_registry(tmp_path)
    manifest = json.loads(path.read_text())
    manifest["capabilities"][0]["input_schema"]["$ref"] = "https://example.invalid/schema.json"
    path.write_text(json.dumps(manifest))
    result = registry.request("register", {"path": str(path)})
    assert result["error"]["code"] == "invalid_configuration"
    assert "local JSON Schema" in result["error"]["message"] and not sessions


def test_existing_onboarding_output_is_rejected_before_provider_lookup(tmp_path):
    output = tmp_path / "draft.json"
    output.write_text("preserve")
    with pytest.raises(FileExistsError):
        onboard(tmp_path / "not-a-provider.json", "missing", "new-app", output)
    assert output.read_text() == "preserve"
