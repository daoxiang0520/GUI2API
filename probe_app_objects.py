"""Bounded, read-only discovery of object interfaces in four selected apps.

No GUI input, process injection, app restart, account config reads, message sends
or playback actions. Only current app-family listeners are queried for standard
CDP discovery, and only published read-only COM properties are used.
"""
from __future__ import annotations

import argparse
import ctypes
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import winreg
from urllib.error import HTTPError

import psutil
import win32api

from cdp_backend import fetch_json
from object_client import ObjectSession

ROOT = Path(__file__).resolve().parent
REPORT = ROOT / 'app-object-probe.json'
FAMILIES = {
    'wechat': {'weixin.exe', 'wechat.exe'},
    'qq': {'qq.exe', 'qqnt.exe'},
    'wps': {'wps.exe', 'et.exe', 'wpp.exe', 'wpspdf.exe', 'wpscloudsvr.exe', 'wpscenter.exe'},
    'cloudmusic': {'cloudmusic.exe', 'cloudmusichelper.exe'},
}
PROGIDS = ('kwps.Application', 'kwps.Application.9', 'wps.Application', 'ket.Application', 'ket.Application.9', 'et.Application', 'kwpp.Application', 'kwpp.Application.9', 'wpp.Application')


def windows(pids):
    user = ctypes.WinDLL('user32', use_last_error=True)
    callback = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    user.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
    user.GetClassNameW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
    user.IsWindowVisible.argtypes = user.IsIconic.argtypes = [ctypes.c_void_p]
    rows = []
    def add(hwnd, unused):
        pid = ctypes.c_uint32()
        user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in pids and user.IsWindowVisible(hwnd):
            cls = ctypes.create_unicode_buffer(256)
            user.GetClassNameW(hwnd, cls, 256)
            rows.append({'window': int(hwnd), 'pid': pid.value, 'class': cls.value, 'minimized': bool(user.IsIconic(hwnd))})
        return True
    user.EnumWindows(callback(add), 0)
    return rows


def version(path):
    try:
        data = win32api.GetFileVersionInfo(path, '\\')
        return '.'.join(str(v) for v in (data['FileVersionMS'] >> 16, data['FileVersionMS'] & 65535, data['FileVersionLS'] >> 16, data['FileVersionLS'] & 65535))
    except Exception:
        return None


def inventory():
    apps = {key: {'processes': [], 'listeners': [], 'windows': []} for key in FAMILIES}
    processes = list(psutil.process_iter(['pid', 'ppid', 'name', 'exe']))
    mapping = {}
    for process in processes:
        name = (process.info.get('name') or '').lower()
        for family, names in FAMILIES.items():
            if name in names:
                mapping[process.pid] = family
    for _ in range(3):
        for process in processes:
            if process.info.get('ppid') in mapping and process.pid not in mapping:
                mapping[process.pid] = mapping[process.info['ppid']]
    for process in processes:
        family = mapping.get(process.pid)
        if not family:
            continue
        info = process.info
        row = {'pid': process.pid, 'parent': info.get('ppid'), 'name': info.get('name'), 'exe': info.get('exe'), 'version': version(info['exe']) if info.get('exe') else None}
        try:
            row['debug_flags'] = [v for v in process.cmdline() if v.startswith(('--remote-debugging-port', '--remote-debugging-pipe', '--inspect=', '--inspect-port='))]
            row['framework_modules'] = sorted({Path(m.path).name for m in process.memory_maps(grouped=True) if any(n in Path(m.path).name.lower() for n in ('qt5core', 'qt6core', 'libcef', 'chrome.dll', 'wrapper.node', 'qqnt.dll', 'wechatwin', 'weixin.dll', 'clr.dll', 'coreclr.dll'))})
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            row['process_detail_error'] = 'unavailable'
        apps[family]['processes'].append(row)
    for row in windows(set(mapping)):
        apps[mapping[row['pid']]]['windows'].append(row)
    try:
        for connection in psutil.net_connections(kind='tcp'):
            if connection.pid not in mapping or connection.status != psutil.CONN_LISTEN:
                continue
            if connection.laddr.ip in ('0.0.0.0', '127.0.0.1', '::', '::1'):
                apps[mapping[connection.pid]]['listeners'].append({'pid': connection.pid, 'address': connection.laddr.ip, 'port': connection.laddr.port})
    except psutil.AccessDenied:
        for app in apps.values():
            app['listener_error'] = 'access_denied'
    return apps


