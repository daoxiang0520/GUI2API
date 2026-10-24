"""Cross-runtime integration check on owned objects, with no desktop inputs."""
from __future__ import annotations

import json
import os
from pathlib import Path
import statistics
import queue
import subprocess
import sys
import threading
import time
import uuid

from object_client import ObjectSession

ROOT = Path(__file__).resolve().parent


def ok(result):
    assert result.get("ok"), result
    return result["data"]


def scalar(result):
    data = ok(result)
    assert data["result"]["kind"] == "value", data
    return data["result"]["value"]


def check(session, target):
    started = time.perf_counter()
    roots = ok(session.request("list", {"target": target}))["objects"]
    startup = round((time.perf_counter() - started) * 1000, 3)
    assert len(roots) == 2 and roots[0]["ref"] == roots[1]["ref"]
    ref = roots[0]["ref"]
    desc = ok(session.request("describe", {"ref": ref}))
    assert {"count", "label"} <= {p["name"] for p in desc["properties"]}
    assert "changed" in {e["name"] for e in desc["events"]}

    def method(name):
        found = [m for m in desc["methods"] if m["name"] == name or m.get("member") == name]
        assert len(found) == 1, (name, desc)
        return found[0]["name"]

    literal = '对象调用 " literal ; process.exit(1); $(hello) `中文`\nsecond line'
    set_result = ok(session.request("set", {"ref": ref, "member": "label", "value": literal}))
    assert set_result["status"] == "verified"
    assert scalar(session.request("get", {"ref": ref, "member": "label"})) == literal
    assert scalar(session.request("call", {"ref": ref, "member": method("echo"), "arguments": [literal]})) == literal
    ok(session.request("watch", {"ref": ref, "names": ["changed"]}))
    assert scalar(session.request("call", {"ref": ref, "member": method("add"), "arguments": [3]})) == 3
    assert scalar(session.request("get", {"ref": ref, "member": "count"})) == 3
    events = ok(session.request("events", {"target": target, "timeout_ms": 100}))
    assert len(events["events"]) == 1 and events["events"][0]["object"]["ref"] == ref
    assert events["events"][0]["name"] == "changed" and not events["values_in_events"]
    result = {"startup_ms": startup, "type": desc["type"], "properties": len(desc["properties"]), "methods": len(desc["methods"]), "events": len(desc["events"]), "same_object_identity": True, "literal_roundtrip": True, "method_state_readback": True, "business_event": True}
    if target != "qt-demo":
        # Metadata enumeration must not call even an explicitly side-effectful getter.
        ok(session.request("describe", {"ref": ref}))
        assert scalar(session.request("get", {"ref": ref, "member": "probe"})) == 1
        child = ok(session.request("call", {"ref": ref, "member": method("child")}))["result"]
        assert child["kind"] == "object"
        child_ref = child["ref"]
        repeated = ok(session.request("call", {"ref": ref, "member": method("child")}))["result"]
        assert child_ref == repeated["ref"]
        assert scalar(session.request("call", {"ref": ref, "member": method("same_child"), "arguments": [None], "object_arguments": {"0": child_ref}})) is True
        ok(session.request("describe", {"ref": child_ref}))
        assert scalar(session.request("get", {"ref": child_ref, "member": "label"})) == "子对象"
        ok(session.request("release", {"ref": child_ref}))
        stale = session.request("get", {"ref": child_ref, "member": "label"})
        assert stale["error"]["code"] == "stale_ref"
        next_child = ok(session.request("call", {"ref": ref, "member": method("child")}))["result"]
        assert next_child["ref"] != child_ref
        failed = session.request("call", {"ref": ref, "member": method("fail_after_add")})
        assert failed["error"]["code"] == "application_exception" and failed["error"]["operation_may_have_executed"]
        assert scalar(session.request("get", {"ref": ref, "member": "count"})) == 4
        result.update(describe_did_not_execute_getters=True, child_identity_and_object_arguments=True, released_reference_rejected=True, exception_did_not_retry=True)
    private = session.request("call", {"ref": ref, "member": "__class__", "arguments": []})
    assert private["error"]["code"] == "unknown_member"
    ok(session.request("watch", {"ref": ref, "names": ["changed"], "enabled": False}))
    ok(session.request("call", {"ref": ref, "member": method("add"), "arguments": [1]}))
    assert not ok(session.request("events", {"target": target}))["events"]
    samples = []
    for _ in range(12):
        start = time.perf_counter()
        scalar(session.request("get", {"ref": ref, "member": "count"}))
        samples.append((time.perf_counter() - start) * 1000)
    result["warm_property_read_median_ms"] = round(statistics.median(samples), 3)
    return result


