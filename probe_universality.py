"""Bounded cross-application API experiments, with explicit evidence levels.

Inventory never changes application state. Execution experiments use owned
scratch documents/profiles or read-only methods; their scope is recorded.
"""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import hashlib
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import statistics
import subprocess
import socket
import sys
import time
import uuid
import winreg

import psutil

ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "reports" / "universality-20261023"
REPORT = OUTPUT / "inventory.json"


def archive_previous(filename):
    path = OUTPUT / filename
    if path.exists():
        archived = OUTPUT / "attempts" / (path.stem + "-" + uuid.uuid4().hex[:8] + ".json")
        archived.parent.mkdir(parents=True, exist_ok=True)
        archived.write_bytes(path.read_bytes())


def implementation(path):
    return {"path": path.relative_to(ROOT).as_posix(), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def process_ids(executable):
    return {p.pid: p.info.get("create_time") for p in psutil.process_iter(["name", "create_time"])
            if (p.info.get("name") or "").casefold() == executable.casefold()}


class ProtocolCheck:
    def __init__(self, session, report, filename):
        self.session, self.report, self.filename = session, report, filename

    def request(self, op, **args):
        started = time.perf_counter()
        result = self.session.request(op, args)
        entry = {"op": op, "member": args.get("member"), "ok": result.get("ok"),
                 "elapsed_ms": round((time.perf_counter() - started) * 1000, 3)}
        if not result.get("ok"):
            entry["error"] = result.get("error")
        self.report.setdefault("requests", []).append(entry)
        save(self.filename, self.report)
        if not result.get("ok"):
            raise RuntimeError(json.dumps(result.get("error"), ensure_ascii=False))
        return result["data"]

    def get(self, ref, member):
        result = self.request("get", ref=ref, member=member)["result"]
        return result["ref"] if result["kind"] == "object" else result.get("value")

    def call(self, ref, member, *arguments):
        result = self.request("call", ref=ref, member=member, arguments=list(arguments))["result"]
        return result["ref"] if result["kind"] == "object" else result.get("value")

    def set(self, ref, member, value):
        return self.request("set", ref=ref, member=member, value=value)

    def method(self, ref, name):
        methods = self.request("describe", ref=ref)["methods"]
        matches = [m["name"] for m in methods if m["name"].casefold() in (name.casefold(), (name + "@get").casefold())]
        if len(matches) != 1:
            raise RuntimeError(f"No unique advertised method {name}; available: {[m['name'] for m in methods]}")
        return matches[0]


def com_case(app):
    from object_client import ObjectSession
    spec = {"word": ("WINWORD.EXE", "Documents"), "excel": ("EXCEL.EXE", "Workbooks"),
            "powerpoint": ("POWERPNT.EXE", "Presentations")}
    filename = app + ".json"
    archive_previous(filename)
    report = {"app": app, "channel": "COM IDispatch / same generic ComAdapter", "ok": False,
              "screenshots": 0, "input_injection": 0, "personal_documents_opened": 0,
              "scratch_document_created": False, "evidence_level": "not_connected",
              "implementation": implementation(ROOT / "bridges/com_agent.py")}
    before = process_ids(spec[app][0]) if app in spec else {}
    if before:
        report.update(status="skipped", reason="Existing application process; ownership of a new singleton instance is not guaranteed")
        save(filename, report)
        print(json.dumps(report, ensure_ascii=False), flush=True)
        return
    target = "windows-shell" if app == "shell" else app + "-automation"
    created, document, collection, root = [], None, None, None
    with ObjectSession(ROOT / "objects.universality.json") as session:
        check = ProtocolCheck(session, report, filename)
        try:
            root = check.request("list", target=target)["objects"][0]["ref"]
            if app in spec:
                created = [psutil.Process(pid) for pid in process_ids(spec[app][0]) if pid not in before]
                report["owned_application_pids"] = [p.pid for p in created]
                if len(created) != 1:
                    raise RuntimeError("A single newly started application process was not established")
                collection = check.get(root, spec[app][1])
                if check.get(collection, "Count") != 0:
                    raise RuntimeError("The new automation application was not empty; no document was changed")
            desc = check.request("describe", ref=root)
            report.update(evidence_level="metadata", object_type=desc["type"],
                          properties=len(desc["properties"]), methods=len(desc["methods"]),
                          metadata_cache=desc.get("metadata_cache"), interfaces=desc.get("interfaces"),
                          inheritance_warnings=desc.get("inheritance_warnings"))
            if app == "shell":
                folder = check.call(root, "NameSpace", 17)
                item = check.get(folder, "Self")
                assert check.get(item, "IsFolder") is True
                entries = check.call(folder, "Items")
                report.update(evidence_level="read_verified", child_object_chain=True,
                              drive_item_count=check.get(entries, "Count"), windows_opened=0, ok=True)
            else:
                document = check.call(collection, "Add", *([0] if app == "powerpoint" else []))
                report["scratch_document_created"] = True
                if app == "word":
                    editor = check.get(document, "Content")
                    text_member = "Text"
                elif app == "excel":
                    sheet = check.get(document, "ActiveSheet")
                    editor = check.call(sheet, "Range@get", "A1")
                    text_member = "Value2"
                else:
                    slides = check.get(document, "Slides")
                    master = check.get(document, "SlideMaster")
                    layouts = check.get(master, "CustomLayouts")
                    layout = check.call(layouts, check.method(layouts, "Item"), 1)
                    slide = check.request("call", ref=slides, member=check.method(slides, "AddSlide"),
                                          arguments=[1, None], object_arguments={"1": layout})["result"]["ref"]
                    shapes = check.get(slide, "Shapes")
                    initial_shapes = check.get(shapes, "Count")
                    shape = check.call(shapes, "AddTextbox", 1, 10.0, 10.0, 300.0, 50.0)
                    frame = check.get(shape, "TextFrame")
                    editor = check.get(frame, "TextRange")
                    text_member = "Text"
                samples = []
                for index in range(5):
                    text = f"AppCtl 通用接口验证 {app} {index}"
                    started = time.perf_counter()
                    check.set(editor, text_member, text)
                    actual = check.get(editor, text_member)
                    assert actual.rstrip("\r") == text, {"readback": actual, "expected": text}
                    samples.append(round((time.perf_counter() - started) * 1000, 3))
                report.update(evidence_level="write_readback_verified", repetitions=5, roundtrip_ms=samples,
                              median_roundtrip_ms=statistics.median(samples), child_object_chain=True)
                if app == "excel":
                    check.set(editor, "Formula", "=SUM(1,2,3)")
                    check.call(root, "Calculate")
                    report["calculation_result"] = check.get(editor, "Value2")
                    assert report["calculation_result"] == 6
                    report["evidence_level"] = "business_result_verified"
                elif app == "powerpoint":
                    assert check.get(shapes, "Count") == initial_shapes + 1 and check.get(slides, "Count") == 1
                    report["same_provider_object_argument_verified"] = True
                report["ok"] = True
        except Exception as exc:
            report.update(error_type=type(exc).__name__, error=str(exc))
        finally:
            if document and created:
                try:
                    if app == "powerpoint":
                        check.set(document, "Saved", True)
                        check.call(document, "Close")
                    else:
                        check.call(document, "Close", 0)
                    report["scratch_document_closed_without_save"] = True
                except Exception as exc:
                    report["document_cleanup_error"] = str(exc)
            if collection and created:
                try:
                    empty = check.get(collection, "Count") == 0
                    report["application_empty_after_test"] = empty
                    if empty:
                        check.call(root, "Quit", *([0] if app == "word" else []))
                        report["owned_application_quit_requested"] = True
                except Exception as exc:
                    report["application_cleanup_error"] = str(exc)
    for proc in created:
        try:
            proc.wait(timeout=5)
        except psutil.TimeoutExpired:
            pass
    report["owned_application_pids_remaining"] = [p.pid for p in created if p.is_running()]
    save(filename, report)
    print(json.dumps({k: v for k, v in report.items() if k != "requests"}, ensure_ascii=False), flush=True)


def application_path(name):
    inventory = json.loads(REPORT.read_text(encoding="utf-8"))
    found = [Path(os.path.expandvars(r["path"])) for r in inventory["installed"]["application_paths"]
             if r["name"].casefold() == name.casefold()]
    return next((p for p in found if p.is_file()), None)


def require_ok(result):
    if not result.get("ok"):
        raise RuntimeError(json.dumps(result.get("error"), ensure_ascii=False))
    return result["data"]


def close_owned(process):
    """Close only a process spawned by this invocation, and its captured children."""
    try:
        parent = psutil.Process(process.pid)
        if parent.create_time() != process._appctl_created:
            return {"closed": False, "reason": "process_identity_not_verified"}
        owned = parent.children(recursive=True) + [parent]
    except psutil.NoSuchProcess:
        return {"closed": True, "owned_pids_remaining": []}
    for proc in reversed(owned):
        try:
            # The Popen handle already owns terminate rights. Reopening a
            # Windows process during its exit can spuriously return AccessDenied.
            if proc.pid == process.pid:
                if process.poll() is None: process.terminate()
            else: proc.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    _, alive = psutil.wait_procs(owned, timeout=4)
    # No other processes are discovered or killed after the ownership snapshot.
    for proc in alive:
        try:
            if proc.is_running():
                if proc.pid == process.pid:
                    if process.poll() is None: process.kill()
                else: proc.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    _, alive = psutil.wait_procs(alive, timeout=3)
    return {"closed": not alive, "owned_pids_remaining": [p.pid for p in alive]}


def launch_owned(argv, hidden=True):
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = 0 if hidden else 1
    started = time.time()
    process = subprocess.Popen([str(v) for v in argv], stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               creationflags=subprocess.CREATE_NO_WINDOW, startupinfo=startup)
    process._appctl_started = started
    try:
        process._appctl_created = psutil.Process(process.pid).create_time()
    except psutil.NoSuchProcess:
        process._appctl_created = None
    return process


def browser_case(app):
    from cdp_backend import CdpBackend, targets
    filename = app + ".json"
    archive_previous(filename)
    report = {"app": app, "channel": "same Chromium CDP adapter", "ok": False,
              "scope": "owned headless browser, separate profile, locally authored test page",
              "real_website_business_coverage": False, "screenshots": 0, "input_injection": 0,
              "evidence_level": "not_connected"}
    exe = application_path("chrome.exe" if app == "chrome" else "msedge.exe")
    if not exe:
        report.update(status="skipped", reason="Installed executable not found")
        save(filename, report)
        return
    run = OUTPUT / "scratch" / (app + "-" + uuid.uuid4().hex[:8])
    profile = run / "profile"
    run.mkdir(parents=True)
    process = launch_owned([exe, "--headless=new", "--remote-debugging-address=127.0.0.1",
                            "--remote-debugging-port=0", "--user-data-dir=" + str(profile),
                            "--no-first-run", "--disable-extensions", "--disable-background-networking",
                            "--disable-component-update", "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE localhost",
                            (ROOT / "tests/fixtures/universality.html").as_uri()])
    backend = CdpBackend()
    try:
        port_file = profile / "DevToolsActivePort"
        deadline = time.monotonic() + 15
        while not port_file.exists() and time.monotonic() < deadline and process.poll() is None:
            time.sleep(.1)
        endpoint = "http://127.0.0.1:" + port_file.read_text().splitlines()[0]
        pages = targets(endpoint)
        fixture_pages = [p for p in pages if p.get("url") == (ROOT / "tests/fixtures/universality.html").as_uri()]
        assert len(fixture_pages) == 1, "Exactly one owned local page must be selected"
        context = {"endpoint": endpoint, "target": fixture_pages[0]["id"]}
        started = time.perf_counter()
        data = require_ok(backend.request("inspect", {**context, "depth": 12, "limit": 200}))
        report.update(evidence_level="metadata", inspection_ms=round((time.perf_counter() - started) * 1000, 3),
                      nodes=len(data["elements"]))
        report["last_step"] = "resolve_input_and_button"
        report["control_metadata"] = [{"name": r["name"], "role": r["role"], "actions": r["actions"]} for r in data["elements"]]
        inputs = [r for r in data["elements"] if r["name"] == "测试正文" and "set" in r["actions"]]
        buttons = [r for r in data["elements"] if r["name"] == "提交测试记录" and "invoke" in r["actions"]]
        assert len(inputs) == len(buttons) == 1
        require_ok(backend.request("watch", context))
        samples = []
        for i in range(5):
            report["last_step"] = f"write_and_submit_{i}"
            text = f'AppCtl {app} {i}，引号 " 与换行\n第二行'
            started = time.perf_counter()
            require_ok(backend.request("set", {**context, "ref": inputs[0]["ref"], "value": text}))
            assert require_ok(backend.request("get", {**context, "ref": inputs[0]["ref"]}))["value"] == text
            require_ok(backend.request("invoke", {**context, "ref": buttons[0]["ref"]}))
            echo = require_ok(backend.request("get", {**context, "css": "#echo"}))["value"]
            assert echo == text, {"echo": echo, "expected": text}
            assert require_ok(backend.request("get", {**context, "css": "#count"}))["value"] == str(i + 1)
            samples.append(round((time.perf_counter() - started) * 1000, 3))
        report["last_step"] = "verify_canvas_gap"
        canvas = [r for r in data["elements"] if r["name"] == "仅有像素的绘图区"]
        assert canvas and all("set" not in r["actions"] and "invoke" not in r["actions"] for r in canvas)
        report.update(ok=True, evidence_level="fixture_business_result_verified", repetitions=5,
                      input_event_state_verified=True, submission_result_verified=True, roundtrip_ms=samples,
                      median_roundtrip_ms=statistics.median(samples),
                      event_count=len(require_ok(backend.request("events", {**context, "timeout_ms": 500}))["events"]),
                      pixel_only_canvas_not_actionable=True)
    except Exception as exc:
        report.update(error_type=type(exc).__name__, error=str(exc))
    finally:
        backend.close()
        report["cleanup"] = close_owned(process)
    save(filename, report)
    print(json.dumps(report, ensure_ascii=False), flush=True)


def electron_case():
    """Use the same CDP control adapter on an isolated real VS Code workbench."""
    from cdp_backend import CdpBackend, targets
    app, filename = "vscode", "vscode.json"
    archive_previous(filename)
    report = {"app": app, "ok": False, "channel": "same Chromium CDP adapter on actual Electron UI",
              "screenshots": 0, "input_injection": 0, "personal_profile_used": False,
              "evidence_level": "not_connected"}
    exe = application_path("code.exe")
    if not exe or (exe.parent / "data").exists():
        report.update(status="skipped", reason="Executable missing or portable mode would override profile isolation")
        save(filename, report)
        return
    run = OUTPUT / "scratch" / (app + "-" + uuid.uuid4().hex[:8])
    workspace = run / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "fixture.txt").write_text("AppCtl_owned_fixture\n", encoding="utf-8")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    endpoint = f"http://127.0.0.1:{port}"
    process = launch_owned([exe, "--new-window", "--user-data-dir=" + str(run / "profile"),
                            "--extensions-dir=" + str(run / "extensions"), "--disable-extensions",
                            "--skip-welcome", "--skip-release-notes", "--skip-add-to-recently-opened",
                            "--locale=en", "--remote-debugging-address=127.0.0.1",
                            "--remote-debugging-port=" + str(port), workspace])
    backend = CdpBackend()
    try:
        pages = []
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and process.poll() is None:
            try:
                pages = [p for p in targets(endpoint) if "workbench" in p.get("url", "")]
                if len(pages) == 1:
                    break
            except Exception:
                pass
            time.sleep(.2)
        assert len(pages) == 1, "One isolated workbench target was not established"
        # The endpoint must actually be owned by our newly launched process tree.
        owned_pids = {process.pid} | {p.pid for p in psutil.Process(process.pid).children(recursive=True)}
        sockets = [c for c in psutil.net_connections(kind="tcp") if c.status == "LISTEN" and c.laddr.port == port]
        assert sockets and all(c.pid in owned_pids for c in sockets), "CDP listener ownership could not be verified"
        context = {"endpoint": endpoint, "target": pages[0]["id"]}
        time.sleep(.5)
        data = require_ok(backend.request("inspect", {**context, "depth": 16, "limit": 800}))
        report.update(evidence_level="control_metadata", nodes=len(data["elements"]), owned_pid=process.pid,
                      control_names=[r["name"] for r in data["elements"] if set(r["actions"]) - {"get", "focus"}][:60])
        search = [r for r in data["elements"] if "invoke" in r["actions"] and r["name"].startswith("Search") and "Ctrl+Shift+F" in r["name"]]
        if len(search) == 1:
            require_ok(backend.request("invoke", {**context, "ref": search[0]["ref"]}))
            data = require_ok(backend.request("inspect", {**context, "depth": 18, "limit": 1000}))
            inputs = [r for r in data["elements"] if "set" in r["actions"] and r["name"] == "Search"]
            report["editable_names"] = [r["name"] for r in data["elements"] if "set" in r["actions"]]
            if len(inputs) == 1:
                text = "AppCtl_owned_fixture"
                require_ok(backend.request("set", {**context, "ref": inputs[0]["ref"], "value": text}))
                assert require_ok(backend.request("get", {**context, "ref": inputs[0]["ref"]}))["value"] == text
                report.update(evidence_level="control_write_readback_verified", search_field_write_verified=True)
                time.sleep(.8)
                data = require_ok(backend.request("inspect", {**context, "depth": 20, "limit": 1000}))
                report["search_result_labels"] = [r["name"] for r in data["elements"]
                                                  if "1 result" in r["name"] or text in r["name"] and r["role"] not in ("Edit", "Text")][:12]
                report["search_business_result_verified"] = bool(report["search_result_labels"])
        else:
            report["write_deferred"] = "No unique advertised Search activity action in this startup state"
        report["ok"] = True
    except Exception as exc:
        report.update(error_type=type(exc).__name__, error=str(exc))
    finally:
        backend.close()
        report["cleanup"] = close_owned(process)
    save(filename, report)
    print(json.dumps(report, ensure_ascii=False), flush=True)


