"""Validate QQ's newly exposed control metadata without reading chat bodies.

Only an exactly labelled, initially empty Search edit may be exercised. It is
read back and restored; no composer or Send button is ever used.
"""
import json
from contextlib import ExitStack
from pathlib import Path
import time

from desktop import DesktopSession

ROOT = Path(__file__).resolve().parent
SEARCH_NAMES = {"搜索", "搜索联系人", "搜索联系人、群聊", "搜索联系人/群聊", "搜索好友", "Search"}
KNOWN_WORDS = ("搜索", "发送", "输入", "消息", "联系人", "聊天")


def main():
    report = {"screenshots": 0, "message_sends": 0, "chat_bodies_saved": False, "search_test": "not_attempted"}
    with ExitStack() as lifetime:
        desktop=lifetime.enter_context(DesktopSession())
        apps = desktop.request("apps")["data"]["windows"]
        windows = [a for a in apps if a["app"].lower() == "qq"]
        if len(windows) != 1:
            raise AssertionError("QQ must have exactly one selected top-level window")
        window = windows[0]["window"]
        state = desktop.request("window", {"window": window})["data"]
        report["window_before"] = state
        (ROOT / "qq-control-validation.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
        restore_minimized = bool(state.get("minimized"))
        if restore_minimized:
            def restore():
                report["minimized_restored"] = desktop.request("window", {"window":window,"state":"minimize","pid":state["pid"]}).get("data",{}).get("status")=="verified"
            lifetime.callback(restore)
            report["restore_result"] = desktop.request("window", {"window": window, "state": "restore"}).get("data")
            time.sleep(.4)

        def observe():
            observed = desktop.request("inspect", {"window": window, "backend": "auto", "depth": 8, "limit": 250, "actionable": False, "probe_timeout": 8})
            if not observed.get("ok"):
                report["observation_error"] = {"error":observed.get("error"),"channels":observed.get("data",{}).get("channels",[])}
                return {"elements":[],"quality":{"nodes":0}}
            return observed["data"]

        # Probe the shared protocols first so a lazily enabled accessibility
        # bridge has a chance to respond before the bounded fresh observation.
        desktop.request("probe", {"window": window, "depth": 8, "limit": 300})
        data = observe()
        rows = data["elements"]
        report["quality"] = data["quality"]
        # Save only recognized UI words and capabilities, never actual names.
        report["recognized_controls"] = [{"words": [w for w in KNOWN_WORDS if w in r["name"]], "role": r["role"], "actions": r["actions"], "exact_search_label": r["name"] in SEARCH_NAMES} for r in rows if any(w in r["name"] for w in KNOWN_WORDS) and r["role"] in ("Edit", "Button", "ComboBox")]
        report["editable_controls"] = [{"role": r["role"], "recognized_words": [w for w in KNOWN_WORDS if w in r["name"]], "exact_search_label": r["name"] in SEARCH_NAMES} for r in rows if "set" in r["actions"]]
        candidates = [r for r in rows if r["name"] in SEARCH_NAMES and r["role"] == "Edit" and "set" in r["actions"] and r.get("enabled")]
        if len(candidates) == 1:
            target = candidates[0]["ref"]
            initial = desktop.request("get", {"ref": target})
            if initial.get("ok") and initial["data"].get("source") in ("UIA3.ValuePattern","ValuePattern","IAccessible.accValue") and initial["data"].get("value") == "":
                probe = "appctl-local-search-probe"
                try:
                    written = desktop.request("set", {"ref": target, "value": probe})
                    read = desktop.request("get", {"ref": target})
                    report["search_test"] = "verified" if written.get("ok") and read.get("ok") and read["data"].get("value") == probe else "not_verified"
                finally:
                    fresh = observe()
                    matching = [r for r in fresh["elements"] if r["name"] in SEARCH_NAMES and r["role"] == "Edit" and "set" in r["actions"]]
                    if len(matching) == 1:
                        current = desktop.request("get", {"ref": matching[0]["ref"]})
                        if current.get("ok") and current["data"].get("value") == probe:
                            restored = desktop.request("set", {"ref": matching[0]["ref"], "value": ""})
                            report["search_restored"] = restored.get("ok") and restored.get("data", {}).get("status") == "verified"
                        else:
                            report["search_restored"] = current.get("ok") and current["data"].get("value") == ""
                            report["concurrent_change_detected"] = not report["search_restored"]
                    else:
                        report["search_restored"] = False
            else:
                report["search_test"] = "skipped_nonempty_or_no_value_pattern"
        else:
            report["search_test"] = "skipped_no_unique_exact_search_edit"
    (ROOT / "qq-control-validation.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
