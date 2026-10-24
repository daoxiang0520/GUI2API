"""One explicit WPS automation activation, followed only by safe API reads.

No documents are created, opened, edited or saved. Quit is allowed only if the
application exposes a HWND from a newly created process and zero documents.
"""
import ctypes
import gc
import json
from pathlib import Path
import sys
import time
import winreg

import psutil
import pythoncom
import win32com.client

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bridges.com_agent import ComAdapter


def pids():
    return {p.pid for p in psutil.process_iter(['name']) if (p.info.get('name') or '').lower() in ('wps.exe', 'et.exe', 'wpp.exe')}


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    report = {'ok': False, 'progid': 'kwps.Application', 'activation_attempted': False, 'documents_created': 0, 'documents_modified': 0}
    before = pids()
    obj = adapter = collection = None
    pythoncom.CoInitialize()
    try:
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, r'kwps.Application\CLSID') as key:
            clsid = winreg.QueryValueEx(key, '')[0]
        report['clsid'] = clsid
        report['activation_attempted'] = True
        dispatch = pythoncom.CoCreateInstance(pythoncom.MakeIID(clsid), None, pythoncom.CLSCTX_LOCAL_SERVER, pythoncom.IID_IDispatch)
        obj = win32com.client.Dispatch(dispatch)
        dispatch = None
        adapter = ComAdapter()
        desc = adapter.describe(obj)
        report.update(ok=True, type=desc['type'], properties=len(desc['properties']), methods=len(desc['methods']))
        props = {p['name']: p for p in desc['properties']}
        report['metadata'] = {k: adapter.read(obj, k) for k in ('Name', 'Version', 'Build') if k in props and props[k]['readable']}
        hwnd = next((adapter.read(obj, k) for k in ('Hwnd', 'HWND', 'HWnd') if k in props and props[k]['readable']), None)
        if hwnd:
            pid = ctypes.c_uint32()
            user = ctypes.WinDLL('user32')
            user.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
            user.GetWindowThreadProcessId(int(hwnd), ctypes.byref(pid))
            report['automation_window_pid'] = pid.value
            report['new_process_ownership_verified'] = pid.value not in before and pid.value in pids()
        if 'Documents' in props and props['Documents']['readable']:
            collection = adapter.read(obj, 'Documents')
            collection_desc = adapter.describe(collection)
            if any(p['name'] == 'Count' and p['readable'] for p in collection_desc['properties']):
                report['document_count'] = adapter.read(collection, 'Count')
        methods = {m['name'] for m in desc['methods']}
        if 'CentimetersToPoints' in methods:
            report['pure_method_check'] = {'method': 'CentimetersToPoints', 'argument': 1, 'result': adapter.call(obj, 'CentimetersToPoints', [1])}
        if report.get('new_process_ownership_verified') and report.get('document_count') == 0 and 'Quit' in methods:
            adapter.call(obj, 'Quit', [])
            report['owned_empty_instance_quit'] = True
        else:
            report['owned_empty_instance_quit'] = False
            report['cleanup'] = 'COM references released; no pre-existing application was quit'
    except Exception as exc:
        hr = getattr(exc, 'hresult', None)
        report['error'] = {'type': type(exc).__name__, 'message': str(exc), 'hresult': f'0x{hr & 0xffffffff:08X}' if hr is not None else None}
    finally:
        obj = collection = adapter = None
        gc.collect()
        pythoncom.CoUninitialize()
        time.sleep(.2)
        report['new_application_pids_after_release'] = sorted(pids() - before)
        print(json.dumps(report, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