def native_case(app, objects_only=False):
    """Inspect actual installed programs; write only a uniquely identified scratch address field."""
    from desktop import DesktopSession
    from native_qt import RemoteMemory, inspect_qt
    filenames = {"7zip": "7zFM.exe", "gowin": "gw_ide.exe", "imager": "rpi-imager.exe",
                 "notepad": "notepad.exe"}
    filename = app + ".json"
    archive_previous(filename)
    report = {"app": app, "ok": False, "screenshots": 0, "input_injection": 0,
              "evidence_level": "not_connected", "control_values_saved": False,
              "business_methods_called": 0, "personal_documents_modified": 0}
    exe = application_path(filenames[app])
    if not exe or process_ids(exe.name):
        report.update(status="skipped", reason="Missing executable or existing instance cannot be isolated")
        save(filename, report)
        print(json.dumps(report, ensure_ascii=False), flush=True)
        return
    scratch = OUTPUT / "scratch" / (app + "-" + uuid.uuid4().hex[:8])
    scratch.mkdir(parents=True)
    (scratch / "fixture.txt").write_text("AppCtl owned scratch file\n", encoding="utf-8")
    argv = [exe] + ([scratch] if app == "7zip" else [scratch / "fixture.txt"] if app == "notepad" else [])
    try:
        process = launch_owned(argv)
    except OSError as exc:
        report.update(error_type=type(exc).__name__, error=str(exc), status="launch_failed")
        save(filename, report)
        print(json.dumps(report, ensure_ascii=False), flush=True)
        return
    if objects_only:
        try:
            time.sleep(1)
            report.update(owned_pid=process.pid, scope="owned process startup; no main-window or UI coverage claim",
                          window_lookup_required=False, channel="same Qt5 native object observer")
            report["native_qt"] = inspect_qt(process.pid, max_objects=128, max_seconds=6)
            report["ok"] = bool(report["native_qt"].get("ok"))
            if report["ok"]:
                report["evidence_level"] = "native_object_metadata"
        except Exception as exc:
            report.update(error_type=type(exc).__name__, error=str(exc))
        finally:
            report["cleanup"] = close_owned(process)
        save(filename, report)
        print(json.dumps({k: v for k, v in report.items() if k != "native_qt"}, ensure_ascii=False), flush=True)
        return
    try:
        deadline = time.monotonic() + 12
        selected = []
        while time.monotonic() < deadline and process.poll() is None:
            selected = [w for w in windows(visible_only=False) if w["pid"] == process.pid and w["caption"] and not w["owner"] and w["title_length"] and w["width"] >= 200 and w["height"] >= 100]
            if len(selected) == 1:
                break
            time.sleep(.2)
        if len(selected) != 1:
            raise RuntimeError("Owned launcher did not expose one unambiguous main window in its own PID; not attaching to a guessed process")
        report["owned_pid"] = process.pid
        # Window selection is based on returned HWND/PID, not reconstructed handles.
        window = selected[0]
        report["window_class"] = window["class"]
        with DesktopSession() as session:
            report["channels"] = []
            observed = {}
            for channel in ("win32", "uia3", "msaa"):
                result = session.request("inspect", {"window": window["hwnd"], "backend": channel,
                                         "depth": 9, "limit": 250, "actionable": False, "probe_timeout": 4})
                data = result.get("data", {})
                observed[channel] = data.get("elements", [])
                report["channels"].append({"backend": channel, "ok": result.get("ok"),
                                            "quality": data.get("quality"), "roundtrip_ms": result.get("roundtrip_ms"),
                                            "error": result.get("error")})
            report["evidence_level"] = "control_metadata"
            if app == "7zip":
                candidates = []
                for row in observed["win32"]:
                    if "set" in row.get("actions", []) and "get" in row.get("actions", []):
                        result = session.request("get", {"ref": row["ref"]})
                        if result.get("ok") and result["data"].get("value", "").rstrip("\\/") == str(scratch).rstrip("\\/"):
                            candidates.append((row, result["data"]["value"]))
                if len(candidates) == 1:
                    row, original = candidates[0]
                    replacement = str(scratch / "child")
                    (scratch / "child").mkdir()
                    try:
                        require_ok(session.request("set", {"ref": row["ref"], "value": replacement}))
                        assert require_ok(session.request("get", {"ref": row["ref"]}))["value"] == replacement
                        report.update(evidence_level="control_write_readback_verified", address_field_written=True,
                                      navigation_business_result_verified=False)
                    finally:
                        require_ok(session.request("set", {"ref": row["ref"], "value": original}))
                        report["original_address_restored"] = require_ok(session.request("get", {"ref": row["ref"]}))["value"] == original
                else:
                    report["write_deferred"] = "No unique editable control containing the owned scratch path"
        memory = RemoteMemory(process.pid)
        try:
            modules = memory.modules
            report["qt_modules"] = [m["name"] for m in modules if "qt5core" in m["name"].lower() or "qt6core" in m["name"].lower()]
        finally:
            memory.close()
        if any("qt5core" in n.lower() for n in report["qt_modules"]):
            report["native_qt"] = inspect_qt(process.pid, max_objects=128, max_seconds=6)
            if report["native_qt"].get("ok"):
                report["evidence_level"] = "native_object_metadata"
        elif any("qt6core" in n.lower() for n in report["qt_modules"]):
            report["native_object_gap"] = "Qt6 layout adapter is not implemented; Qt5 observer was not applied"
        report["ok"] = any(r["ok"] for r in report["channels"])
    except Exception as exc:
        report.update(error_type=type(exc).__name__, error=str(exc))
    finally:
        report["cleanup"] = close_owned(process)
    save(filename, report)
    print(json.dumps({k: v for k, v in report.items() if k != "native_qt"}, ensure_ascii=False), flush=True)


