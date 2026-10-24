"""Inspect messenger accessibility counts without sending application input."""
import json
from collections import Counter
from pathlib import Path
import time

from appctl import UiaSession


def require(result):
    if not result.get("ok"):
        raise RuntimeError(json.dumps(result, ensure_ascii=False))
    return result["data"]


def main():
    report = {"checked_at_unix": time.time(), "screenshots": False, "message_or_text_input_commands": 0, "samples": []}
    with UiaSession(timeout=8) as enumeration:
        windows = require(enumeration.request("apps"))["windows"]
    for app, title in (("QQ", "QQ"), ("Weixin", "微信")):
        with UiaSession(timeout=8) as session:
            matches = [w for w in windows if w["app"].lower() == app.lower() and w["title"] == title]
            if len(matches) != 1:
                report["samples"].append({"app": app, "status": "not_sampled", "matching_windows": len(matches)})
                continue
            target = {"window": matches[0]["window"], "pid": matches[0]["pid"]}
            original = require(session.request("window", target))
            sample = {"app": app, "window": target["window"], "original_minimized": original["minimized"], "views": []}
            try:
                if original["minimized"]:
                    restored = require(session.request("window", dict(target, state="restore")))
                    if restored["minimized"]:
                        raise RuntimeError("Could not restore target window for the accessibility probe")
                for view in ("control", "raw"):
                    if session.process.poll() is not None:
                        session.close()
                        session = UiaSession(timeout=8)
                    result = session.request("inspect", {"window": target["window"], "view": view, "depth": 8, "limit": 200, "actionable": False})
                    if not result.get("ok"):
                        sample["views"].append({"view": view, "error": result.get("error")})
                        print(json.dumps({"app": app, "view": view, "error": result.get("error")}), flush=True)
                        continue
                    tree = result["data"]
                    nodes = tree["elements"]
                    sample["views"].append({
                        "view": view, "visited": tree["visited"], "truncated": tree["truncated"],
                        "named_nodes": sum(bool(n["name"]) for n in nodes),
                        "nodes_with_semantic_actions": sum(any(a != "get" for a in n["actions"]) for n in nodes),
                        "settable_nodes": sum("set" in n["actions"] for n in nodes),
                        "roles": dict(Counter(n["role"] for n in nodes)),
                        "roundtrip_ms": result["roundtrip_ms"],
                    })
                    print(json.dumps({"app": app, **sample["views"][-1]}), flush=True)
            finally:
                if original["minimized"]:
                    if session.process.poll() is None:
                        state = require(session.request("window", dict(target, state="minimize")))
                    else:
                        with UiaSession() as recovery:
                            state = require(recovery.request("window", dict(target, state="minimize")))
                    sample["original_window_state_restored"] = state["minimized"]
                    if not state["minimized"]:
                        raise RuntimeError("Could not verify original window state restoration")
                session.close()
            report["samples"].append(sample)
    path = Path(__file__).with_name("messenger-accessibility.json")
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
