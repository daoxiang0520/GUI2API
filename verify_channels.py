"""Exercise actual channels using owned, disposable native and browser fixtures.

Existing user applications are only enumerated to locate the installed browser.
No messages, user documents, or personal browser profiles are modified.
"""
from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import statistics
import subprocess
import time
import uuid

from desktop import DesktopSession

ROOT = Path(__file__).resolve().parent


def ok(result):
    if not result.get("ok"):
        raise AssertionError(json.dumps(result, ensure_ascii=False))
    return result["data"]


def unique(rows, name, action):
    found = [r for r in rows if r["name"] == name and action in r["actions"]]
    if len(found) != 1:
        raise AssertionError(f"Expected one {name}/{action}, found {len(found)}")
    return found[0]["ref"]


def read_state(path):
    for _ in range(30):
        try:
            return json.loads(path.read_text(encoding="utf-8-sig"))
        except (FileNotFoundError, ValueError):
            time.sleep(.05)
    raise AssertionError("Fixture state not available")


def main():
    # Recover only our own prior fixture windows if an interrupted build/run left
    # one behind. Verify executable location before posting a close message.
    with DesktopSession() as cleanup:
        for saved in (ROOT / "tests/runs").glob("*/state.json"):
            prior = read_state(saved)
            info = cleanup.request("describe", {"window": prior["window"]})
            if not info.get("ok"):
                continue
            data = info["data"]
            exe = Path(data.get("exe", ""))
            if data["pid"] == prior["pid"] and exe.parent == ROOT / "tests/fixtures" and exe.name.startswith("NativeFixture-"):
                ctypes.windll.user32.PostMessageW(ctypes.c_void_p(prior["window"]), 16, 0, 0)
    run = ROOT / "tests/runs" / uuid.uuid4().hex[:10]
    run.mkdir(parents=True)
    state_path = run / "state.json"
    ps = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    built = subprocess.run([str(ps), "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(ROOT / "tests/fixtures/Build-NativeFixture.ps1")], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
    if built.returncode:
        raise AssertionError(built.stderr)
    executable = built.stdout.strip().splitlines()[-1]
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = 0
    native_process = subprocess.Popen([executable, str(state_path)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW, startupinfo=startup)
    report = {"screenshots": 0, "existing_app_input_commands": 0, "native": {}, "cdp": {}}
    state = read_state(state_path)
    window, fixture_pid = state["window"], state["pid"]
    browser_process = None
    try:
        with DesktopSession() as session:
            observed = {}
            for backend in ("win32", "uia3", "uia", "msaa"):
                result = session.request("inspect", {"window": window, "backend": backend, "depth": 9, "limit": 300, "actionable": False})
                data = ok(result)
                observed[backend] = data["elements"]
                report["native"][backend] = {"quality": data["quality"], "observation_ms": result["roundtrip_ms"]}
            for backend in ("uia3","msaa"):
                scope_rows=observed[backend]
                if backend=="msaa":
                    scope_rows=[r for r in scope_rows if r["role"]=="Pane" and r["parent"] is not None]
                canvas=unique(scope_rows,"测试自绘画布","get")
                scoped=ok(session.request("inspect",{"root_ref":canvas,"depth":4,"limit":30,"actionable":False}))
                assert scoped["elements"] and all(r["backend"]==backend for r in scoped["elements"])
                expected="自绘计数增加" if backend=="uia3" else "自绘输入槽"
                assert any(r["name"]==expected for r in scoped["elements"]),json.dumps({"backend":backend,"expected":expected,"scoped":[{"name":r["name"],"role":r["role"],"parent":r["parent"]} for r in scoped["elements"]],"full_custom":[{"name":r["name"],"role":r["role"],"parent":r["parent"]} for r in observed[backend] if "自绘" in r["name"]]},ensure_ascii=False)
                report["native"][backend]["subtree_verified"]=True
            watch = ok(session.request("watch", {"window": window, "backend": "win32"}))
            edit = unique(observed["win32"], "", "set")
            value = "通用 Win32 API 验证"
            assert ok(session.request("set", {"ref": edit, "value": value}))["status"] == "verified"
            assert ok(session.request("get", {"ref": edit}))["value"] == value
            assert read_state(state_path)["standard_text"] == value
            assert ok(session.request("set", {"ref": edit, "value": ""}))["status"] == "verified"
            button = unique(observed["win32"], "计数增加", "invoke")
            before = read_state(state_path)["count"]
            ok(session.request("invoke", {"ref": button}))
            assert read_state(state_path)["count"] == before + 1
            events = ok(session.request("events", {"timeout_ms": 1000}))
            assert events["invalidated"] and events["events"]
            report["native"]["win32"].update(set_readback_verified=True, invoked_counter_verified=True, event_count=len(events["events"]), event_source=watch["source"])
            custom = unique(observed["msaa"], "自绘输入槽", "set")
            assert ok(session.request("set", {"ref": custom, "value": "自绘也有对象"}))["status"] == "verified"
            assert ok(session.request("get", {"ref": custom}))["value"] == "自绘也有对象"
            assert read_state(state_path)["custom_text"] == "自绘也有对象"
            assert ok(session.request("set", {"ref": custom, "value": ""}))["status"] == "verified"
            custom_button = unique(observed["msaa"], "自绘计数增加", "invoke")
            before = read_state(state_path)["count"]
            ok(session.request("invoke", {"ref": custom_button}))
            assert read_state(state_path)["count"] == before + 1
            custom_check = unique(observed["msaa"], "自绘复选框", "toggle-on")
            assert ok(session.request("toggle-on", {"ref": custom_check}))["status"] == "verified"
            assert read_state(state_path)["custom_checked"]
            assert ok(session.request("toggle-off", {"ref": custom_check}))["status"] == "verified"
            report["native"]["msaa"].update(custom_set_readback_verified=True, custom_counter_verified=True, custom_toggle_verified=True)
            edit = unique(observed["uia"], "标准输入框", "set")
            assert ok(session.request("set", {"ref": edit, "value": "UIA 验证"}))["status"] == "verified"
            assert read_state(state_path)["standard_text"] == "UIA 验证"
            assert ok(session.request("set", {"ref": edit, "value": ""}))["status"] == "verified"
            timings = [session.request("get", {"ref": edit})["roundtrip_ms"] for _ in range(10)]
            report["native"]["uia"].update(set_readback_verified=True, warm_get_median_ms=statistics.median(timings))
            native_edit = unique(observed["uia3"], "标准输入框", "set")
            assert ok(session.request("set", {"ref": native_edit, "value": "原生 COM 验证"}))["status"] == "verified"
            assert read_state(state_path)["standard_text"] == "原生 COM 验证"
            assert ok(session.request("set", {"ref": native_edit, "value": ""}))["status"] == "verified"
            native_button = unique(observed["uia3"], "自绘计数增加", "invoke")
            before = read_state(state_path)["count"]
            ok(session.request("invoke", {"ref": native_button}))
            assert read_state(state_path)["count"] == before + 1
            report["native"]["uia3"].update(set_readback_verified=True, custom_counter_verified=True)
            uia_watch = ok(session.request("watch", {"window": window, "backend": "uia"}))
            assert ok(session.request("set", {"ref": edit, "value": "事件验证"}))["status"] == "verified"
            uia_events = ok(session.request("events", {"timeout_ms": 1500}))
            assert uia_events["invalidated"]
            assert ok(session.request("set", {"ref": edit, "value": ""}))["status"] == "verified"
            report["native"]["uia"].update(event_count=len(uia_events["events"]), event_source=uia_watch["source"])
            protected = [r for rows in observed.values() for r in rows if r.get("password")]
            assert protected
            assert all(session.request("get", {"ref": r["ref"]}).get("error", {}).get("code") == "protected_control" for r in protected)
            report["native"]["password_controls_blocked"] = len(protected)
            assert session.request("invoke", {"ref": "not-a-ref"})["error"]["code"] == "stale_ref"
            auto = ok(session.request("inspect", {"window": window, "actionable": False, "limit": 300, "depth": 9}))
            report["native"]["auto"] = {"quality": auto["quality"], "backends": sorted({r["backend"] for r in auto["elements"]})}
            print(json.dumps({"stage": "native", "result": report["native"]}, ensure_ascii=False), flush=True)

            apps = ok(session.request("apps"))["windows"]
            browser = next((a for a in apps if a["app"].lower() in ("chrome", "msedge")), None)
            if browser is None:
                raise AssertionError("No installed Chromium browser was discovered")
            executable = ok(session.request("describe", {"window": browser["window"]}))["exe"]
            profile = run / "browser-profile"
            browser_process = subprocess.Popen([executable, "--headless=new", "--remote-debugging-address=127.0.0.1", "--remote-debugging-port=0", "--user-data-dir=" + str(profile), "--no-first-run", "--disable-extensions", "--disable-background-networking", "--disable-component-update", (ROOT / "tests/fixtures/web.html").as_uri()], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
            port_file = profile / "DevToolsActivePort"
            for _ in range(100):
                if port_file.exists():
                    break
                time.sleep(.1)
            port = int(port_file.read_text().splitlines()[0])
            endpoint = f"http://127.0.0.1:{port}"
            page_targets = ok(session.request("targets", {"endpoint": endpoint}))["targets"]
            assert len(page_targets) == 1
            context = {"endpoint": endpoint, "target": page_targets[0]["id"], "backend": "cdp"}
            data = ok(session.request("inspect", {**context, "depth": 12, "limit": 200, "actionable": False}))
            query = unique(data["elements"], "搜索内容", "set")
            sanitized = session.request("set", {"ref": query, "value": '文本含引号 " 与换行\n'})
            assert not sanitized["ok"] and sanitized["error"]["code"] == "verification_failed"
            literal = '通用 DOM API，包含 "引号" 和 ${literal}'
            assert ok(session.request("set", {"ref": query, "value": literal}))["status"] == "verified"
            assert ok(session.request("get", {"ref": query}))["value"] == literal
            ok(session.request("watch", context))
            increment = unique(data["elements"], "计数增加", "invoke")
            ok(session.request("invoke", {"ref": increment}))
            assert ok(session.request("get", {**context, "css": "#count"}))["value"] == "1"
            cdp_events = ok(session.request("events", {"timeout_ms": 1500}))
            assert cdp_events["invalidated"]
            checkbox = unique(data["elements"], "测试复选框", "toggle-on")
            assert ok(session.request("toggle-on", {"ref": checkbox}))["status"] == "verified"
            assert ok(session.request("toggle-off", {"ref": checkbox}))["status"] == "verified"
            protected = [r for r in data["elements"] if r.get("password")]
            assert len(protected) == 1
            assert session.request("get", {"ref": protected[0]["ref"]})["error"]["code"] == "protected_control"
            canvas = next(r for r in data["elements"] if r["name"] == "纯画布示例，未暴露内部按钮对象")
            assert "invoke" not in canvas["actions"] and "set" not in canvas["actions"]
            conn = next(iter(session.cdp.connections.values()))
            conn.close()
            assert session.request("get", {"ref": query})["error"]["code"] == "stale_ref"
            report["cdp"] = {"fixture": "isolated headless Chromium with its own profile", "quality": data["quality"], "set_readback_verified": True, "sanitization_detected_without_retry": True, "counter_verified": True, "toggle_verified": True, "password_control_blocked": True, "reconnection_rejects_stale_ref": True, "event_count": len(cdp_events["events"]), "pixel_only_canvas_has_no_button_semantics": True}
            print(json.dumps({"stage": "cdp", "result": report["cdp"]}, ensure_ascii=False), flush=True)
    finally:
        if browser_process and browser_process.poll() is None:
            browser_process.terminate()
            browser_process.wait(timeout=10)
        actual_pid = ctypes.c_ulong()
        ctypes.windll.user32.GetWindowThreadProcessId(ctypes.c_void_p(window), ctypes.byref(actual_pid))
        if actual_pid.value == fixture_pid:
            ctypes.windll.user32.PostMessageW(ctypes.c_void_p(window), 16, 0, 0)
        try:
            native_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            native_process.terminate()
            native_process.wait(timeout=5)
    (ROOT / "channel-validation.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