def check_com(session):
    start = time.perf_counter()
    roots = ok(session.request("list", {"target": "com-demo"}))["objects"]
    ref = roots[0]["ref"]
    desc = ok(session.request("describe", {"ref": ref}))
    assert desc["type"] == "IDictionary"
    assert {"Add", "Item@get", "Item@put", "Exists"} <= {m["name"] for m in desc["methods"]}
    ok(session.request("set", {"ref": ref, "member": "CompareMode", "value": 1}))
    assert scalar(session.request("get", {"ref": ref, "member": "CompareMode"})) == 1
    text = "COM 业务对象：literal $(hello)"
    ok(session.request("call", {"ref": ref, "member": "Add", "arguments": ["probe", text]}))
    assert scalar(session.request("get", {"ref": ref, "member": "Count"})) == 1
    assert scalar(session.request("call", {"ref": ref, "member": "Exists", "arguments": ["probe"]})) is True
    assert scalar(session.request("call", {"ref": ref, "member": "Item@get", "arguments": ["probe"]})) == text
    ok(session.request("call", {"ref": ref, "member": "Item@put", "arguments": ["probe", "changed"]}))
    assert scalar(session.request("call", {"ref": ref, "member": "Item@get", "arguments": ["probe"]})) == "changed"
    assert scalar(session.request("call", {"ref": ref, "member": "Keys"})) == ["probe"]
    # A real COM IDispatch child returned by Invoke must retain native identity.
    ok(session.request('call', {'ref': ref, 'member': 'Add', 'arguments': ['self', None], 'object_arguments': {'1': ref}}))
    try:
        child = ok(session.request('call', {'ref': ref, 'member': 'Item@get', 'arguments': ['self']}))['result']
        assert child['kind'] == 'object' and child['ref'] == ref
        assert scalar(session.request('get', {'ref': child['ref'], 'member': 'Count'})) == 2
    finally:
        ok(session.request('call', {'ref': ref, 'member': 'Remove', 'arguments': ['self']}))
    readonly = session.request("set", {"ref": ref, "member": "Count", "value": 2})
    assert readonly["error"]["code"] == "unsupported"
    return {"type": desc["type"], "properties": len(desc["properties"]), "methods": len(desc["methods"]), "actual_com_idispatch": True, "indexed_property_calls": True, "method_state_readback": True, "property_write_readback": True, "com_returned_object_identity": True, "readonly_property_rejected": True, "connection_point_events": "not_implemented", "total_ms": round((time.perf_counter() - start) * 1000, 3)}


