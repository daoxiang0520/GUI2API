"""Text-first Windows application control. No screenshots or visual fallback.

One-shot: python appctl.py apps
Warm session: python appctl.py session  (one JSON request per stdin line)
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
from typing import Any

ROOT = Path(__file__).resolve().parent
ACTIONS = ("get", "set", "invoke", "focus", "select", "toggle-on", "toggle-off", "expand", "collapse", "scroll")
MUTATIONS = (set(ACTIONS) - {"get"}) | {"window",'dismiss_popup','request_accessibility'}
PROTOCOL = "appctl/2"
BACKENDS = ("auto", "uia3", "uia", "msaa", "win32", "cdp")


def error(code: str, message: str, request_id: Any = None) -> dict:
    return {"id": request_id, "ok": False, "error": {"code": code, "message": message}}


class UiaSession:
    """A single long-lived native UIA process, with bounded request waits."""

    def __init__(self, timeout: float = 20, backend: str = "uia"):
        if backend not in ("uia", "uia3", "msaa", "win32"):
            raise ValueError("Unknown native backend")
        self.backend = backend
        powershell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        self.timeout = timeout
        self.sequence = 0
        self.output: queue.Queue[str | None] = queue.Queue()
        self.errors: list[str] = []
        self.lock = threading.Lock()
        self.started_at = time.perf_counter()
        self.process = subprocess.Popen(
            [str(powershell), "-NoLogo", "-NoProfile", "-NonInteractive", "-Mta", "-File", str(ROOT / "native/Start-AppCtlWorker.ps1"), "-Backend", backend],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            encoding="utf-8", errors="replace", text=True, bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()
        ready = self.request("ping")
        self.startup_ms = round((time.perf_counter() - self.started_at) * 1000, 2)
        if not ready.get("ok"):
            self.close()
            raise RuntimeError(json.dumps(ready, ensure_ascii=False))

    def _read_stdout(self):
        try:
            for line in self.process.stdout:
                self.output.put(line)
        finally:
            self.output.put(None)

    def _read_stderr(self):
        for line in self.process.stderr:
            self.errors.append(line.rstrip())
            self.errors[:] = self.errors[-30:]

    def request(self, op: str, args: dict | None = None, request_id: Any = None) -> dict:
        with self.lock:
            self.sequence += 1
            wire_id = self.sequence
            started = time.perf_counter()
            mutation = op in MUTATIONS and not (op == "window" and (args or {}).get("state", "get") == "get")
            if self.process.poll() is not None:
                return error("worker_stopped", "Native UIA worker stopped. " + "\n".join(self.errors), request_id)
            try:
                self.process.stdin.write(json.dumps({"id": wire_id, "op": op, "args": args or {}}, ensure_ascii=False) + "\n")
                self.process.stdin.flush()
                line = self.output.get(timeout=self.timeout)
                if line is None:
                    self.process.wait(timeout=2)
                    return error("outcome_unknown" if mutation else "worker_stopped", "\n".join(self.errors) or "Native worker exited without a reply.", request_id)
                result = json.loads(line.lstrip("\ufeff"))
                if result.get("id") != wire_id:
                    self.close()
                    return error("outcome_unknown" if mutation else "protocol_error", "Worker response ID mismatch; the session was closed.", request_id)
                result["id"] = request_id
                result["roundtrip_ms"] = round((time.perf_counter() - started) * 1000, 2)
                return result
            except queue.Empty:
                self.close()
                return error("outcome_unknown" if mutation else "timeout", "Native request timed out. Worker closed; no operation was retried. Re-observe before any further action.", request_id)
            except (BrokenPipeError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
                self.close()
                return error("outcome_unknown" if mutation else "worker_error", str(exc), request_id)

    def close(self):
        if self.process.poll() is None:
            self.process.kill()
        try:
            self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            if stream:
                try:
                    stream.close()
                except OSError:
                    pass

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def input_command(args: dict) -> list[str]:
    """Explicit input commands; these never run as an automatic UIA fallback."""
    verb = args.get("action")
    window = args.get("window")
    if not isinstance(window, int) or isinstance(window, bool) or window <= 0:
        raise ValueError("input requires a positive window handle")
    if verb in ("keys", "type"):
        field = "text" if verb == "type" else "keys"
        if not isinstance(args.get(field), str):
            raise ValueError(f"{verb} requires {field}")
        command = ["send-keys", args[field], "--via", "send-input"]
        if verb == "type":
            command.append("--verbatim")
        if args.get("target"):
            command += ["--target", str(args["target"])]
    elif verb == "drag":
        if not args.get("from") or not args.get("to"):
            raise ValueError("drag requires from and to selectors or explicit screen coordinates x,y")
        command = ["drag", str(args["from"]), str(args["to"])]
    elif verb in ("click", "double-click", "right-click", "hover"):
        if not args.get("target"):
            raise ValueError(f"{verb} requires an explicit target selector")
        command = ["hover" if verb == "hover" else "click", str(args["target"])]
        if verb == "double-click":
            command.append("--double")
        if verb == "right-click":
            command.append("--right")
    else:
        raise ValueError("input action must be keys, type, drag, click, double-click, right-click, or hover")
    return command + ["-w", str(window), "--json"]


def run_input(args: dict, request_id: Any = None) -> dict:
    try:
        command = input_command(args)
    except ValueError as exc:
        return error("invalid_arguments", str(exc), request_id)
    executable = ROOT / "tools/winapp-cli/0.7.1/winapp.exe"
    environment = dict(os.environ, WINAPP_CLI_TELEMETRY_OPTOUT="1", WINAPP_UI_LOCK_DIRECTORY=str(ROOT / ".winapp-ui"))
    started = time.perf_counter()
    try:
        process = subprocess.run([str(executable), "ui", *command], env=environment, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if process.returncode:
            try:
                result = json.loads(process.stdout or process.stderr)
            except ValueError:
                result = error("input_error", (process.stderr or process.stdout).strip())
            result.update(id=request_id, ok=False, backend="winapp-input")
        else:
            result = {"id": request_id, "ok": True, "backend": "winapp-input", "data": {"status": "executed_unverified", "result": json.loads(process.stdout)}}
        result["roundtrip_ms"] = round((time.perf_counter() - started) * 1000, 2)
        return result
    except subprocess.TimeoutExpired:
        return error("outcome_unknown", "Input timed out; no retry was attempted.", request_id)
    except ValueError:
        return error("outcome_unknown", "Input process returned an unreadable response; the operation may have executed and was not retried.", request_id)
    except OSError as exc:
        return error("input_unavailable", str(exc), request_id)


def schema() -> dict:
    return {
        "protocol": PROTOCOL,
        "transport": "CLI or persistent stdin/stdout JSON lines",
        "commands": ["apps", "window", "probe", "inspect", "find", "describe", "targets", "watch", "events", *ACTIONS, "input", "ping", "schema", "objects"],
        "application_objects": {"entry": "python appctl.py objects", "protocol": "appctl.objects/1", "schema": "python appctl.py objects schema", "access": "registered application objects through runtime bridges; independent of HWND", "unmodified_app_attach": False},
        "backends": list(BACKENDS),
        "window": {"window": "HWND", "state": "get|restore|minimize|activate, default get", "pid": "optional expected process ID"},
        "request": {"id": "caller-selected optional ID", "op": "command", "args": "object"},
        "target": {"ref": "opaque reference from this session, OR:", "window": "current HWND", "automation_id": "exact ID", "name": "optional exact name", "role": "optional control type", "within": "optional unique ancestor AutomationId"},
        "inspect": {"window": "HWND (or explicit CDP endpoint + target)", "backend": "auto|uia3|uia|msaa|win32|cdp, default auto", "within": "optional ancestor AutomationId; restricts to native UIA", "view": "control|raw, default control", "depth": "0..20, default 6", "limit": "1..3000 per backend, default 300", "actionable": "default true", "values": "default false; optionally include short readable values", "since": "optional prior snapshot in this session with identical scope; returns state differences"},
        "probe": {"window": "HWND", "probe_timeout": "per-channel observation timeout in seconds, 1..15, default 6", "refresh": "clear window channel cooldowns"},
        "subtree": {"root_ref":"optional existing UIA3/UIA/MSAA container ref; pins the observation to its source channel", "time_budget_ms":"UIA3 traversal budget 100..15000, default 2000; a final provider call may take longer", "partial_diff":"partial trees cannot reliably report removals; narrow the scope"},
        "cdp": {"endpoint": "explicit loopback http://host:port; auto-discovery only tries listeners owned by the window PID", "target": "exact page target ID; required if multiple targets", "css": "optional exact DOM selector for one-shot actions", "restarts_apps": False},
        "events": {"watch": "subscribe to UIA, WinEventHook, or CDP changes", "timeout_ms": "0..5000; bounded event wait", "values_in_events": False},
        "set": {"value": "required; accepts explicit empty string; read-back verification included"},
        "input": {"action": "keys|type|drag|click|double-click|right-click|hover", "window": "HWND", "target": "explicit WinApp selector where required", "keys": "keyboard chord for keys", "text": "literal string for type", "from": "drag start selector or screen x,y", "to": "drag end selector or screen x,y"},
        "screenshots": False,
        "automatic_visual_fallback": False,
        "notes": ["Use session mode to reuse native workers, channel negotiation, and references. One-shot commands pay startup cost.", "inspect preserves each element's backend; actions do not automatically retry on another backend.", "input is an explicit separate path and does not imply verified application results.", "since is snapshot comparison; watch/events is a real event subscription.", "Auto-probing does not restart apps or modify global accessibility/debug settings.", "Discovery is partial, not a promise of human-equivalent coverage."],
    }


def dispatch(session: UiaSession, request: dict) -> dict:
    request_id = request.get("id")
    op, args = request.get("op"), request.get("args", {})
    if not isinstance(op, str) or not isinstance(args, dict):
        return error("invalid_request", "op must be a string and args must be an object", request_id)
    if op == "schema":
        return {"id": request_id, "ok": True, "data": schema()}
    if op == "input":
        return run_input(args, request_id)
    return session.request(op, args, request_id)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    root.add_argument("--pretty", action="store_true")
    sub = root.add_subparsers(dest="op", required=True)
    sub.add_parser("objects", help="Application business objects; use objects --help", add_help=False)
    sub.add_parser("capabilities", help="Register and invoke named application functions; use capabilities --help", add_help=False)
    for op in ("apps", "ping", "schema", "session"):
        sub.add_parser(op)
    for op in ("probe", "describe"):
        command = sub.add_parser(op)
        command.add_argument("--window", type=int, required=True)
        if op == "probe":
            command.add_argument("--probe-timeout", type=float, default=6)
            command.add_argument("--refresh", action="store_true")
            command.add_argument("--compare-legacy", action="store_true")
            command.add_argument("--depth", type=int, default=8)
            command.add_argument("--limit", type=int, default=300)
    targets = sub.add_parser("targets")
    targets.add_argument("--endpoint", required=True)
    window = sub.add_parser("window")
    window.add_argument("--window", type=int, required=True)
    window.add_argument("--state", choices=("get", "restore", "minimize", "activate"), default="get")
    window.add_argument("--pid", type=int)
    for op in ("inspect", "find"):
        inspect = sub.add_parser(op)
        inspect.add_argument("--window", type=int)
        inspect.add_argument("--backend", choices=BACKENDS, default="auto")
        inspect.add_argument("--within")
        inspect.add_argument("--root-ref")
        inspect.add_argument("--time-budget-ms", type=int, default=2000)
        inspect.add_argument("--view", choices=("control", "raw"), default="control")
        inspect.add_argument("--depth", type=int, default=6)
        inspect.add_argument("--limit", type=int, default=300)
        inspect.add_argument("--all", dest="actionable", action="store_false", default=True)
        inspect.add_argument("--values", action="store_true")
        inspect.add_argument("--since")
        inspect.add_argument("--endpoint")
        inspect.add_argument("--target")
        inspect.add_argument("--probe-timeout", type=float, default=6)
        if op == "find":
            inspect.add_argument("--name")
            inspect.add_argument("--role")
            inspect.add_argument("--automation-id", dest="automation_id")
            inspect.add_argument("--contains", action="store_true")
    for op in ("watch", "events"):
        watch = sub.add_parser(op)
        watch.add_argument("--window", type=int)
        watch.add_argument("--backend", choices=BACKENDS, default="auto")
        watch.add_argument("--endpoint")
        watch.add_argument("--target")
        if op == "watch":
            watch.add_argument("--stop", dest="enabled", action="store_false", default=True)
            watch.add_argument("--duration", type=float, default=10, help="stream change events for 0..60 seconds in CLI mode")
        else:
            watch.add_argument("--timeout-ms", type=int, default=0)
            watch.add_argument("--limit", type=int, default=100)
    for op in ACTIONS:
        command = sub.add_parser(op)
        command.add_argument("ref", nargs="?")
        command.add_argument("--window", type=int)
        command.add_argument("--automation-id", dest="automation_id")
        command.add_argument("--within")
        command.add_argument("--name")
        command.add_argument("--role")
        command.add_argument("--backend", choices=BACKENDS, default="auto")
        command.add_argument("--endpoint")
        command.add_argument("--target")
        command.add_argument("--css")
        if op == "set":
            command.add_argument("--value", required=True)
        if op == "get":
            command.add_argument("--max-length", type=int, default=4096)
        if op == "scroll":
            command.add_argument("--direction", choices=("up", "down", "left", "right"), required=True)
    input_parser = sub.add_parser("input")
    input_parser.add_argument("action", choices=("keys", "type", "drag", "click", "double-click", "right-click", "hover"))
    input_parser.add_argument("--window", type=int, required=True)
    for field in ("target", "keys", "text", "from", "to"):
        input_parser.add_argument("--" + field)
    return root


def main(argv: list[str] | None = None) -> int:
    actual_argv = list(sys.argv[1:] if argv is None else argv)
    if actual_argv and actual_argv[0] == "capabilities":
        from capabilityctl import main as capability_main
        return capability_main(actual_argv[1:])
    if actual_argv[:2] == ["--pretty", "capabilities"]:
        from capabilityctl import main as capability_main
        return capability_main(["--pretty", *actual_argv[2:]])
    if actual_argv and actual_argv[0] == "objects":
        from objectctl import main as object_main
        return object_main(actual_argv[1:])
    if actual_argv[:2] == ["--pretty", "objects"]:
        from objectctl import main as object_main
        return object_main(["--pretty", *actual_argv[2:]])
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stdin.reconfigure(encoding="utf-8")
    options = vars(parser().parse_args(actual_argv))
    op, pretty = options.pop("op"), options.pop("pretty")
    emit = lambda result: print(json.dumps(result, ensure_ascii=False, indent=2 if pretty else None), flush=True)
    if op == "schema":
        emit({"ok": True, "data": schema()})
        return 0
    if op == "input":
        result = run_input({k: v for k, v in options.items() if v is not None})
        emit(result)
        return 0 if result.get("ok") else 1
    try:
        from desktop import DesktopSession
        with DesktopSession() as session:
            if op == "session":
                for line in sys.stdin:
                    try:
                        request = json.loads(line)
                        if not isinstance(request, dict):
                            raise ValueError("request must be an object")
                        emit(dispatch(session, request))
                    except (ValueError, TypeError) as exc:
                        emit(error("invalid_json", str(exc)))
                return 0
            if op == "events":
                emit(error("watch_required", "Use watch/events in session mode, or use the streaming watch CLI command."))
                return 1
            if op == "watch":
                duration = options.pop("duration", 10)
                if not 0 <= duration <= 60:
                    emit(error("invalid_arguments", "duration must be 0..60 seconds"))
                    return 1
                args = {k: v for k, v in options.items() if v is not None}
                result = session.request("watch", args)
                emit(result)
                if not result.get("ok"):
                    return 1
                deadline = time.monotonic() + duration
                while args.get("enabled", True) and time.monotonic() < deadline:
                    result = session.request("events", {"timeout_ms": min(1000, max(0, int((deadline-time.monotonic())*1000)))})
                    if not result.get("ok") or result["data"].get("events"):
                        emit(result)
                    if not result.get("ok"):
                        return 1
                emit(session.request("watch", {**args, "enabled": False}))
                return 0
            result = session.request(op, {k: v for k, v in options.items() if v is not None})
            result["startup_ms"] = session.startup_ms
            emit(result)
            return 0 if result.get("ok") else 1
    except (OSError, RuntimeError) as exc:
        emit(error("startup_failed", str(exc)))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
