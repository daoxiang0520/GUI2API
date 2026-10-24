"""Register named application functions above explicitly configured object bridges.

Descriptions are declarations, not inferred facts. Registration never starts a
provider. Contract validation is explicit; application content cannot activate a
candidate. Bindings contain data only, and stale refs are never persisted.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from object_client import ObjectSession

FORMAT = "appctl.capabilities/1"
ROOT = Path(__file__).resolve().parent
IDENTIFIER = re.compile(r"^[a-z][a-z0-9_.-]{0,95}$")


class CapabilityError(Exception):
    def __init__(self, code, message, **details):
        self.code, self.details = code, details
        super().__init__(message)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def load_json(path):
    raw = Path(path).read_bytes()
    if len(raw) > 2 * 1024 * 1024:
        raise ValueError("Configuration exceeds 2 MiB")
    return json.loads(raw.decode("utf-8-sig"))


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2, allow_nan=False)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def check_value(schema, value):
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise CapabilityError("schema_mismatch", "Arguments and results must be finite JSON values") from exc
    errors = sorted(Draft202012Validator(schema).iter_errors(value), key=lambda e: str(list(e.path)))
    if errors:
        raise CapabilityError("schema_mismatch", errors[0].message, path=list(errors[0].path))


def same_json(left, right):
    # JSON booleans are not numbers, even though Python's False == 0.
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return left == right
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(same_json(left[k], right[k]) for k in left)
    if isinstance(left, list):
        return len(left) == len(right) and all(same_json(a, b) for a, b in zip(left, right))
    return left == right


def local_schema(schema):
    """Keep manifest validation local; remote schema references are not fetched."""
    pending = [schema]
    while pending:
        node = pending.pop()
        if isinstance(node, dict):
            for key in ("$ref", "$dynamicRef"):
                if key in node and (not isinstance(node[key], str) or not node[key].startswith("#")):
                    raise ValueError("Only local JSON Schema references are supported")
            pending.extend(node.values())
        elif isinstance(node, list):
            pending.extend(node)


def validate_manifest(manifest):
    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT:
        raise ValueError("Expected appctl.capabilities/1 manifest")
    app, provider = manifest.get("app", {}), manifest.get("provider", {})
    if not isinstance(app, dict) or not IDENTIFIER.fullmatch(app.get("id", "")) or not app.get("name"):
        raise ValueError("Application requires a stable id and display name")
    if not isinstance(provider, dict) or not all(isinstance(provider.get(k), str) and provider[k] for k in ("config", "target")):
        raise ValueError("Choose one explicit object config and target")
    capabilities = manifest.get("capabilities")
    if not isinstance(capabilities, list) or not 1 <= len(capabilities) <= 256:
        raise ValueError("Manifest requires 1..256 capabilities")
    names = set()
    for cap in capabilities:
        if not isinstance(cap, dict) or not IDENTIFIER.fullmatch(cap.get("id", "")) or cap["id"] in names:
            raise ValueError("Capability ids must be valid and unique within an application")
        names.add(cap["id"])
        if not isinstance(cap.get("title"), str) or not cap["title"]:
            raise ValueError("Each capability requires a title")
        purpose = cap.get("purpose", {})
        if not isinstance(purpose, dict) or not isinstance(purpose.get("description"), str) or purpose.get("basis") not in ("declared", "documentation", "inferred", "observed"):
            raise ValueError("Purpose requires a description and an explicit evidence basis")
        if cap.get("effect") not in ("read", "write", "external", "unknown"):
            raise ValueError("Declare effect as read, write, external or unknown")
        for key in ("input_schema", "output_schema"):
            if not isinstance(cap.get(key), dict):
                raise ValueError("Input and output JSON schemas are required")
            local_schema(cap[key])
            Draft202012Validator.check_schema(cap[key])
        if cap["input_schema"].get("type") != "object":
            raise ValueError("Capability arguments must use an object schema")
        binding = cap.get("binding", {})
        if not isinstance(binding, dict) or not isinstance(binding.get("root"), str) or not binding["root"]:
            raise ValueError("Binding requires an exact registered object root name")
        path = binding.get("path", [])
        if not isinstance(path, list) or len(path) > 8:
            raise ValueError("Object path must contain at most 8 steps")
        for index, step in enumerate([*path, binding]):
            allowed = ("get", "call") if index < len(path) else ("get", "set", "call")
            if not isinstance(step, dict) or step.get("op") not in allowed or not isinstance(step.get("member"), str) or not step["member"] or step["member"].startswith("_"):
                raise ValueError("Binding steps require fixed public member names and supported operations")
            if step["op"] == "set" and "value" not in step:
                raise ValueError("A set step requires an explicit value template")
            if not isinstance(step.get("arguments", []), list) or len(step.get("arguments", [])) > 64:
                raise ValueError("Method arguments must be a bounded JSON array")
        tests = cap.get("tests", [])
        if not isinstance(tests, list) or len(tests) > 8:
            raise ValueError("At most 8 explicit validation cases are supported")
        for test in tests:
            if not isinstance(test, dict) or not isinstance(test.get("arguments"), dict) or "expected" not in test:
                raise ValueError("Validation cases require arguments and an explicit expected result")
            check_value(cap["input_schema"], test["arguments"])
            check_value(cap["output_schema"], test["expected"])
        checks = cap.get("postconditions", [])
        if not isinstance(checks, list) or len(checks) > 8 or any(not isinstance(c, dict) or not isinstance(c.get("member"), str) or c["member"].startswith("_") or "equals" not in c for c in checks):
            raise ValueError("Postconditions must name at most 8 public properties and expected values")
    return manifest


def provider_definition(path, target):
    config = load_json(path)
    if config.get("protocol") != "appctl.objects/1" or target not in config.get("targets", {}):
        raise ValueError("The selected provider is not in the supplied object configuration")
    return {"protocol": config["protocol"], "target": target, "definition": config["targets"][target]}


def template(value, arguments):
    if isinstance(value, dict) and set(value) == {"$arg"}:
        if value["$arg"] not in arguments:
            raise CapabilityError("missing_argument", "A binding parameter was not supplied")
        # Literal application data is not interpreted as another template.
        return copy.deepcopy(arguments[value["$arg"]])
    if isinstance(value, dict):
        return {k: template(v, arguments) for k, v in value.items()}
    if isinstance(value, list):
        return [template(v, arguments) for v in value]
    return value


class CapabilityRegistry:
    def __init__(self, path=None, session_factory=ObjectSession):
        self.path = Path(path or os.environ.get("APPCTL_CAPABILITY_REGISTRY", ROOT / "capabilities.local.json")).resolve()
        self.factory, self.sessions = session_factory, {}

    def _read(self):
        if not self.path.exists():
            return {"format": FORMAT, "apps": {}}
        data = load_json(self.path)
        if data.get("format") != FORMAT or not isinstance(data.get("apps"), dict):
            raise ValueError("Invalid capability registry")
        return data

    @contextmanager
    def _edit(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(str(self.path) + ".lock", "a+b") as lock:
            lock.seek(0, 2)
            if not lock.tell():
                lock.write(b"0")
                lock.flush()
            lock.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                data = self._read()
                yield data
                atomic_json(self.path, data)
            finally:
                lock.seek(0)
                if os.name == "nt":
                    msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(lock, fcntl.LOCK_UN)

    def register(self, path):
        path = Path(path).resolve()
        manifest = validate_manifest(load_json(path))
        config = (path.parent / manifest["provider"]["config"]).resolve()
        definition = provider_definition(config, manifest["provider"]["target"])
        record = {"manifest": manifest, "manifest_hash": digest(manifest), "provider_config": str(config),
                  "provider_hash": digest(definition), "validated": {}}
        with self._edit() as data:
            data["apps"][manifest["app"]["id"]] = record
        return {"app": manifest["app"], "registered_candidates": len(manifest["capabilities"]),
                "application_calls": 0, "validation_required": True}

    @staticmethod
    def _fresh(record):
        try:
            return digest(provider_definition(record["provider_config"], record["manifest"]["provider"]["target"])) == record["provider_hash"]
        except (OSError, ValueError, KeyError):
            return False

    def list(self, app_id=None, query=""):
        rows = []
        for name, record in self._read()["apps"].items():
            if app_id and name != app_id:
                continue
            fresh = self._fresh(record)
            for cap in record["manifest"]["capabilities"]:
                if query and query.casefold() not in json.dumps([cap["id"], cap["title"], cap["purpose"]], ensure_ascii=False).casefold():
                    continue
                validation = record["validated"].get(cap["id"])
                rows.append({"app_id": name, "id": cap["id"], "title": cap["title"], "purpose": cap["purpose"],
                             "effect": cap["effect"], "input_schema": cap["input_schema"], "output_schema": cap["output_schema"],
                             "status": "provider_changed" if not fresh else "contract_validated" if validation else "candidate",
                             "callable": fresh and bool(validation), "semantic_truth_proven": False})
        return {"capabilities": rows, "application_calls": 0, "universal_visual_coverage": False}

    def _select(self, app_id, capability_id):
        record = self._read()["apps"].get(app_id)
        if not record:
            raise CapabilityError("unknown_app", "Register this application first")
        caps = [c for c in record["manifest"]["capabilities"] if c["id"] == capability_id]
        if len(caps) != 1:
            raise CapabilityError("unknown_capability", "Choose an exact registered capability id")
        if not self._fresh(record):
            raise CapabilityError("provider_changed", "The selected provider changed; register and validate again")
        return record, caps[0]

    def describe(self, app_id, capability_id):
        record, cap = self._select(app_id, capability_id)
        return {"app": record["manifest"]["app"], "capability": cap,
                "validation": record["validated"].get(capability_id), "semantic_truth_proven": False}

    def _execute(self, record, cap, arguments, expected_contract=None):
        check_value(cap["input_schema"], arguments)
        key = (record["provider_config"], record["provider_hash"])
        if key not in self.sessions:
            session = self.factory(record["provider_config"])
            if not self._fresh(record):
                session.close()
                raise CapabilityError("provider_changed", "Provider changed before connection")
            self.sessions[key] = session
        session = self.sessions[key]
        attempted, contracts = False, []

        def request(op, args):
            nonlocal attempted
            if op in ("get", "set", "call"):
                attempted = True
            result = session.request(op, args)
            if not result.get("ok"):
                fault = result.get("error", {})
                raise CapabilityError(fault.get("code", "binding_failed"), fault.get("message", "Object binding failed"),
                                      operation_may_have_executed=attempted, retried=False)
            return result["data"]

        roots = request("list", {"target": record["manifest"]["provider"]["target"]})["objects"]
        binding = cap["binding"]
        chosen = [r for r in roots if r["name"] == binding["root"]]
        if len(chosen) != 1:
            raise CapabilityError("ambiguous_root", "An exact current object root was not found")
        ref = chosen[0]["ref"]
        steps = [*binding.get("path", []), binding]
        for index, step in enumerate(steps):
            desc = request("describe", {"ref": ref})
            if desc.get("observation_only"):
                raise CapabilityError("observation_only", "Metadata snapshots have no executable object binding")
            category = "methods" if step["op"] == "call" else "properties"
            members = [m for m in desc[category] if m["name"] == step["member"]]
            if len(members) != 1:
                raise CapabilityError("contract_changed", "The exact registered member is no longer advertised", operation_may_have_executed=attempted)
            member = members[0]
            if step["op"] == "call" and (member.get("callable") is False or member.get("signature_available") is False):
                raise CapabilityError("not_callable", "This member has no supported invocation binding")
            if step["op"] in ("get", "set") and not member.get("readable" if step["op"] == "get" else "writable"):
                raise CapabilityError("not_callable", "This property does not support the requested operation")
            contracts.append(digest({"type": desc["type"], "member": member, "op": step["op"]}))
            if expected_contract is not None and (index >= len(expected_contract) or contracts[-1] != expected_contract[index]):
                raise CapabilityError("contract_changed", "Object metadata differs from the validated binding", operation_may_have_executed=attempted)
            if index == len(steps) - 1:
                for check in cap.get("postconditions", []):
                    fields = [p for p in desc["properties"] if p["name"] == check["member"] and p.get("readable")]
                    if len(fields) != 1:
                        raise CapabilityError("contract_changed", "A postcondition property is no longer readable", operation_may_have_executed=attempted)
                    contracts.append(digest({"type": desc["type"], "postcondition": fields[0]}))
                    if expected_contract is not None and (len(contracts) > len(expected_contract) or contracts[-1] != expected_contract[len(contracts) - 1]):
                        raise CapabilityError("contract_changed", "Postcondition metadata changed before execution", operation_may_have_executed=attempted)
            args = {"ref": ref, "member": step["member"]}
            if step["op"] == "call":
                args["arguments"] = template(step.get("arguments", []), arguments)
            if step["op"] == "set":
                args["value"] = template(step["value"], arguments)
            response = request(step["op"], args)
            if response.get("status") == "verification_failed":
                raise CapabilityError("verification_failed", "The object did not retain the requested value", operation_may_have_executed=True)
            result = response.get("result", {})
            if index < len(steps) - 1:
                if result.get("kind") != "object":
                    raise CapabilityError("binding_failed", "Object path did not return an object", operation_may_have_executed=attempted)
                ref = result["ref"]
            elif result.get("kind") != "value":
                raise CapabilityError("unsupported_result", "Named capabilities return JSON values; raw object refs are session-specific", operation_may_have_executed=attempted)
        value = result.get("value")
        for check in cap.get("postconditions", []):
            actual = request("get", {"ref": ref, "member": check["member"]}).get("result", {})
            expected = value if check["equals"] == {"$result": True} else template(check["equals"], arguments)
            if actual.get("kind") != "value" or not same_json(actual.get("value"), expected):
                raise CapabilityError("postcondition_failed", "Function return did not match the declared application state", operation_may_have_executed=True, retried=False)
        try:
            check_value(cap["output_schema"], value)
        except CapabilityError as exc:
            exc.details.update(operation_may_have_executed=attempted, retried=False)
            raise
        return value, contracts

    def validate(self, app_id, capability_id, allow_write=False):
        if type(allow_write) is not bool:
            raise ValueError("allow_write must be boolean")
        record, cap = self._select(app_id, capability_id)
        if cap["purpose"]["basis"] == "inferred":
            raise CapabilityError("purpose_unconfirmed", "A guessed purpose must be reviewed before validation")
        if cap["effect"] not in ("read", "write") or cap["effect"] == "write" and not allow_write:
            raise CapabilityError("explicit_test_required", "Only declared reads or explicitly enabled write tests can run here")
        if not cap.get("tests"):
            raise CapabilityError("tests_required", "Declare isolated validation arguments and expected results")
        self._revoke(app_id, capability_id, record["manifest_hash"])
        contracts = None
        for case in cap["tests"]:
            value, current = self._execute(record, cap, case["arguments"], contracts)
            if not same_json(value, case["expected"]):
                raise CapabilityError("validation_failed", "Observed result differs from the declared test result", operation_may_have_executed=True, retried=False)
            contracts = current
        validation = {"validated_at": datetime.now(timezone.utc).isoformat(), "cases_passed": len(cap["tests"]),
                      "contracts": contracts, "purpose_basis": cap["purpose"]["basis"],
                      "semantic_truth_proven": False, "scope": "declared cases and current metadata only"}
        with self._edit() as data:
            current = data["apps"].get(app_id)
            if not current or current["manifest_hash"] != record["manifest_hash"] or not self._fresh(record):
                raise CapabilityError("registration_changed", "Registration changed during validation")
            current["validated"][capability_id] = validation
        return validation

    def _revoke(self, app_id, capability_id, manifest_hash):
        with self._edit() as data:
            current = data["apps"].get(app_id)
            if current and current["manifest_hash"] == manifest_hash:
                current["validated"].pop(capability_id, None)

    def invoke(self, app_id, capability_id, arguments):
        record, cap = self._select(app_id, capability_id)
        validation = record["validated"].get(capability_id)
        if not validation:
            raise CapabilityError("candidate_only", "Validate this capability before invoking its named API")
        try:
            value, _ = self._execute(record, cap, arguments, validation["contracts"])
        except CapabilityError as exc:
            if exc.code not in ("schema_mismatch", "missing_argument") or exc.details.get("operation_may_have_executed"):
                self._revoke(app_id, capability_id, record["manifest_hash"])
                exc.details["capability_quarantined"] = True
            raise
        return {"result": value, "status": "postconditions_verified" if cap.get("postconditions") else "contract_checked",
                "business_completion_proven": False, "retried": False}

    def request(self, op, args=None):
        args = args or {}
        try:
            if op not in ("register", "list", "describe", "validate", "invoke"):
                raise CapabilityError("unknown_operation", "Unsupported capability registry operation")
            return {"ok": True, "data": getattr(self, op)(**args)}
        except CapabilityError as exc:
            return {"ok": False, "error": {"code": exc.code, "message": str(exc), **exc.details}}
        except (OSError, ValueError, TypeError, KeyError, SchemaError) as exc:
            return {"ok": False, "error": {"code": "invalid_configuration", "message": str(exc)}}

    def close(self):
        for session in self.sessions.values():
            session.close()
        self.sessions.clear()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def onboard(config, target, app_id, output, root=None):
    """Generate a metadata-only draft. No getter, setter or method is executed."""
    if not isinstance(app_id, str) or not IDENTIFIER.fullmatch(app_id):
        raise ValueError("Choose a stable application id")
    config, output = Path(config).resolve(), Path(output).resolve()
    if output.exists():
        raise FileExistsError("Draft output already exists; choose a new path or edit it explicitly")
    provider_definition(config, target)
    with ObjectSession(config) as session:
        listed = session.request("list", {"target": target})
        if not listed.get("ok"):
            raise CapabilityError("provider_unavailable", "The explicitly selected provider could not be opened")
        roots = [r for r in listed["data"]["objects"] if root is None or r["name"] == root]
        if len(roots) != 1:
            raise ValueError("Choose one exact --root from the configured provider")
        result = session.request("describe", {"ref": roots[0]["ref"]})
        if not result.get("ok"):
            raise CapabilityError("metadata_unavailable", "The selected root did not provide metadata")
        desc = result["data"]
    capabilities = []
    for category in ("properties", "methods"):
        for member in desc[category]:
            ops = ["call"] if category == "methods" else ["get"] + (["set"] if member.get("writable") else [])
            for op in ops:
                name = member["name"]
                identifier = op + "." + re.sub(r"[^a-z0-9_.-]", "-", name.casefold())[:70]
                if any(c["id"] == identifier for c in capabilities):
                    identifier += "-" + digest(name)[:8]
                parameters = member.get("parameters", []) if op == "call" else [{"name": "value"}] if op == "set" else []
                fields = {p.get("name") or f"arg{i}": {} for i, p in enumerate(parameters)}
                binding = {"root": roots[0]["name"], "path": [], "op": op, "member": name}
                if op == "call":
                    binding["arguments"] = [{"$arg": n} for n in fields]
                elif op == "set":
                    binding["value"] = {"$arg": "value"}
                capabilities.append({"id": identifier, "title": name + "（用途待确认）",
                    "purpose": {"description": f"Observed {desc['type']}.{name}; business purpose is unconfirmed", "basis": "inferred"},
                    "effect": "unknown", "input_schema": {"type": "object", "properties": fields, "required": list(fields), "additionalProperties": False},
                    "output_schema": {}, "binding": binding, "tests": [],
                    "discovery": {"type": desc["type"], "observation_only": bool(desc.get("observation_only")), "member": member}})
                if len(capabilities) >= 256:
                    break
            if len(capabilities) >= 256:
                break
        if len(capabilities) >= 256:
            break
    manifest = {"format": FORMAT, "app": {"id": app_id, "name": app_id},
                "provider": {"config": os.path.relpath(config, output.parent), "target": target}, "capabilities": capabilities}
    validate_manifest(manifest)
    atomic_json(output, manifest)
    total = len(desc["methods"]) + sum(1 + bool(p.get("writable")) for p in desc["properties"])
    return {"path": str(output), "candidates": len(capabilities), "property_reads": 0, "method_calls": 0,
            "root_member_candidates": total, "truncated": len(capabilities) < total,
            "provider_start_may_launch_application": True, "automatically_registered": False, "semantic_truth_proven": False}