def registered_com():
    rows = []
    for progid in PROGIDS:
        candidates = []
        for view, flag in (('64', winreg.KEY_WOW64_64KEY), ('32', winreg.KEY_WOW64_32KEY)):
            try:
                with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, progid + '\\CLSID', 0, winreg.KEY_READ | flag) as key:
                    clsid = winreg.QueryValueEx(key, '')[0]
                candidates.append({'view': view, 'clsid': clsid})
            except FileNotFoundError:
                pass
        rows.append({'progid': progid, 'registered': bool(candidates), 'registrations': candidates})
    return rows


def cdp_probe(app):
    checked, endpoints = [], []
    seen = set()
    for listener in app['listeners']:
        port = listener['port']
        if port in seen or len(seen) >= 12:
            continue
        seen.add(port)
        endpoint = ('http://[::1]:' if listener['address'] in ('::', '::1') else 'http://127.0.0.1:') + str(port)
        result = {'pid': listener['pid'], 'port': port}
        try:
            details = fetch_json(endpoint, '/json/version', timeout=.6)
            if not isinstance(details, dict) or not ('Browser' in details or 'webSocketDebuggerUrl' in details):
                raise ValueError('not CDP')
            pages = fetch_json(endpoint, '/json/list', timeout=.6)
            result['cdp'] = True
            result['engine'] = details.get('Browser')
            result['targets'] = [{'id': p.get('id'), 'type': p.get('type'), 'debug_socket': bool(p.get('webSocketDebuggerUrl'))} for p in pages] if isinstance(pages, list) else []
            endpoints.append({'endpoint': endpoint, **result})
        except Exception as exc:
            result.update(cdp=False, error=type(exc).__name__)
        checked.append(result)
    return {'checked': checked, 'endpoints': endpoints, 'listener_scope': 'selected app family only; at most 12 ports'}


def com_probe(registrations):
    output = []
    with ObjectSession() as session:
        for row in registrations:
            if not row['registered']:
                continue
            result = session.request('attach', {'progid': row['progid']})
            item = {'progid': row['progid'], 'attached': result.get('ok'), 'error': result.get('error')}
            if result.get('ok'):
                ref = result['data']['objects'][0]['ref']
                desc = session.request('describe', {'ref': ref})
                if desc.get('ok'):
                    data = desc['data']
                    item['type'] = data['type']
                    item['properties'] = len(data['properties'])
                    item['methods'] = len(data['methods'])
                    item['known_members'] = [m['name'] for m in data['methods'] if m['name'].split('@')[0] in ('CentimetersToPoints', 'InchesToPoints', 'GetVersion')]
                    reads = {}
                    for prop in data['properties']:
                        if prop['name'] in ('Name', 'Version', 'Build') and prop['readable']:
                            read = session.request('get', {'ref': ref, 'member': prop['name']})
                            reads[prop['name']] = read.get('data', {}).get('result') if read.get('ok') else read.get('error')
                    item['readback'] = reads
            output.append(item)
    return output


