"""Reflect an explicitly selected IDispatch object, or inspect the Windows ROT.

Discovery reads type information only; it never calls application properties or
business methods. Moniker display names (which can contain filenames) are hashed.
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import gc
from pathlib import Path
import sys
import winreg

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from object_bridge import Fault, ObjectHost, PythonAdapter


def native_window_object(window, expected_pid=None):
    """Request a window's advertised native model with bounded WM_GETOBJECT."""
    import pythoncom
    import win32com.client
    user = ctypes.WinDLL('user32', use_last_error=True)
    user.IsWindow.argtypes = [ctypes.c_void_p]
    user.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
    user.SendMessageTimeoutW.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_size_t, ctypes.c_ssize_t, ctypes.c_uint32, ctypes.c_uint32, ctypes.POINTER(ctypes.c_ssize_t)]
    user.SendMessageTimeoutW.restype = ctypes.c_void_p
    if type(window) is not int or window <= 0 or not user.IsWindow(window):
        raise Fault('invalid_window', 'The selected window does not exist')
    pid = ctypes.c_uint32()
    user.GetWindowThreadProcessId(window, ctypes.byref(pid))
    if expected_pid is not None and pid.value != expected_pid:
        raise Fault('stale_ref', 'The window process changed')
    token = ctypes.c_ssize_t()
    if not user.SendMessageTimeoutW(window, 0x003D, 0, -16, 2 | 32, 350, ctypes.byref(token)):
        raise Fault('native_model_timeout', 'WM_GETOBJECT did not answer within 350 ms')
    if not token.value:
        raise Fault('native_model_not_exposed', 'The window did not expose OBJID_NATIVEOM')
    dispatch = pythoncom.ObjectFromLresult(token.value, pythoncom.IID_IDispatch, 0)
    return win32com.client.Dispatch(dispatch)


def window_tree_objects(window, expected_pid):
    """Generic native-model probe, scoped to one process and bounded in time."""
    import time
    import win32gui
    import win32process
    if not win32gui.IsWindow(window) or win32process.GetWindowThreadProcessId(window)[1] != expected_pid:
        raise Fault('stale_ref', 'The selected window process changed')
    children = []
    def child(hwnd, _):
        if len(children) < 15 and win32process.GetWindowThreadProcessId(hwnd)[1] == expected_pid:
            children.append(hwnd)
    win32gui.EnumChildWindows(window, child, None)
    deadline, roots = time.monotonic() + 3, {}
    for hwnd in [window, *children]:
        if time.monotonic() >= deadline:
            break
        try:
            obj = native_window_object(hwnd, expected_pid)
            # Reject objects without a real IDispatch type description.
            ComAdapter().describe(obj)
            roots['window_model_' + str(len(roots))] = obj
        except Exception:
            continue
    if not roots:
        raise Fault('native_model_not_exposed', 'No native COM model was exposed by the bounded window/child probe')
    return roots


def rot_entries():
    import pythoncom
    rot = pythoncom.GetRunningObjectTable()
    bind = pythoncom.CreateBindCtx(0)
    rows = []
    for i, moniker in enumerate(rot.EnumRunning()):
        if i >= 128:
            break
        try:
            name = moniker.GetDisplayName(bind, None)
            key = hashlib.sha256(name.encode("utf-8")).hexdigest()[:24]
            rows.append((key, rot, moniker))
        except pythoncom.com_error:
            continue
    return rows


def discovery():
    import pythoncom
    import win32com.client
    rows, unavailable = [], 0
    for key, rot, moniker in rot_entries():
        try:
            dispatch = rot.GetObject(moniker).QueryInterface(pythoncom.IID_IDispatch)
            obj = win32com.client.Dispatch(dispatch)
            description = ComAdapter().describe(obj)
            rows.append({"rot_id": key, "type": description["type"], "properties": len(description["properties"]), "methods": len(description["methods"]), "framework": "COM IDispatch", "business_calls": 0})
        except (pythoncom.com_error, Fault, AttributeError):
            unavailable += 1
    return {"objects": rows, "unavailable": unavailable, "scope": "current-session registered running COM objects; at most 128", "business_calls": 0, "moniker_paths_saved": False}