def suite(cases=None):
    cases = cases or [("com", "word"), ("com", "excel"), ("com", "powerpoint"), ("com", "shell"), ("browser", "chrome"), ("browser", "edge"),
                       ("native", "7zip"), ("qt", "gowin"), ("native", "imager"), ("native", "notepad"), ("electron", None)]
    for stage, app in cases:
        print(json.dumps({"starting": app, "stage": stage}), flush=True)
        argv = [sys.executable, str(Path(__file__).resolve()), "--stage", stage] + (["--app", app] if app else [])
        proc = subprocess.run(argv,
                              cwd=ROOT, timeout=100, creationflags=subprocess.CREATE_NO_WINDOW,
                              capture_output=True, encoding="utf-8", errors="replace")
        if proc.stdout:
            print(proc.stdout.strip(), flush=True)
        if proc.returncode:
            print(json.dumps({"app": app, "worker_exit": proc.returncode, "error": proc.stderr[-1800:]}), flush=True)


def audit():
    original = json.loads(REPORT.read_text(encoding="utf-8"))
    current_windows = {w["hwnd"]: w for w in windows(visible_only=False)}
    selected = {"explorer.exe", "chrome.exe", "msedge.exe", "QQ.exe", "Weixin.exe", "wps.exe", "cloudmusic.exe", "CalculatorApp.exe"}
    rows = []
    for app in original["applications"]:
        if app["name"] not in selected:
            continue
        for window in app["windows"]:
            if window["class"] not in ("CabinetWClass", "Chrome_WidgetWin_1", "Qt51514QWindowIcon", "OpusApp", "OrpheusBrowserHost", "Windows.UI.Core.CoreWindow"):
                continue
            current = current_windows.get(window["hwnd"])
            rows.append({"app": app["name"], "hwnd": window["hwnd"],
                         "same_hwnd_pid": bool(current) and current["pid"] == window["pid"],
                         "minimized_state_preserved": bool(current) and current["minimized"] == window["minimized"]})
    originals = {r["pid"] for r in original["applications"]}
    new_apps = []
    names = {"notepad.exe", "7zfm.exe", "gw_ide.exe", "rpi-imager.exe", "code.exe", "winword.exe", "excel.exe", "powerpnt.exe"}
    for proc in psutil.process_iter(["name", "create_time"]):
        if proc.pid in originals or (proc.info.get("name") or "").casefold() not in names:
            continue
        try:
            argv = proc.cmdline()
            new_apps.append({"name": proc.info["name"], "pid": proc.pid,
                             "owned_scratch_argument": any(str(OUTPUT / "scratch").casefold() in a.casefold() for a in argv),
                             "created": proc.info["create_time"]})
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    result = {"captured_at": datetime.now(timezone.utc).isoformat(), "original_windows": rows,
              "all_original_windows_preserved": all(r["same_hwnd_pid"] for r in rows),
              "all_original_minimized_states_preserved": all(r["minimized_state_preserved"] for r in rows),
              "additional_candidate_processes": new_apps}
    save("audit.json", result)
    print(json.dumps(result, ensure_ascii=False), flush=True)