def check_running_com(session):
    """Register only an owned object, then discover and call it cross-process."""
    ready = queue.Queue()
    stop = threading.Event()

    def owner():
        import hashlib
        import pythoncom
        import win32com.client
        pythoncom.CoInitialize()
        cookie = None
        try:
            obj = win32com.client.Dispatch("Scripting.Dictionary")
            obj.Add("owned-marker", "AppCtl test object")
            rot = pythoncom.GetRunningObjectTable()
            moniker = pythoncom.CreateItemMoniker("!", "AppCtlOwnedObject-" + uuid.uuid4().hex)
            cookie = rot.Register(1, obj._oleobj_, moniker)
            name = moniker.GetDisplayName(pythoncom.CreateBindCtx(0), None)
            ready.put((hashlib.sha256(name.encode("utf-8")).hexdigest()[:24], None))
            while not stop.wait(.005):
                pythoncom.PumpWaitingMessages()
        except Exception as exc:
            ready.put((None, str(exc)))
        finally:
            if cookie is not None:
                rot.Revoke(cookie)
            # Release apartment-bound COM wrappers before uninitializing their
            # apartment. Keeping these locals alive until function return emits
            # a pywin32 IUnknown-release warning after CoUninitialize.
            obj = moniker = rot = None
            import gc
            gc.collect()
            pythoncom.CoUninitialize()

    thread = threading.Thread(target=owner, daemon=True)
    thread.start()
    target = None
    try:
        rot_id, failure = ready.get(timeout=5)
        assert failure is None, failure
        discovered = ok(session.request("discover", {"runtime": "com"}))
        assert any(row["rot_id"] == rot_id for row in discovered["objects"]), discovered
        attached = ok(session.request("attach", {"rot_id": rot_id}))
        target = attached["target"]
        ref = attached["objects"][0]["ref"]
        assert ok(session.request("describe", {"ref": ref}))["type"] == "IDictionary"
        assert scalar(session.request("get", {"ref": ref, "member": "Count"})) == 1
        ok(session.request("call", {"ref": ref, "member": "Add", "arguments": ["second", "cross-process object call"]}))
        assert scalar(session.request("get", {"ref": ref, "member": "Count"})) == 2
        return {"discovered": True, "attached": True, "cross_process_method_readback": True, "existing_user_apps_modified": 0, "owned_registration_revoked_in_finally": True}
    finally:
        if target:
            session.providers[target].close()
        stop.set()
        thread.join(timeout=3)
        assert not thread.is_alive(), "Owned ROT registration thread did not stop"


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    report = {"ok": False, "protocol": "appctl.objects/1", "screenshots": 0, "desktop_inputs": 0, "personal_apps_modified": 0, "runtime_results": {}, "exploration_results": {}, "universal_unmodified_app_coverage": False}
    try:
        with ObjectSession(ROOT / "objects.example.json") as session:
            targets = ok(session.request("targets"))["targets"]
            assert len(targets) == 5 and all(not t["connected"] for t in targets)
            for target in targets:
                name = target["target"]
                report["runtime_results"][name] = check_com(session) if name == "com-demo" else check(session, name)
                graph = ok(session.request("explore", {"target": name, "query": "count"}))
                assert graph["entry_found"] and graph["statistics"]["descriptions"] == 1, graph
                assert graph["statistics"]["property_reads"] == graph["statistics"]["declared_read_calls"] == 0
                assert graph["matches"] and not graph["errors"] and not graph["stopped_by"], graph
                report["exploration_results"][name] = {"metadata_graph": True, "keyword_matches": len(graph["matches"]), "properties_read": 0, "methods_called": 0}
                print(json.dumps({"runtime": name, "passed": True}, ensure_ascii=False), flush=True)
            report["running_com_discovery"] = session.request("discover", {"runtime": "com"})
            report["running_com_metadata_checks"] = []
            for row in report["running_com_discovery"].get("data", {}).get("objects", [])[:2]:
                attached = session.request("attach", {"rot_id": row["rot_id"]})
                item = {"type": row["type"], "attached": attached.get("ok"), "business_calls": 0}
                if attached.get("ok"):
                    ref = attached["data"]["objects"][0]["ref"]
                    desc = session.request("describe", {"ref": ref})
                    item["metadata_read"] = desc.get("ok")
                    if desc.get("ok"):
                        item["methods"] = len(desc["data"]["methods"])
                        item["properties"] = len(desc["data"]["properties"])
                report["running_com_metadata_checks"].append(item)
            report["owned_running_com_attach"] = check_running_com(session)
        # Exercise the actual CLI JSON-lines path, including invalid JSON recovery.
        wire = '\n'.join(['not json', json.dumps({"op": "targets"}), json.dumps({"op": "list", "args": {"target": "python-demo"}}), json.dumps({"op": "explore", "args": {"target": "python-demo", "call_read_methods": True}})]) + '\n'
        process = subprocess.run([sys.executable, str(ROOT / "appctl.py"), "objects", "--config", str(ROOT / "objects.example.json"), "session"], input=wire, capture_output=True, text=True, encoding="utf-8", timeout=20, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        replies = [json.loads(line) for line in process.stdout.splitlines()]
        assert process.returncode == 0 and len(replies) == 4 and not replies[0]["ok"] and all(r["ok"] for r in replies[1:]), (process.stdout, process.stderr)
        assert replies[3]["data"]["statistics"]["objects"] == 2
        report["cli_json_lines"] = True
        temp_dir = ROOT / "tests/runs" / ("object-pytest-" + uuid.uuid4().hex[:10])
        environment = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
        unit = subprocess.run([sys.executable, "-m", "pytest", "-q", "--tb=short", "--basetemp", str(temp_dir), "tests/test_objects.py", "tests/test_object_explorer.py", "tests/test_native_observations.py", "tests/test_com_metadata.py", "tests/test_capability_registry.py", "tests/test_appctl.py", "tests/test_router.py", "tests/test_messenger_runtime.py", "tests/test_gui_api.py", "tests/test_gui_controls.py"], cwd=ROOT, env=environment, capture_output=True, text=True, encoding="utf-8", timeout=45, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        assert unit.returncode == 0, unit.stdout[-7000:] + unit.stderr[-2000:]
        report["unit_tests"] = unit.stdout.strip().splitlines()[-1]
        mcp = subprocess.run([sys.executable, "tests/smoke_object_mcp.py"], cwd=ROOT, env=environment, capture_output=True, text=True, encoding="utf-8", timeout=45, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        assert mcp.returncode == 0, mcp.stdout[-3000:] + mcp.stderr[-3000:]
        report["mcp"] = json.loads(mcp.stdout.strip().splitlines()[-1])
        report["ok"] = True
    except Exception as exc:
        report["error"] = str(exc)
        raise
    finally:
        (ROOT / "object-validation.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    if "--com-check" in sys.argv:
        with ObjectSession() as session:
            result = check_running_com(session)
        report_path = ROOT / "object-validation.json"
        if report_path.exists():
            report = json.loads(report_path.read_text(encoding="utf-8"))
            report["owned_running_com_cleanup_check"] = result
            report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(result))
    else:
        main()