class ComAdapter(PythonAdapter):
    runtime = "com"

    def __init__(self, cache_metadata=False):
        self.identities = {}
        self.next_identity = 0
        self.cache_metadata = cache_metadata
        self.descriptions = {}

    def identity(self, obj):
        import pythoncom
        unknown = obj._oleobj_.QueryInterface(pythoncom.IID_IUnknown)
        for key, value in self.identities.items():
            if value == unknown:
                return key
        self.next_identity += 1
        self.identities[self.next_identity] = unknown
        return self.next_identity

    def forget_identity(self, obj):
        key = self.identity(obj)
        self.identities.pop(key, None)
        self.descriptions.pop(key, None)

    @staticmethod
    def interfaces(info, diagnostics=None):
        """Visit inherited type information, excluding outgoing event interfaces."""
        pending, seen = [info], set()
        while pending and len(seen) < 32:
            current = pending.pop(0)
            attr = current.GetTypeAttr()
            key = (str(getattr(attr, 'guid', '')), getattr(attr, 'typekind', None),
                   current.GetDocumentation(-1)[0])
            if key in seen:
                continue
            seen.add(key)
            yield current, attr
            for index in range(min(getattr(attr, 'cImplTypes', 0), 32)):
                try:
                    if current.GetImplTypeFlags(index) & 2:  # IMPLTYPEFLAG_FSOURCE
                        continue
                    pending.append(current.GetRefTypeInfo(current.GetRefTypeOfImplType(index)))
                except Exception as exc:
                    if diagnostics is not None:
                        diagnostics.append({"interface": key[2], "base_index": index, "error": type(exc).__name__})
                    continue
        if pending and diagnostics is not None:
            diagnostics.append({"stopped_by": "interface_budget"})

    def describe(self, obj):
        identity = self.identity(obj) if self.cache_metadata else None
        if identity is not None and identity in self.descriptions:
            return self.descriptions[identity]
        try:
            info = obj._oleobj_.GetTypeInfo()
        except Exception as exc:
            raise Fault("type_info_unavailable", "The COM object does not provide ITypeInfo") from exc
        properties, methods, method_names = {}, [], set()
        interfaces, diagnostics = [], []
        for interface, attr in self.interfaces(info, diagnostics):
            interfaces.append(interface.GetDocumentation(-1)[0])
            for i in range(min(attr.cFuncs, 4096)):
                method = interface.GetFuncDesc(i)
                if method.wFuncFlags & (1 | 64):  # restricted or hidden
                    continue
                names = interface.GetNames(method.memid)
                if not names or names[0].startswith("_"):
                    continue
                name = names[0]
                # Vtable-oriented inherited interfaces may describe [out, retval]
                # explicitly. Such values are returned by IDispatch.Invoke, not
                # supplied as positional input by the caller.
                params, outputs = [], []
                returned_type = str(method.rettype[0])
                for j, arg in enumerate(method.args):
                    flags = arg[1]
                    parameter = {"name": names[j + 1] if j + 1 < len(names) else f"arg{j}",
                                 "type": str(arg[0]), "required": not bool(flags & 16), "flags": flags}
                    if flags & 8 or flags & 2 and not flags & 1:
                        outputs.append(parameter)
                        if flags & 8:
                            returned_type = parameter["type"]
                    else:
                        params.append(parameter)
                if method.invkind == 2 and not params or method.invkind in (4, 8) and len(params) == 1:
                    prop = properties.setdefault(name, {"name": name, "type": returned_type, "readable": False, "writable": False, "getter_executed": False, "dispid": method.memid})
                    prop["readable" if method.invkind == 2 else "writable"] = True
                    if method.invkind == 2:
                        prop["type"] = returned_type
                else:
                    suffix = {1: "", 2: "@get", 4: "@put", 8: "@putref"}.get(method.invkind)
                    if suffix is None or (name + suffix).casefold() in method_names:
                        continue
                    method_names.add((name + suffix).casefold())
                    methods.append({"name": name + suffix, "parameters": params, "out_parameters": outputs,
                                    "returns": returned_type, "effect": "unknown", "dispid": method.memid, "invkind": method.invkind,
                                    "callable": not any(not p['flags'] & 8 for p in outputs)})
        result = {"type": info.GetDocumentation(-1)[0], "properties": list(properties.values()), "methods": methods,
                  "events": [], "interfaces": interfaces, "inheritance_warnings": diagnostics,
                  "metadata_cache": bool(self.cache_metadata),
                  "discovery": "COM ITypeInfo with inherited interfaces; connection-point events not implemented"}
        if identity is not None:
            self.descriptions[identity] = result
        return result

    def read(self, obj, member):
        prop = next(p for p in self.describe(obj)["properties"] if p["name"] == member)
        return self.normalize(obj._oleobj_.Invoke(prop["dispid"], 0, 2, True))

    @staticmethod
    def normalize(value):
        # Direct IDispatch.Invoke returns a raw PyIDispatch for child objects.
        # Retain it as the same business object rather than a Python wrapper API.
        if type(value).__name__ == 'PyIDispatch':
            import win32com.client
            return win32com.client.Dispatch(value)
        return value

    def write(self, obj, member, value):
        prop = next(p for p in self.describe(obj)["properties"] if p["name"] == member)
        obj._oleobj_.Invoke(prop["dispid"], 0, 4, False, value)

    def call(self, obj, member, arguments):
        method = next(m for m in self.describe(obj)["methods"] if m["name"] == member)
        if not method.get("callable", True):
            raise Fault("unsupported_parameters", "Explicit by-reference output arguments are not implemented for this COM method")
        required = sum(p["required"] for p in method["parameters"])
        if not required <= len(arguments) <= len(method["parameters"]):
            raise Fault("invalid_arguments", "Incorrect COM argument count")
        return self.normalize(obj._oleobj_.Invoke(method["dispid"], 0, method["invkind"], True, *arguments))