def media_probe():
    powershell = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    try:
        process = subprocess.run([str(powershell), '-NoLogo', '-NoProfile', '-NonInteractive', '-Mta', '-File', str(ROOT / 'bridges/Probe-MediaObjects.ps1')], capture_output=True, encoding='utf-8', timeout=12, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        return json.loads(process.stdout.lstrip('\ufeff')) if process.returncode == 0 else {'ok': False, 'error': process.stderr[-1500:]}
    except (subprocess.TimeoutExpired, ValueError) as exc:
        return {'ok': False, 'error': type(exc).__name__}


def qq_module_probe(app):
    import shutil
    candidates = []
    for window in app['windows']:
        try:
            candidates.extend(m.path for m in psutil.Process(window['pid']).memory_maps(grouped=True) if Path(m.path).name.lower() == 'wrapper.node')
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            continue
    if not candidates:
        return {'ok': False, 'error': 'loaded_wrapper_not_found'}
    module = Path(candidates[0])
    directories = [module.parent, module.parents[1], module.parents[2]]
    for process in app['processes']:
        if process.get('exe'):
            directories.append(Path(process['exe']).parent)
    directories = list(dict.fromkeys(str(p) for p in directories))
    environment = {**os.environ, 'PATH': os.pathsep.join(directories) + os.pathsep + os.environ.get('PATH', '')}
    try:
        process = subprocess.run([shutil.which('node') or 'node', str(ROOT / 'bridges/probe_qq_module.mjs'), str(module)], cwd=module.parents[2], env=environment, capture_output=True, encoding='utf-8', errors='replace', timeout=12, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if process.returncode == 0:
            return json.loads(process.stdout.strip().splitlines()[-1])
        return {'ok': False, 'module': str(module), 'exit_code': process.returncode, 'error': process.stderr[-2000:], 'account_sessions_initialized': 0}
    except (subprocess.TimeoutExpired, ValueError) as exc:
        return {'ok': False, 'module': str(module), 'error': type(exc).__name__, 'account_sessions_initialized': 0}


def existing_onebot_probe(app):
    rows = []
    # Only services that already answered HTTP are candidates. Do not replay
    # requests against binary sockets or scan any additional ports.
    for item in app.get('cdp', {}).get('checked', []):
        if item.get('error') not in ('HTTPError', 'JSONDecodeError', 'ValueError'):
            continue
        row = {'port': item['port'], 'identified_onebot': False}
        try:
            value = fetch_json('http://127.0.0.1:' + str(item['port']), '/get_version_info', timeout=.8)
            data = value.get('data') if isinstance(value, dict) else None
            if isinstance(data, dict) and isinstance(data.get('app_name'), str) and value.get('retcode') == 0:
                row.update(identified_onebot=True, version={k: data[k] for k in ('app_name', 'app_version', 'protocol_version') if k in data})
            else:
                row['status'] = 'unrecognized_response; body not retained'
        except HTTPError as exc:
            row['http_status'] = exc.code
        except Exception as exc:
            row['error'] = type(exc).__name__
        rows.append(row)
    return rows


def automation_audit(report):
    rows = []
    for pid in report.get('wps_automation_activation', {}).get('new_application_pids_after_release', []):
        try:
            process = psutil.Process(pid)
            flags = [a for a in process.cmdline()[1:] if a.lower().startswith(('/automation','-automation','/embedding','-embedding','/com','-com'))]
            rows.append({'pid': pid, 'name': process.name(), 'parent': process.ppid(), 'exe': process.exe(), 'automation_flags': flags})
        except psutil.NoSuchProcess:
            rows.append({'pid': pid, 'exited': True})
    return rows


def cleanup_owned_wps(report):
    tracked = report.get('wps_automation_activation', {}).get('new_application_pids_after_release', [])
    baseline = {p['pid'] for p in report['apps']['wps']['processes']}
    expected_paths = {p['exe'].lower() for p in report['apps']['wps']['processes'] if p.get('exe') and p['name'].lower() == 'wps.exe'}
    result = {'tracked_pids': tracked, 'quit_attempts': 0, 'preexisting_wps_pids': sorted(baseline)}
    live = []
    for pid in tracked:
        if pid in baseline:
            return {**result, 'skipped': 'ownership_check_failed'}
        try:
            process = psutil.Process(pid)
            flags = {arg.lower() for arg in process.cmdline()[1:]}
            if process.exe().lower() not in expected_paths or '/automation' not in flags or '-embedding' not in flags:
                return {**result, 'skipped': 'ownership_check_failed'}
            live.append(process)
        except psutil.NoSuchProcess:
            pass
    if not live:
        return {**result, 'already_exited': True}
    with ObjectSession(ROOT / 'objects.apps.json') as session:
        listed = session.request('list', {'target': 'wps-active'})
        if not listed.get('ok'):
            return {**result, 'skipped': 'active_object_unavailable', 'error': listed.get('error')}
        ref = listed['data']['objects'][0]['ref']
        visible = session.request('get', {'ref': ref, 'member': 'Visible'})
        result['visible'] = visible.get('data', {}).get('result', {}).get('value')
        documents = session.request('get', {'ref': ref, 'member': 'Documents'})
        if not documents.get('ok') or documents['data']['result']['kind'] != 'object':
            return {**result, 'skipped': 'document_collection_unavailable'}
        count = session.request('get', {'ref': documents['data']['result']['ref'], 'member': 'Count'})
        result['document_count'] = count.get('data', {}).get('result', {}).get('value')
        if result['visible'] is not False or result['document_count'] != 0:
            return {**result, 'skipped': 'preserve_visible_or_nonempty_automation_instance'}
        result['quit_attempts'] = 1
        result['quit_result'] = session.request('call', {'ref': ref, 'member': 'Quit', 'arguments': []})
    for process in live:
        try:
            process.wait(timeout=3)
        except psutil.TimeoutExpired:
            # Do not force-close anything. Retain evidence for the caller.
            pass
    result['remaining_owned_pids'] = [p.pid for p in live if p.is_running()]
    result['original_main_windows_alive'] = all(psutil.pid_exists(w['pid']) for w in report['apps']['wps']['windows'])
    return result


def wps_protocol_check():
    before = {p.pid for p in psutil.process_iter(['name']) if (p.info.get('name') or '').lower() == 'wps.exe'}
    result = {'activation_requests': 1, 'documents_created': 0, 'documents_modified': 0}
    with ObjectSession(ROOT / 'objects.apps.json') as session:
        root = session.request('list', {'target': 'wps-automation'})
        if not root.get('ok'):
            return {**result, 'ok': False, 'error': root.get('error')}
        ref = root['data']['objects'][0]['ref']
        created = [p for p in psutil.process_iter(['name']) if p.pid not in before and (p.info.get('name') or '').lower() == 'wps.exe']
        owned = bool(created)
        for process in created:
            try:
                flags = {v.lower() for v in process.cmdline()[1:]}
                owned = owned and '/automation' in flags and '-embedding' in flags
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                owned = False
        result['created_automation_pids'] = [p.pid for p in created]
        visible = session.request('get', {'ref': ref, 'member': 'Visible'})
        is_visible = visible.get('data', {}).get('result', {}).get('value')
        count = None
        try:
            desc = session.request('describe', {'ref': ref})
            assert desc.get('ok'), desc
            result.update(type=desc['data']['type'], properties=len(desc['data']['properties']), methods=len(desc['data']['methods']))
            documents = session.request('get', {'ref': ref, 'member': 'Documents'})
            assert documents.get('ok') and documents['data']['result']['kind'] == 'object', documents
            doc_ref = documents['data']['result']['ref']
            count_read = session.request('get', {'ref': doc_ref, 'member': 'Count'})
            assert count_read.get('ok'), count_read
            count = count_read['data']['result']['value']
            converted = session.request('call', {'ref': ref, 'member': 'CentimetersToPoints', 'arguments': [1]})
            assert converted.get('ok'), converted
            result['pure_method_result'] = converted['data']['result']['value']
            assert abs(result['pure_method_result'] - 28.35) < .01
            result.update(ok=True, child_object_readback=True, document_count=count)
        finally:
            if owned and is_visible is False and count == 0:
                result['cleanup_quit'] = session.request('call', {'ref': ref, 'member': 'Quit', 'arguments': []})
            else:
                result['cleanup_skipped'] = 'Ownership/hidden/empty conditions were not all verified'
    for process in created:
        try:
            process.wait(timeout=3)
        except psutil.TimeoutExpired:
            pass
    result['remaining_created_pids'] = [p.pid for p in created if p.is_running()]
    return result


def final_audit(report):
    user = ctypes.WinDLL('user32', use_last_error=True)
    user.IsWindow.argtypes = [ctypes.c_void_p]
    user.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
    rows = []
    for name, app in report['apps'].items():
        for window in app['windows']:
            pid = ctypes.c_uint32()
            valid = bool(user.IsWindow(window['window']))
            if valid:
                user.GetWindowThreadProcessId(window['window'], ctypes.byref(pid))
            rows.append({'app': name, 'window': window['window'], 'same_window_and_pid': valid and pid.value == window['pid']})
    return {'original_windows': rows, 'all_original_windows_preserved': all(r['same_window_and_pid'] for r in rows)}


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stage', choices=('inventory', 'interfaces', 'native', 'extended', 'modules', 'services', 'audit', 'cleanup', 'protocol', 'finalize', 'all'), default='all')
    parser.add_argument('--activate-wps', action='store_true', help='Explicitly start one empty WPS automation instance for the extended-stage check')
    args = parser.parse_args()
    report = json.loads(REPORT.read_text(encoding='utf-8')) if REPORT.exists() else {}
    report.update(observed_at=datetime.now().astimezone().isoformat(timespec='seconds'), screenshots=0, input_commands=0, messages_sent=0, app_restarts=0, process_injections=0, personal_content_saved=False)
    if args.stage in ('inventory', 'all'):
        report['apps'] = inventory()
        report['wps_com_registrations'] = registered_com()
        REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        for name, app in report['apps'].items():
            print(json.dumps({'app': name, 'processes': len(app['processes']), 'windows': app['windows'], 'listeners': app['listeners'], 'versions': sorted({p['version'] for p in app['processes'] if p['version']})}, ensure_ascii=False), flush=True)
    if args.stage in ('interfaces', 'all'):
        for name, app in report['apps'].items():
            app['cdp'] = cdp_probe(app)
            print(json.dumps({'app': name, 'cdp_endpoints': len(app['cdp']['endpoints']), 'ports_checked': len(app['cdp']['checked'])}), flush=True)
            REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        report['wps_active_com'] = com_probe(report['wps_com_registrations'])
        report['media_sessions'] = media_probe()
    if args.stage in ('native', 'all'):
        for name, app in report['apps'].items():
            try:
                process = subprocess.run([sys.executable, str(ROOT / 'bridges/probe_native_models.py'), '--windows-json', json.dumps(app['windows'])], capture_output=True, encoding='utf-8', timeout=14, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                app['native_object_model'] = json.loads(process.stdout) if process.returncode == 0 else {'ok': False, 'error': process.stderr[-1200:]}
            except (subprocess.TimeoutExpired, ValueError) as exc:
                app['native_object_model'] = {'ok': False, 'error': type(exc).__name__}
            print(json.dumps({'app': name, 'native_model': app['native_object_model']}, ensure_ascii=False), flush=True)
            REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    if args.stage in ('extended', 'all') and args.activate_wps:
        report['wps_automation_activation'] = {'attempts': 1}
        REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        try:
            process = subprocess.run([sys.executable, str(ROOT / 'bridges/probe_wps_activation.py')], capture_output=True, encoding='utf-8', timeout=25, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            report['wps_automation_activation'] = json.loads(process.stdout) if process.returncode == 0 else {'ok': False, 'error': process.stderr[-1500:]}
        except (subprocess.TimeoutExpired, ValueError) as exc:
            report['wps_automation_activation'] = {'ok': False, 'error': type(exc).__name__, 'activation_outcome': 'unknown; no automatic retry'}
        print(json.dumps({'wps_automation': report['wps_automation_activation']}, ensure_ascii=False), flush=True)
    if args.stage in ('extended', 'all'):
        with ObjectSession(ROOT / 'objects.apps.json') as session:
            result = session.request('list', {'target': 'app-audio'})
            report['core_audio'] = result
            if result.get('ok'):
                rows = []
                for root in result['data']['objects']:
                    desc = session.request('describe', {'ref': root['ref']})
                    item = {'name': root['name'], 'description': desc}
                    item['readback'] = {}
                    if desc.get('ok'):
                        for prop in desc['data']['properties']:
                            if prop['name'] in ('ProcessId', 'ProcessName', 'State', 'Volume', 'Muted'):
                                value = session.request('get', {'ref': root['ref'], 'member': prop['name']})
                                item['readback'][prop['name']] = value.get('data', {}).get('result') if value.get('ok') else value.get('error')
                    rows.append(item)
                report['core_audio']['data']['observed_sessions'] = rows
            print(json.dumps({'core_audio': report['core_audio']}, ensure_ascii=False), flush=True)
    if args.stage in ('modules', 'all'):
        if 'qq_native_module' in report:
            report.setdefault('qq_native_module_attempts', []).append(report['qq_native_module'])
        report['qq_native_module'] = qq_module_probe(report['apps']['qq'])
        result = report['qq_native_module']
        print(json.dumps({'qq_native_module': {k: v for k, v in result.items() if k != 'exports'}, 'export_count': len(result.get('exports', [])), 'export_names': [e['name'] for e in result.get('exports', [])][:25]}, ensure_ascii=False), flush=True)
    if args.stage in ('services', 'all'):
        report['qq_existing_onebot'] = existing_onebot_probe(report['apps']['qq'])
        print(json.dumps({'qq_existing_onebot': report['qq_existing_onebot']}, ensure_ascii=False), flush=True)
    if args.stage in ('audit', 'all'):
        report['wps_automation_process_audit'] = automation_audit(report)
        report['wps_active_com_after_activation'] = com_probe([r for r in report['wps_com_registrations'] if r['progid'] in ('kwps.Application', 'ket.Application', 'kwpp.Application')])
        print(json.dumps({'automation_processes': report['wps_automation_process_audit'], 'active_com_after_activation': report['wps_active_com_after_activation']}, ensure_ascii=False), flush=True)
    if args.stage in ('cleanup', 'all'):
        report['wps_automation_cleanup'] = cleanup_owned_wps(report)
        print(json.dumps({'wps_automation_cleanup': report['wps_automation_cleanup']}, ensure_ascii=False), flush=True)
    if args.stage == 'protocol':
        report['wps_object_protocol_check'] = wps_protocol_check()
        print(json.dumps({'wps_object_protocol_check': report['wps_object_protocol_check']}, ensure_ascii=False), flush=True)
    if args.stage in ('finalize', 'all', 'protocol', 'services'):
        report['final_audit'] = final_audit(report)
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'report': str(REPORT), 'com': report.get('wps_active_com'), 'media': report.get('media_sessions')}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
