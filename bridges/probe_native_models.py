"""Read-only bounded native-object probe; no titles, contents or edits."""
import argparse
import ctypes
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bridges.com_agent import ComAdapter, native_window_object


def candidates(roots):
    user = ctypes.WinDLL('user32', use_last_error=True)
    cb_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    user.GetClassNameW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
    user.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
    user.EnumChildWindows.argtypes = [ctypes.c_void_p, cb_type, ctypes.c_void_p]
    rows = []
    seen = set()
    def add(hwnd, unused):
        if hwnd in seen or len(rows) >= 100:
            return len(rows) < 100
        seen.add(hwnd)
        cls = ctypes.create_unicode_buffer(256)
        pid = ctypes.c_uint32()
        user.GetClassNameW(hwnd, cls, 256)
        user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        rows.append({'window': int(hwnd), 'pid': pid.value, 'class': cls.value})
        return True
    for root in roots:
        add(root['window'], None)
        user.EnumChildWindows(root['window'], cb_type(add), None)
    preferred = ('_WwG', 'EXCEL7', 'paneClassDC', 'OpusApp', 'Qt5QWindowIcon')
    rows.sort(key=lambda row: (row['class'] not in preferred, row['window'] not in {r['window'] for r in roots}))
    return rows[:24]


def main():
    import pythoncom
    parser = argparse.ArgumentParser()
    parser.add_argument('--windows-json', required=True)
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding='utf-8')
    pythoncom.CoInitialize()
    rows = []
    for row in candidates(json.loads(args.windows_json)):
        item = dict(row)
        try:
            obj = native_window_object(row['window'], row['pid'])
            adapter = ComAdapter()
            desc = adapter.describe(obj)
            item.update(exposed=True, type=desc['type'], properties=len(desc['properties']), methods=len(desc['methods']))
            if any(p['name'] == 'Application' and p['readable'] for p in desc['properties']):
                obj = adapter.read(obj, 'Application')
                desc = adapter.describe(obj)
                item['application_type'] = desc['type']
            reads = {}
            for prop in desc['properties']:
                if prop['name'] in ('Name', 'Version', 'Build') and prop['readable']:
                    value = adapter.read(obj, prop['name'])
                    if type(value) in (str, int, float, bool):
                        reads[prop['name']] = value
            item['readback'] = reads
            methods = {m['name'] for m in desc['methods']}
            if 'CentimetersToPoints' in methods:
                item['pure_method_check'] = {'method': 'CentimetersToPoints', 'argument': 1, 'result': adapter.call(obj, 'CentimetersToPoints', [1])}
            obj = adapter = None
        except Exception as exc:
            item.update(exposed=False, error=getattr(exc, 'code', type(exc).__name__), hresult=f'0x{exc.hresult & 0xffffffff:08X}' if hasattr(exc, 'hresult') else None)
        rows.append(item)
    print(json.dumps({'ok': True, 'data': {'windows': rows, 'exposed': sum(r['exposed'] for r in rows), 'documents_read': 0, 'mutations': 0}}, ensure_ascii=False))
    pythoncom.CoUninitialize()


if __name__ == '__main__':
    main()