def main():
    import pythoncom
    import win32com.client
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--discover", action="store_true")
    mode.add_argument("--rot", help="Exact hashed ID returned by discovery")
    mode.add_argument("--active", help="GetActiveObject ProgID; never launches an application")
    mode.add_argument("--create", help="Explicitly activate a registered COM application object")
    mode.add_argument("--hwnd", type=int, help="Window advertising OBJID_NATIVEOM")
    mode.add_argument("--window-tree", type=int, help="Bounded native-model probe of this window and its children")
    parser.add_argument("--pid", type=int, help="Expected owner PID for --hwnd")
    parser.add_argument("--cache-metadata", action="store_true", help="Cache stable COM type metadata per object for this provider session; values are never cached")
    mode.add_argument("--dictionary-demo", action="store_true", help="Create an owned Scripting.Dictionary COM object")
    mode.add_argument("--system-shell", action="store_true", help="Connect the standard Windows Shell.Application object; no window is opened")
    args = parser.parse_args()
    obj = host = None
    pythoncom.CoInitialize()
    try:
        if args.discover:
            sys.stdout.reconfigure(encoding="utf-8")
            print(json.dumps({"ok": True, "data": discovery()}, ensure_ascii=False))
            return
        if args.dictionary_demo:
            obj = win32com.client.Dispatch("Scripting.Dictionary")
        elif args.system_shell:
            obj = win32com.client.Dispatch("Shell.Application")
        elif args.active:
            obj = win32com.client.GetActiveObject(args.active)
        elif args.create:
            with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, args.create + '\\CLSID') as key:
                clsid = winreg.QueryValueEx(key, '')[0]
            obj = win32com.client.Dispatch(pythoncom.CoCreateInstance(pythoncom.MakeIID(clsid), None, pythoncom.CLSCTX_LOCAL_SERVER, pythoncom.IID_IDispatch))
        elif args.window_tree:
            if not args.pid:
                raise Fault('invalid_arguments', '--window-tree requires the expected --pid')
            roots = window_tree_objects(args.window_tree, args.pid)
        elif args.hwnd:
            obj = native_window_object(args.hwnd, args.pid)
        else:
            found = [row for row in rot_entries() if row[0] == args.rot]
            if len(found) != 1:
                raise Fault("stale_ref", "The selected running COM object is no longer registered")
            _, rot, moniker = found[0]
            obj = win32com.client.Dispatch(rot.GetObject(moniker).QueryInterface(pythoncom.IID_IDispatch))
        host = ObjectHost(roots if args.window_tree else {"application": obj}, adapter=ComAdapter(cache_metadata=args.cache_metadata), pump=pythoncom.PumpWaitingMessages)
        host.serve()
    finally:
        host = obj = None
        gc.collect()
        pythoncom.CoUninitialize()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # Initialization errors must be visible even when stderr is not retained.
        # Wait for the first handshake and return a matching protocol error.
        sys.stdin.reconfigure(encoding='utf-8')
        sys.stdout.reconfigure(encoding='utf-8')
        try:
            request = json.loads(sys.stdin.readline())
        except ValueError:
            request = {}
        code = getattr(exc, 'code', 'com_initialization_failed')
        hr = getattr(exc, 'hresult', None)
        print(json.dumps({'id': request.get('id'), 'ok': False, 'error': {'code': code, 'message': str(exc), 'hresult': f'0x{hr & 0xffffffff:08X}' if hr is not None else None}}, ensure_ascii=False), flush=True)