def summarize(integrate=False):
    cases = []
    names = ("word", "excel", "powerpoint", "shell", "chrome", "edge", "7zip", "gowin", "imager", "notepad", "vscode")
    observations = {"observed_at": datetime.now(timezone.utc).isoformat(), "apps": {}}
    for name in names:
        path = OUTPUT / (name + ".json")
        if not path.exists():
            continue
        row = json.loads(path.read_text(encoding="utf-8"))
        entry = {k: row.get(k) for k in ("app", "ok", "evidence_level", "channel", "error", "status", "median_roundtrip_ms") if k in row}
        entry["report"] = path.relative_to(ROOT).as_posix()
        if name == "gowin" and row.get("native_qt", {}).get("ok"):
            observations["observed_at"] = row.get("captured_at") or datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
            observations["apps"][name] = {"native_qt_objects": row["native_qt"]}
            entry["native_statistics"] = row["native_qt"]["statistics"]
        cases.append(entry)
    save("observations.json", observations)
    result = {"cases": cases, "selection": "Purposive sample of installed programs; not a random sample or a complete feature inventory",
              "coverage_percentage_estimated": False, "visual_computer_use_baseline_run": False,
              "messages_sent": 0, "screenshots": 0, "input_injection": 0,
              "browser_scope": "Chrome and Edge run the same locally authored page; this validates the transport and event handling, not arbitrary websites",
              "independent_framework_count_warning": "Office applications share COM; Chrome and Edge share Chromium. Application count is not framework count."}
    if integrate and observations["apps"]:
        from object_client import ObjectSession
        with ObjectSession(ROOT / "objects.universality.json") as session:
            exploration = session.request("explore", {"target": "gowin-observed", "query": "project window",
                                          "max_objects": 128, "max_depth": 6, "max_requests": 256, "max_seconds": 15})
            save("gowin-exploration.json", exploration)
            assert exploration.get("ok"), exploration
            result["generic_object_exploration_verified"] = True
    save("summary.json", result)
    print(json.dumps(result, ensure_ascii=False), flush=True)


