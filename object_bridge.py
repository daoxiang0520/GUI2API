"""Framework-neutral object protocol and an embeddable Python/Qt provider.

Only explicitly registered roots are reachable. Describing an object never calls
property getters. Access is by opaque instance IDs, not Python expressions.
"""
from __future__ import annotations

from collections import deque
from contextlib import redirect_stdout
import inspect
import json
import math
import queue
import sys
import threading
import time
import uuid

PROTOCOL = "appctl.objects/1"


class Fault(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def exposed(*, read_only=False):
    def decorate(fn):
        fn.__appctl_read_only__ = read_only
        return fn
    return decorate


class Event:
    """Small optional event adapter for ordinary Python business objects."""
    def __init__(self):
        self.listeners = []

    def connect(self, listener):
        self.listeners.append(listener)

    def disconnect(self, listener):
        self.listeners.remove(listener)

    def emit(self, *args):
        for listener in list(self.listeners):
            listener(*args)


def type_name(annotation):
    if annotation is inspect.Signature.empty:
        return "unknown"
    return getattr(annotation, "__name__", str(annotation))


def json_value(value, depth=0):
    if depth > 20:
        return False
    if value is None or type(value) in (str, bool, int):
        return True
    if type(value) is float:
        return math.isfinite(value)
    if type(value) in (list, tuple):
        return all(json_value(v, depth + 1) for v in value)
    if type(value) is dict:
        return all(type(k) is str and json_value(v, depth + 1) for k, v in value.items())
    return False


class PythonAdapter:
    runtime = "python"

    def identity(self, obj):
        return id(obj)

    def forget_identity(self, obj):
        pass

    def describe(self, obj):
        properties, methods, events = [], [], []
        members = dict(inspect.getmembers_static(obj))
        for name, value in members.items():
            if name.startswith("_"):
                continue
            if isinstance(value, Event):
                events.append({"name": name})
            elif isinstance(value, property):
                properties.append({"name": name, "type": type_name(getattr(value.fget, "__annotations__", {}).get("return", inspect.Signature.empty)), "readable": value.fget is not None, "writable": value.fset is not None, "getter_executed": False})
            elif inspect.isfunction(value) or inspect.ismethod(value) or inspect.ismethoddescriptor(value):
                fn = getattr(obj, name)
                signature_available = True
                try:
                    signature = inspect.signature(fn)
                    parameters = [{"name": p.name, "type": type_name(p.annotation), "required": p.default is inspect.Signature.empty and p.kind not in (p.VAR_POSITIONAL, p.VAR_KEYWORD), "kind": p.kind.name} for p in signature.parameters.values()]
                    returns = type_name(signature.return_annotation)
                except (TypeError, ValueError):
                    signature_available = False
                    parameters, returns = [], "unknown"
                methods.append({"name": name, "parameters": parameters, "returns": returns, "signature_available": signature_available, "effect": "read" if getattr(fn, "__appctl_read_only__", False) else "unknown"})
            elif (name in getattr(obj, "__dict__", {}) or inspect.ismemberdescriptor(value)) and not callable(value):
                properties.append({"name": name, "type": type(value).__name__, "readable": True, "writable": True, "getter_executed": False})
        return {"type": type(obj).__module__ + "." + type(obj).__qualname__, "properties": properties, "methods": methods, "events": events}

    def read(self, obj, member):
        return getattr(obj, member)

    def write(self, obj, member, value):
        setattr(obj, member, value)

    def call(self, obj, member, arguments):
        fn = getattr(obj, member)
        try:
            inspect.signature(fn).bind(*arguments)
        except ValueError:
            pass
        except TypeError as exc:
            raise Fault("invalid_arguments", str(exc))
        result = fn(*arguments)
        if inspect.isawaitable(result):
            import asyncio
            return asyncio.run(result)
        return result

    def subscribe(self, obj, name, listener):
        event = getattr(obj, name)
        event.connect(listener)
        return lambda: event.disconnect(listener)


class QtAdapter(PythonAdapter):
    """Qt meta-object discovery for a registered QObject in its own process.

    This provider currently uses PyQt5 bindings. It does not attach to another
    process or make arbitrary native C++ methods reflective.
    """
    runtime = "qt"

    def describe(self, obj):
        meta = obj.metaObject()
        properties, methods, events = [], [], []
        for i in range(meta.propertyCount()):
            prop = meta.property(i)
            properties.append({"name": str(prop.name()), "type": str(prop.typeName()), "readable": prop.isReadable(), "writable": prop.isWritable(), "getter_executed": False})
        for i in range(meta.methodCount()):
            method = meta.method(i)
            if int(method.access()) != 2:  # QMetaMethod.Public
                continue
            signature = bytes(method.methodSignature()).decode()
            name = bytes(method.name()).decode()
            if int(method.methodType()) == 1:  # QMetaMethod.Signal
                events.append({"name": name, "signature": signature})
            elif int(method.methodType()) in (0, 2):
                names = [bytes(v).decode() for v in method.parameterNames()]
                types = [bytes(v).decode() for v in method.parameterTypes()]
                methods.append({"name": signature, "member": name, "parameters": [{"name": n or f"arg{j}", "type": t, "required": True} for j, (n, t) in enumerate(zip(names, types))], "returns": str(method.typeName()), "effect": "unknown"})
        # Overloaded Qt signals share one Python bound-signal name.
        events = list({e["name"]: e for e in events}.values())
        return {"type": meta.className(), "properties": properties, "methods": methods, "events": events, "discovery": "QMetaObject properties, public slots and invokable methods"}

    def read(self, obj, member):
        return obj.property(member)

    def write(self, obj, member, value):
        if not obj.setProperty(member, value):
            raise Fault("write_failed", "Qt rejected the property write")

    def call(self, obj, member, arguments):
        return getattr(obj, member.split("(")[0])(*arguments)


class ObjectHost:
    def __init__(self, roots, adapter=None, pump=None):
        self.adapter = adapter or PythonAdapter()
        self.roots = dict(roots)
        self.instance = uuid.uuid4().hex
        self.objects, self.identities = {}, {}
        self.root_ids = set()
        self.subscriptions = {}
        self.events = deque(maxlen=1024)
        self.event_lock = threading.RLock()
        self.dropped = 0
        self.sequence = 0
        self.has_pump = pump is not None
        self.pump = pump or (lambda: None)
        for value in self.roots.values():
            self.root_ids.add(self.box(value, force_object=True)["ref"])

    def box(self, value, force_object=False):
        if not force_object and json_value(value):
            return {"kind": "value", "value": value}
        identity = self.adapter.identity(value)
        ref = self.identities.get(identity)
        if ref not in self.objects:
            if len(self.objects) >= 4096:
                raise Fault("object_limit", "Release unused object references")
            ref = self.instance + ":" + uuid.uuid4().hex
            self.objects[ref] = value
            self.identities[identity] = ref
        return {"kind": "object", "ref": ref, "type": type(value).__name__}

    def resolve(self, ref):
        if ref not in self.objects:
            raise Fault("stale_ref", "Object reference is no longer available")
        return self.objects[ref]

    def unbox(self, value):
        if not isinstance(value, dict) or value.get("kind") not in ("value", "object"):
            raise Fault("invalid_arguments", "Arguments must use value/object envelopes")
        if value["kind"] == "object":
            return self.resolve(value.get("ref"))
        if not json_value(value.get("value")):
            raise Fault("invalid_arguments", "Argument is not a finite JSON value")
        return value.get("value")

    def member(self, obj, name, category):
        description = self.adapter.describe(obj)
        exact = [m for m in description[category] if m["name"] == name]
        if not exact and category == "methods":
            exact = [m for m in description[category] if m.get("member") == name]
        if len(exact) != 1:
            raise Fault("ambiguous_member" if exact else "unknown_member", "Choose an exact advertised member")
        return exact[0]

    def event(self, ref, name):
        with self.event_lock:
            if len(self.events) == self.events.maxlen:
                self.dropped += 1
            self.sequence += 1
            self.events.append({"sequence": self.sequence, "object": self.box(self.resolve(ref), True), "name": name})

    def request(self, op, args):
        self.pump()
        if op == "ping":
            return {"protocol": PROTOCOL, "instance": self.instance, "runtime": self.adapter.runtime, "access": "registered_roots", "operations": ["list", "describe", "get", "set", "call", "watch", "events", "release"], "arbitrary_process_attach": False}
        if op == "list":
            return {"objects": [{"name": name, **self.box(obj, True)} for name, obj in self.roots.items()]}
        if op == "events":
            timeout = args.get("timeout_ms", 0)
            limit = args.get("limit", 100)
            if type(timeout) is not int or not 0 <= timeout <= 5000 or type(limit) is not int or not 1 <= limit <= 1000:
                raise Fault("invalid_arguments", "timeout_ms must be 0..5000 and limit 1..1000")
            deadline = time.monotonic() + timeout / 1000
            while not self.events and time.monotonic() < deadline:
                self.pump()
                time.sleep(.005)
            with self.event_lock:
                rows = [self.events.popleft() for _ in range(min(limit, len(self.events)))]
                dropped, self.dropped = self.dropped, 0
            return {"events": rows, "dropped": dropped, "values_in_events": False}
        ref = args.get("ref")
        obj = self.resolve(ref)
        if op == "describe":
            return {"object": self.box(obj, True), **self.adapter.describe(obj)}
        if op == "get":
            prop = self.member(obj, args.get("member"), "properties")
            if not prop["readable"]:
                raise Fault("unsupported", "Property is not readable")
            return {"result": self.box(self.adapter.read(obj, prop["name"]))}
        if op == "set":
            prop = self.member(obj, args.get("member"), "properties")
            if not prop["writable"]:
                raise Fault("unsupported", "Property is not writable")
            value = self.unbox(args["value"])
            self.adapter.write(obj, prop["name"], value)
            if not prop["readable"]:
                return {"status": "executed_unverified"}
            actual = self.adapter.read(obj, prop["name"])
            return {"status": "verified" if actual == value else "verification_failed", "result": self.box(actual)}
        if op == "call":
            method = self.member(obj, args.get("member"), "methods")
            arguments = [self.unbox(v) for v in args.get("arguments", [])]
            result = self.adapter.call(obj, method["name"], arguments)
            return {"status": "returned", "effect": method["effect"], "result": self.box(result)}
        if op == "watch":
            names = args.get("names")
            if names is None:
                names = list(dict.fromkeys(e["name"] for e in self.adapter.describe(obj)["events"]))
            if not isinstance(names, list) or any(not isinstance(n, str) for n in names):
                raise Fault("invalid_arguments", "names must be an array of event names")
            for name in names:
                self.member(obj, name, "events")
                key = (ref, name)
                if key in self.subscriptions:
                    self.subscriptions.pop(key)()
                if args.get("enabled", True):
                    self.subscriptions[key] = self.adapter.subscribe(obj, name, lambda *unused, r=ref, n=name: self.event(r, n))
            return {"watching": [n for r, n in self.subscriptions if r == ref], "values_in_events": False}
        if op == "release":
            if ref in self.root_ids:
                raise Fault("root_reference", "Root objects live for the provider session")
            for key in list(self.subscriptions):
                if key[0] == ref:
                    self.subscriptions.pop(key)()
            self.identities.pop(self.adapter.identity(obj), None)
            self.objects.pop(ref)
            self.adapter.forget_identity(obj)
            return {"released": True}
        raise Fault("unknown_operation", "Unknown object operation")

    def serve(self):
        sys.stdin.reconfigure(encoding="utf-8")
        sys.stdout.reconfigure(encoding="utf-8")
        lines = sys.stdin
        if self.has_pump:
            incoming = queue.Queue(maxsize=32)
            def reader():
                try:
                    for line in sys.stdin:
                        incoming.put(line)
                finally:
                    incoming.put(None)
            threading.Thread(target=reader, daemon=True).start()
            def pumped_lines():
                while True:
                    self.pump()
                    try:
                        line = incoming.get(timeout=.005)
                    except queue.Empty:
                        continue
                    if line is None:
                        return
                    yield line
            lines = pumped_lines()
        protocol_output = sys.stdout
        with redirect_stdout(sys.stderr):
            for line in lines:
                request = {}
                try:
                    request = json.loads(line)
                    if not isinstance(request, dict) or not isinstance(request.get("args", {}), dict):
                        raise Fault("invalid_request", "Request and args must be objects")
                    result = {"id": request.get("id"), "ok": True, "data": self.request(request.get("op"), request.get("args", {}))}
                except Fault as exc:
                    result = {"id": request.get("id") if isinstance(request, dict) else None, "ok": False, "error": {"code": exc.code, "message": str(exc)}}
                except Exception as exc:
                    result = {"id": request.get("id") if isinstance(request, dict) else None, "ok": False, "error": {"code": "application_exception", "message": type(exc).__name__ + ": " + str(exc), "operation_may_have_executed": True}}
                print(json.dumps(result, ensure_ascii=False, allow_nan=False), file=protocol_output, flush=True)
