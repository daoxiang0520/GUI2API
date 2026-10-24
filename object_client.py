"""One client protocol for business objects from independent runtime bridges.

Providers are explicitly configured stdio processes. Commands are argv arrays,
never shell strings. A provider timeout invalidates all its object references;
calls and writes are never automatically retried.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid

from object_bridge import PROTOCOL

ROOT = Path(__file__).resolve().parent


def error(code, message, **details):
    return {"ok": False, "error": {"code": code, "message": message, **details}}


def schema():
    return {
        "protocol": PROTOCOL, "entry": "python appctl.py objects",
        "operations": ["discover", "attach", "targets", "connect", "list", "describe", "explore", "get", "set", "call", "watch", "events", "release"],
        "object_model": {
            "identity": "opaque session reference, independent of HWND and rendering",
            "members": "properties, methods with signatures, events",
            "values": "kind:value for literal JSON, kind:object for a retained business object",
            "object_arguments": "map argument index to a same-provider object reference",
            "call_result": "returned means the function returned; it does not prove downstream business completion",
        },
        "exploration": {
            "client_operation": "explore", "format": "appctl.exploration/1",
            "entry_required": "configured provider or current attached object ref",
            "default": "metadata graph and text matching; no property reads or method calls",
            "traversal": "opt-in exact property names and declared read methods requiring no arguments",
            "outputs": "object identity graph, metadata fingerprints, matches, budgets, deferred work and errors",
            "business_semantics_inferred": False, "private_process_bootstrap": False,
        },
        "transport": ["stdio"], "config": "APPCTL_OBJECT_CONFIG or objects.local.json",
        "runtime_adapters": {
            "python": "registered Python objects; reflection and optional Event adapter",
            "javascript": "registered Node.js objects; property descriptors, prototype methods, EventEmitter",
            "dotnet": "registered .NET objects; Reflection and CLR events",
            "qt": "registered PyQt5 QObjects; QMetaObject properties, slots, signals",
            "com": "existing running COM IDispatch objects with ITypeInfo; metadata-only ROT discovery and explicit attach; connection-point events not implemented",
            "java": "not implemented",
            "native_cpp": "requires an explicit object bridge or application ABI adapter",
            "native_observation": "read-only metadata snapshots from Qt5 QObject and MSVC x64 RTTI observers; native invocation is not available",
            "application_fields": "explicit application profiles; version-bound Qt accessibility bootstrap or live Electron Node-API bridge; compare-and-set fields and actual model read-back, no message sending",
        },
        "arbitrary_process_attach": False, "universal_unmodified_app_coverage": False,
        "mutation_retry": False, "screenshots": False,
    }


def discover(runtime="com"):
    if runtime != "com":
        return error("unsupported_discovery", "Only registered running COM objects can currently be discovered outside an application")
    try:
        process = subprocess.run([sys.executable, str(ROOT / "bridges/com_agent.py"), "--discover"], capture_output=True, encoding="utf-8", timeout=10, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if process.returncode:
            return error("discovery_unavailable", "The COM discovery helper could not inspect this session")
        return json.loads(process.stdout)
    except subprocess.TimeoutExpired:
        return error("discovery_timeout", "COM discovery exceeded 10 seconds; its helper was stopped")
    except (OSError, ValueError) as exc:
        return error("discovery_unavailable", str(exc))


class StdioProvider:
    def __init__(self, command, cwd, runtime=None, timeout=6):
        self.sequence = 0
        self.timeout = timeout
        self.generation = uuid.uuid4().hex
        self.lock = threading.RLock()
        self.lines = queue.Queue()
        self.process = subprocess.Popen(command, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, encoding="utf-8", errors="strict", bufsize=1, env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        threading.Thread(target=self._reader, daemon=True).start()
        result = self.request("ping", {}, timeout=max(15, timeout))
        if not result.get("ok") or result.get("data", {}).get("protocol") != PROTOCOL:
            self.close(force=True)
            cause = result.get('error')
            raise RuntimeError("Object provider initialization failed: " + json.dumps(cause, ensure_ascii=False) if cause else "Object provider did not complete the protocol handshake")
        self.info = result["data"]
        if runtime and self.info.get("runtime") != runtime:
            self.close(force=True)
            raise RuntimeError("Provider runtime differs from the configured runtime")

    def _reader(self):
        try:
            for line in self.process.stdout:
                if len(line) > 4 * 1024 * 1024:
                    self.lines.put(None)
                    return
                self.lines.put(line)
        except (UnicodeError, OSError, ValueError):
            pass
        finally:
            self.lines.put(None)

    def request(self, op, args, timeout=None):
        # A property getter may execute application code, too. Conservatively
        # classify everything except metadata/events as potentially mutating.
        uncertain = op not in ("ping", "list", "describe", "events")
        with self.lock:
            if self.process.poll() is not None:
                return error("provider_stopped", "Provider stopped; reconnect and discover new references")
            self.sequence += 1
            request_id = self.sequence
            try:
                self.process.stdin.write(json.dumps({"id": request_id, "op": op, "args": args}, ensure_ascii=False, allow_nan=False) + "\n")
                self.process.stdin.flush()
                line = self.lines.get(timeout=self.timeout if timeout is None else timeout)
                if line is None:
                    raise OSError("Provider stopped without a reply")
                result = json.loads(line.lstrip("\ufeff"))
                if not isinstance(result, dict) or result.get("id") != request_id or type(result.get("ok")) is not bool:
                    raise ValueError("Provider response does not match this request")
                return result
            except (queue.Empty, OSError, ValueError) as exc:
                self.close(force=True)
                return error("outcome_unknown" if uncertain else "provider_timeout" if isinstance(exc, queue.Empty) else "protocol_error", "Provider closed; no operation was retried", operation_may_have_executed=uncertain)

    def close(self, force=False):
        if not force and self.process.poll() is None:
            try:
                self.process.stdin.close()
                self.process.wait(timeout=3)
            except (OSError, ValueError, subprocess.TimeoutExpired):
                pass
        if self.process.poll() is None:
            self.process.kill()
        try:
            self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass
        for stream in (self.process.stdin, self.process.stdout):
            try:
                stream.close()
            except (OSError, ValueError):
                pass


class ObjectSession:
    def __init__(self, config=None, provider_factory=StdioProvider):
        self.config_path = Path(config or os.environ.get("APPCTL_OBJECT_CONFIG", ROOT / "objects.local.json")).resolve()
        self.factory = provider_factory
        self.providers = {}
        self.refs, self.reverse = {}, {}
        self.lock = threading.RLock()
        if self.config_path.exists():
            self.config = json.loads(self.config_path.read_text(encoding="utf-8-sig"))
            if self.config.get("protocol") != PROTOCOL or not isinstance(self.config.get("targets"), dict):
                raise ValueError("Object configuration must specify appctl.objects/1 and a targets object")
        else:
            self.config = {"protocol": PROTOCOL, "targets": {}}

    def target_info(self):
        # Never return command lines, environment variables, or credentials.
        return [{"target": name, "runtime": config.get("runtime", "unknown"), "description": config.get("description", ""), "connected": name in self.providers and self.providers[name].process.poll() is None, "transport": config.get("transport", "stdio")} for name, config in self.config["targets"].items()]

    def _provider(self, target, connect=True):
        if target not in self.config["targets"]:
            raise ValueError("Unknown target; list explicitly configured targets first")
        if target in self.providers:
            provider = self.providers[target]
            if provider.process.poll() is not None:
                raise RuntimeError("Provider stopped. Use connect explicitly to start a new instance")
            return provider
        if not connect:
            raise RuntimeError("Provider is not connected")
        config = self.config["targets"][target]
        if config.get("transport", "stdio") != "stdio":
            raise ValueError("Only configured stdio providers are currently supported")
        command = config.get("command")
        if not isinstance(command, list) or not command or any(not isinstance(v, str) for v in command):
            raise ValueError("Provider command must be a nonempty argv array")
        replacements = {"{root}": str(ROOT), "{config_dir}": str(self.config_path.parent), "{python}": sys.executable, "{node}": shutil.which("node") or "node", "{powershell}": str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe")}
        def expand(value):
            for key, replacement in replacements.items():
                value = value.replace(key, replacement)
            return value
        timeout = config.get("timeout", 6)
        if type(timeout) not in (int, float) or not .05 <= timeout <= 60:
            raise ValueError("Provider timeout must be .05..60 seconds")
        self.providers[target] = self.factory([expand(v) for v in command], expand(config.get("cwd", "{root}")), runtime=config.get("runtime"), timeout=timeout)
        return self.providers[target]

    def _reference(self, ref):
        record = self.refs.get(ref)
        if not record or time.monotonic() - record["seen"] > 600:
            raise LookupError("Object reference expired; list or inspect again")
        provider = self.providers.get(record["target"])
        if not provider or provider.generation != record["generation"] or provider.process.poll() is not None:
            raise LookupError("The source provider was replaced or stopped")
        record["seen"] = time.monotonic()
        return record

    def _wrap(self, target, value):
        # Only protocol envelopes are interpreted. kind:value contains literal
        # application data, even if that data happens to contain a ref key.
        if isinstance(value, dict):
            if value.get("kind") == "value":
                return value
            if value.get("kind") == "object":
                provider = self.providers[target]
                native = value.get("ref")
                if not isinstance(native, str):
                    raise ValueError("Provider returned an invalid object reference")
                key = (target, provider.generation, native)
                ref = self.reverse.get(key)
                if ref is None:
                    ref = "object_" + uuid.uuid4().hex
                    self.reverse[key] = ref
                self.refs[ref] = {"target": target, "generation": provider.generation, "native_ref": native, "seen": time.monotonic()}
                return {**value, "ref": ref, "target": target}
            return {k: self._wrap(target, v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._wrap(target, v) for v in value]
        return value

    def request(self, op, args=None, request_id=None):
        args = dict(args or {})
        with self.lock:
            try:
                if op == "schema":
                    result = {"ok": True, "data": schema()}
                elif op == "discover":
                    result = discover(args.get("runtime", "com"))
                elif op == "attach":
                    if args.get("runtime", "com") != "com":
                        raise ValueError("External attach currently supports running COM objects only")
                    rot_id, progid = args.get("rot_id"), args.get("progid")
                    if (rot_id is None) == (progid is None):
                        raise ValueError("Specify exactly one rot_id or active-object progid")
                    if rot_id is not None and (not isinstance(rot_id, str) or not re.fullmatch(r"[0-9a-f]{24}", rot_id)):
                        raise ValueError("rot_id must be an exact ID returned by discovery")
                    if progid is not None and (not isinstance(progid, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.]{1,127}", progid)):
                        raise ValueError("Invalid COM ProgID")
                    target = "com:" + (rot_id or progid)
                    activate = args.get('activate', False)
                    if type(activate) is not bool or activate and rot_id is not None:
                        raise ValueError('activate must be boolean and requires a ProgID')
                    if activate:
                        target = 'com-created:' + progid
                    self.config["targets"][target] = {"runtime": "com", "description": "Explicit COM object activation" if activate else "Explicitly attached running COM object", "command": ["{python}", "{root}/bridges/com_agent.py", "--create" if activate else "--rot" if rot_id else "--active", rot_id or progid]}
                    if target in self.providers and self.providers[target].process.poll() is not None:
                        self.providers[target].close()
                        del self.providers[target]
                    provider = self._provider(target)
                    result = provider.request("list", {})
                    if result.get("ok"):
                        result["data"] = {"target": target, "connection": provider.info, **self._wrap(target, result["data"])}
                elif op == "targets":
                    result = {"ok": True, "data": {"targets": self.target_info(), "configured": self.config_path.exists(), "access": "explicitly_configured_providers"}}
                elif op == "explore":
                    from object_explorer import explore
                    result = explore(self, **args)
                else:
                    target = args.pop("target", None)
                    ref = args.get("ref")
                    record = self._reference(ref) if ref is not None else None
                    if record:
                        if target not in (None, record["target"]):
                            raise ValueError("The object belongs to a different provider")
                        target = record["target"]
                        args["ref"] = record["native_ref"]
                    if op not in ("connect", "list", "events") and record is None:
                        raise ValueError("This operation requires a current object ref")
                    if op == "connect" and target in self.providers and self.providers[target].process.poll() is not None:
                        self.providers[target].close()
                        del self.providers[target]
                    provider = self._provider(target, connect=record is None and op in ("list", "connect"))
                    if op == "connect":
                        result = {"ok": True, "data": {"target": target, **provider.info}}
                    else:
                        if op == "set":
                            if "value" not in args:
                                raise ValueError("set requires an explicit value")
                            args["value"] = {"kind": "value", "value": args["value"]}
                        if op == "call":
                            raw = args.get("arguments", [])
                            if not isinstance(raw, list):
                                raise ValueError("arguments must be a JSON array")
                            args["arguments"] = [{"kind": "value", "value": value} for value in raw]
                            object_arguments = args.pop("object_arguments", {})
                            if not isinstance(object_arguments, dict):
                                raise ValueError("object_arguments must map argument indices to object refs")
                            for index, argument_ref in object_arguments.items():
                                if not str(index).isdigit() or not 0 <= int(index) < len(raw):
                                    raise ValueError("An object argument index is outside arguments")
                                other = self._reference(argument_ref)
                                if other["target"] != target or other["generation"] != provider.generation:
                                    raise ValueError("Object arguments must belong to the same provider instance")
                                args["arguments"][int(index)] = {"kind": "object", "ref": other["native_ref"]}
                        result = provider.request(op, args)
                        if result.get("ok"):
                            result["data"] = self._wrap(target, result["data"])
                            if op == "release":
                                self.refs.pop(ref, None)
                                self.reverse.pop((target, provider.generation, record["native_ref"]), None)
                result["id"] = request_id
                return result
            except LookupError as exc:
                return {"id": request_id, **error("stale_ref", str(exc))}
            except (TypeError, ValueError, KeyError) as exc:
                return {"id": request_id, **error("invalid_arguments", str(exc))}
            except (RuntimeError, OSError) as exc:
                return {"id": request_id, **error("provider_unavailable", str(exc))}

    def close(self):
        for provider in self.providers.values():
            provider.close()
        self.providers.clear()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