def save(name, value):
    OUTPUT.mkdir(parents=True, exist_ok=True)
    value.setdefault("captured_at", datetime.now(timezone.utc).isoformat())
    (OUTPUT / name).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def windows(visible_only=True):
    user = ctypes.WinDLL("user32", use_last_error=True)
    user.IsWindowVisible.argtypes = [wintypes.HWND]
    user.IsIconic.argtypes = [wintypes.HWND]
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    user.GetWindow.argtypes = [wintypes.HWND, ctypes.c_uint]
    user.GetWindow.restype = wintypes.HWND
    user.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    rows = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    @callback_type
    def visit(hwnd, _):
        if not visible_only or user.IsWindowVisible(hwnd):
            pid = wintypes.DWORD()
            user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            name = ctypes.create_unicode_buffer(256)
            user.GetClassNameW(hwnd, name, len(name))
            rect = wintypes.RECT()
            user.GetWindowRect(hwnd, ctypes.byref(rect))
            rows.append({"hwnd": int(hwnd), "pid": pid.value, "class": name.value,
                         "minimized": bool(user.IsIconic(hwnd)), "visible": bool(user.IsWindowVisible(hwnd)),
                         "caption": user.GetWindowLongW(hwnd, -16) & 0x00C00000 == 0x00C00000,
                         "title_length": user.GetWindowTextLengthW(hwnd),
                         "width": rect.right - rect.left, "height": rect.bottom - rect.top,
                         "owner": int(user.GetWindow(hwnd, 4) or 0)})
        return True

    user.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user.EnumWindows(visit, 0)
    return rows


