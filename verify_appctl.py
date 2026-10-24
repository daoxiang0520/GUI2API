"""Bounded desktop validation. --exercise writes only to the empty Bot search box."""
import argparse
import json
from pathlib import Path
import statistics
import time

from appctl import UiaSession


def require(result):
    if not result.get("ok"):
        raise RuntimeError(json.dumps(result, ensure_ascii=False))
    return result["data"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exercise", action="store_true")
    exercise = parser.parse_args().exercise
    report = {"screenshots": False, "exercise": exercise}
    with UiaSession(timeout=20) as session:
        report["startup_ms"] = session.startup_ms
        windows = require(session.request("apps"))["windows"]
        matches = [w for w in windows if w["app"].lower() == "explorer" and w["title"] == "Bot - 文件资源管理器"]
        if len(matches) != 1:
            raise RuntimeError("Expected exactly one Bot Explorer window")
        window = matches[0]["window"]
        scope = {"window": window, "within": "FileExplorerSearchBox", "depth": 3, "limit": 30, "actionable": False, "values": True}
        inspected = session.request("inspect", scope)
        state = require(inspected)
        fields = [e for e in state["elements"] if e["automation_id"] == "TextBox" and e["role"] == "Edit"]
        if len(fields) != 1:
            raise RuntimeError("Search field was not uniquely discoverable")
        target = {"ref": fields[0]["ref"]}
        original = require(session.request("get", target))
        report["scoped_inspect_roundtrip_ms"] = inspected["roundtrip_ms"]
        report["scoped_nodes"] = state["visited"]
        timings = []
        for _ in range(10):
            result = session.request("get", target)
            require(result)
            timings.append(result["roundtrip_ms"])
        report["get_roundtrip_ms"] = {"median": statistics.median(timings), "min": min(timings), "max": max(timings), "samples": timings}
        delta = session.request("inspect", dict(scope, since=state["snapshot"]))
        diff = require(delta)
        report["delta_roundtrip_ms"] = delta["roundtrip_ms"]
        report["unchanged_delta"] = not (diff["added"] or diff["changed"] or diff["removed"])
        unsupported = session.request("invoke", target)
        report["unsupported_is_explicit"] = not unsupported["ok"] and unsupported["error"]["code"] == "unsupported"
        stale = session.request("get", {"ref": "invalid-ref"})
        report["stale_ref_is_explicit"] = not stale["ok"] and stale["error"]["code"] == "stale_ref"
        wrong_scope = session.request("inspect", dict(scope, depth=2, since=state["snapshot"]))
        report["incompatible_snapshot_rejected"] = not wrong_scope["ok"] and wrong_scope["error"]["code"] == "unknown_snapshot"
        if exercise:
            if original["source"] != "ValuePattern" or original["value"] != "":
                raise RuntimeError("Search field is not empty; refusing to alter it")
            report["refresh"] = require(session.request("invoke", {"window": window, "automation_id": "refreshButton"}))
            probe = "appctl-cli-probe"
            try:
                result = session.request("set", dict(target, value=probe))
                set_result = require(result)
                assert set_result["status"] == "verified"
                report["set_roundtrip_ms"] = result["roundtrip_ms"]
                assert require(session.request("get", target))["value"] == probe
                change = require(session.request("inspect", dict(scope, since=state["snapshot"])))
                report["value_change_detected"] = any(row.get("value") == probe for row in change["changed"])
                if not report["value_change_detected"]:
                    raise RuntimeError("Expected text change was absent from delta")
            finally:
                restore = require(session.request("set", dict(target, value=original["value"])))
                report["original_restored"] = restore["status"] == "verified" and require(session.request("get", target))["value"] == original["value"]
                if not report["original_restored"]:
                    raise RuntimeError("Could not verify restoration")
        assert report["unsupported_is_explicit"] and report["stale_ref_is_explicit"] and report["incompatible_snapshot_rejected"]
    report["checked_at_unix"] = time.time()
    output = Path(__file__).with_name("appctl-validation.json")
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