def registry_value(hive, path, name=""):
    try:
        with winreg.OpenKey(hive, path) as key:
            return winreg.QueryValueEx(key, name)[0]
    except OSError:
        return None


def installed():
    paths = []
    app_paths = r"Software\Microsoft\Windows\CurrentVersion\App Paths"
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            with winreg.OpenKey(hive, app_paths) as key:
                for index in range(winreg.QueryInfoKey(key)[0]):
                    name = winreg.EnumKey(key, index)
                    path = registry_value(hive, app_paths + "\\" + name)
                    if isinstance(path, str):
                        paths.append({"name": name, "path": path.strip('"')})
        except OSError:
            pass
    system = Path(os.environ["SystemRoot"]) / "System32"
    for name in ("notepad.exe", "mspaint.exe", "calc.exe", "charmap.exe", "write.exe"):
        if (system / name).exists():
            paths.append({"name": name, "path": str(system / name)})
    com = {}
    for progid in ("Shell.Application", "Scripting.Dictionary", "Word.Application", "Excel.Application",
                   "PowerPoint.Application", "kwps.Application", "ket.Application", "kwpp.Application",
                   "AcroExch.App"):
        clsid = registry_value(winreg.HKEY_CLASSES_ROOT, progid + r"\CLSID")
        com[progid] = {"registered": bool(clsid)}
        if clsid:
            com[progid]["local_server"] = registry_value(winreg.HKEY_CLASSES_ROOT, "CLSID\\" + clsid + r"\LocalServer32")
    return {"application_paths": paths, "com": com}


def inventory():
    from native_qt import RemoteMemory
    seen = {}
    for window in windows():
        pid = window["pid"]
        if pid not in seen:
            try:
                proc = psutil.Process(pid)
                seen[pid] = {"pid": pid, "name": proc.name(), "exe": proc.exe(),
                             "created": proc.create_time(), "windows": []}
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        seen[pid]["windows"].append(window)
    markers = ("qt5", "qt6", "libcef", "chrome", "webview", "coreclr", "clr.dll", "jvm", "tk86", "tcl86")
    for row in seen.values():
        try:
            memory = RemoteMemory(row["pid"])
            try:
                row["framework_modules"] = [m for m in memory.modules if any(s in m["name"].lower() for s in markers)]
            finally:
                memory.close()
        except Exception as exc:
            row["module_error"] = type(exc).__name__
    report = {"captured_at": datetime.now(timezone.utc).isoformat(), "stage": "inventory",
              "scope": {"window_titles": False, "business_values": False, "application_calls": 0,
                        "screenshots": 0, "input_injection": 0},
              "applications": list(seen.values()), "installed": installed()}
    save("inventory.json", report)
    print(json.dumps({"applications": [{"name": r["name"], "pid": r["pid"], "exe": r["exe"],
                       "windows": r["windows"], "modules": [m["name"] for m in r.get("framework_modules", [])],
                       "module_error": r.get("module_error")} for r in seen.values()],
                      "installed": report["installed"]}, ensure_ascii=False), flush=True)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", choices=("inventory", "com", "browser", "native", "qt", "electron", "suite", "extended", "audit", "summarize", "integrate", "finalize"), default="inventory")
    parser.add_argument("--app", choices=("word", "excel", "powerpoint", "shell", "chrome", "edge", "7zip", "gowin", "imager", "notepad"))
    args = parser.parse_args()
    allowed = {"com": {"word", "excel", "powerpoint", "shell"}, "browser": {"chrome", "edge"},
               "native": {"7zip", "gowin", "imager", "notepad"}, "qt": {"7zip", "gowin", "imager", "notepad"}}
    if args.stage in allowed and args.app not in allowed[args.stage]:
        parser.error("Choose an --app appropriate to the selected stage")
    if args.stage == "inventory":
        inventory()
    elif args.stage == "suite":
        suite()
        summarize(integrate=True)
        audit()
    elif args.stage == "extended":
        suite([("com", "word"), ("com", "excel"), ("com", "powerpoint"), ("qt", "gowin"), ("electron", None), ("audit", None)])
    elif args.stage == "audit":
        audit()
    elif args.stage == "electron":
        electron_case()
    elif args.stage in ("summarize", "integrate"):
        summarize(integrate=args.stage == "integrate")
    elif args.stage == "finalize":
        native_case("gowin", objects_only=True)
        summarize(integrate=True)
        audit()
    elif not args.app:
        parser.error("--app is required for this stage")
    elif args.stage == "com":
        com_case(args.app)
    elif args.stage == "browser":
        browser_case(args.app)
    elif args.stage == "qt":
        native_case(args.app, objects_only=True)
    else:
        native_case(args.app)


if __name__ == "__main__":
    main()
