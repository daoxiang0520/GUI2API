"""Explore installed code metadata and actual object providers for selected apps.

Code-only stages never load application modules. Dynamic inspection is a
separate, explicit stage and never invokes messaging, playback or document edits.
"""
import argparse
from datetime import datetime
import json
from pathlib import Path
import re
import subprocess
import sys
import threading
import queue

import psutil

from native_metadata import Asar, pe_file
from probe_app_objects import inventory, final_audit

ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / 'reports/business-objects-20261023'
REPORT = OUTPUT / 'discovery.json'


def save(report):
    OUTPUT.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')


def snapshot(report):
    report['apps'] = inventory()
    for name, app in report['apps'].items():
        main_pids = {w['pid'] for w in app['windows']}
        rows = [p for p in app['processes'] if p['pid'] in main_pids]
        app['main'] = rows
        app['modules'] = []
        for row in rows[:1]:
            try:
                app['modules'] = [{'name': Path(m.path).name, 'path': m.path} for m in psutil.Process(row['pid']).memory_maps(grouped=True)
                                  if any(key in Path(m.path).name.lower() for key in ('qt5', 'qt6', 'weixin', 'wrapper.node', 'qqnt.dll', 'libcef', 'cloudmusic.dll'))]
            except (psutil.NoSuchProcess, psutil.AccessDenied) as exc:
                app['module_error'] = type(exc).__name__
        print(json.dumps({'app': name, 'windows': app['windows'], 'main': rows, 'modules': [m['name'] for m in app['modules']]}, ensure_ascii=False), flush=True)


def exports(report):
    for name, app in report['apps'].items():
        app['native_metadata'] = []
        selected = app.get('modules', []) + [{'name': Path(p['exe']).name, 'path': p['exe']} for p in app.get('main', []) if p.get('exe')]
        seen = set()
        for module in selected[:40]:
            if module['path'] in seen:
                continue
            seen.add(module['path'])
            try:
                with pe_file(module['path']) as pe:
                    rows = pe.exports()
                    features = {
                        'qt_application_globals': [e for e in rows if 'QCoreApplication' in e['name'] and ('?self@' in e['name'] or '?instance@' in e['name'])],
                        'qt_static_metaobjects': [e for e in rows if 'staticMetaObject' in e['name']],
                        'qt_metadata_functions': [e for e in rows if any(s in e['name'] for s in ('?metaObject@QObject', '?className@QMetaObject', '?children@QObject'))],
                        'node_initializers': [e for e in rows if any(s in e['name'] for s in ('node_register', 'napi_register', 'node_module', 'napi_module', 'RegisterModule', 'InitModule'))],
                        'javascript_entrypoints': [e for e in rows if any(s in e['name'].lower() for s in ('cef_v8', 'get_browser', 'devtools', 'javascript', 'inspector', 'v8::'))],
                    }
                    item = {**module, 'pointer_size': pe.pointer_size, 'exports': len(rows), **features}
                    if len(rows) <= 80:
                        item['all_exports'] = rows
                    app['native_metadata'].append(item)
            except (OSError, ValueError) as exc:
                app['native_metadata'].append({**module, 'error': str(exc)})
        print(json.dumps({'app': name, 'modules': [{k: v for k, v in m.items() if k in ('name', 'pointer_size', 'exports', 'error') or k == 'qt_application_globals' or k == 'node_initializers'} for m in app['native_metadata']]}, ensure_ascii=False), flush=True)
        save(report)


def archive_probe(path):
    archive = Asar(path)
    code = [n for n in archive.entries if n.endswith(('.js', '.cjs', '.mjs', '.json'))]
    item = {'path': str(path), 'entries': len(archive.entries), 'code_files': len(code), 'evidence': [], 'bytes_scanned': 0}
    # Installed program code only. Never parse profiles, login data or chat DBs.
    patterns = {
        'native_service_access': re.compile(r'\b(get[A-Z][A-Za-z0-9_]*Service)\b'),
        'native_class': re.compile(r'\b(Node[A-Z][A-Za-z0-9_]{3,90})\b'),
        'ipc_channel': re.compile(r'\b(?:ipcRenderer|ipcMain)\.(?:handle|invoke|on|send)\(\s*[\'"]([A-Za-z][A-Za-z0-9_./:-]{2,96})[\'"]'),
        'module_load': re.compile(r'[\'"]([A-Za-z0-9_./\\-]{1,100}\.node)[\'"]'),
    }
    for name in code[:2000]:
        if item['bytes_scanned'] >= 32 * 1024 * 1024:
            item['stopped_by'] = 'code_byte_budget'
            break
        try:
            data = archive.read(name)
        except (ValueError, KeyError):
            continue
        item['bytes_scanned'] += len(data)
        text = data.decode('utf-8', 'replace')
        matches = {key: sorted(set(pattern.findall(text)))[:100] for key, pattern in patterns.items()}
        matches = {key: values for key, values in matches.items() if values}
        if matches:
            item['evidence'].append({'file': name, 'source': 'installed_code_identifiers', 'callable_verified': False, **matches})
    return item


def archives(report):
    for name, app in report['apps'].items():
        roots = {Path(p['exe']).parent for p in app.get('main', []) if p.get('exe')}
        roots.update(Path(m['path']).parent for m in app.get('modules', []) if m['name'].lower() == 'wrapper.node')
        paths = []
        for root in roots:
            for sub in (root, root / 'resources', root / 'resources/app', root / 'package', root / 'resource'):
                if sub.is_dir():
                    paths.extend(sub.glob('*.asar'))
        app['archives'] = []
        for path in list(dict.fromkeys(paths))[:8]:
            try:
                app['archives'].append(archive_probe(path))
            except (OSError, ValueError) as exc:
                app['archives'].append({'path': str(path), 'error': str(exc)})
        print(json.dumps({'app': name, 'archives': [{k: v for k, v in a.items() if k != 'evidence'} for a in app['archives']], 'code_evidence_entries': sum(len(a.get('evidence', [])) for a in app['archives'])}, ensure_ascii=False), flush=True)
        save(report)


def qt_self_test(report):
    from native_qt import inspect_qt, RemoteMemory, Qt5Inspector
    process = subprocess.Popen([sys.executable, str(ROOT / 'tests/fixtures/native_qt_process.py')], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8', creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    lines = queue.Queue()
    threading.Thread(target=lambda: lines.put(process.stdout.readline()), daemon=True).start()
    try:
        ready = json.loads(lines.get(timeout=8))
        assert ready['pid'] == process.pid and ready['ready'], ready
        result = inspect_qt(process.pid)
        if not result.get('ok'):
            import struct
            memory = RemoteMemory(process.pid)
            try:
                inspector = Qt5Inspector(memory, 8)
                diagnostic = {}
                for label in ('application', 'timer'):
                    address = ready[label]
                    vtable, private = struct.unpack('<QQ', memory.read(address, 16))
                    slots = [inspector.ptr(vtable + i * 8) for i in range(5)]
                    diagnostic[label] = {'qptr_valid': inspector.ptr(private + 8) == address,
                                         'slot_modules': [memory.module(s)['name'] if memory.module(s) else None for s in slots],
                                         'metaobject_disassembly': [f'{i.mnemonic} {i.op_str}' for i in inspector.disassembler.disasm(memory.read(slots[0], 128), slots[0])][:30],
                                         'object_found': bool(inspector.qobject(address))}
                report['qt_self_test_diagnostics'] = diagnostic
                print(json.dumps({'owned_qt_diagnostics': diagnostic}), flush=True)
            finally:
                memory.close()
        classes = {c['type']: c for c in result.get('classes', [])}
        assert result['ok'] and 'QCoreApplication' in classes and 'QTimer' in classes, result
        assert {'start', 'stop'} <= {m['name'] for m in classes['QTimer']['methods']}, classes['QTimer']
        assert 'interval' in {p['name'] for p in classes['QTimer']['properties']}
        from native_cpp import CppInspector
        memory = RemoteMemory(process.pid)
        try:
            native = CppInspector(memory)
            rtti = native.object(ready['timer'])
            report['cpp_self_test'] = {'ok': bool(rtti) and 'QTimer' in rtti['type'], 'class': rtti}
        finally:
            memory.close()
        report['qt_self_test'] = {'ok': True, 'owned_pid': process.pid, **result}
        print(json.dumps({'qt_self_test': True, 'cpp_self_test': report['cpp_self_test']['ok'], 'cpp_type': rtti['type'] if rtti else None, 'objects': result['statistics']['objects'], 'classes': list(classes), 'native_calls': 0, 'injected_code': False}), flush=True)
    finally:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)
        for stream in (process.stdout, process.stderr):
            stream.close()
        report.setdefault('qt_self_test', {})['owned_process_closed'] = process.poll() is not None
        save(report)


def qt_probe(report):
    from native_qt import inspect_qt
    if not report.get('qt_self_test', {}).get('ok'):
        raise RuntimeError('The owned native Qt self-test must pass before probing real apps')
    for name in ('wps', 'wechat'):
        app = report['apps'][name]
        if not app.get('main'):
            continue
        pid = app['main'][0]['pid']
        modules = [m['name'] for m in app.get('modules', []) if 'qt5core' in m['name'].lower() or m['name'].lower() == 'weixin.dll']
        try:
            result = inspect_qt(pid, modules, max_objects=256, max_seconds=8)
        except Exception as exc:
            result = {'ok': False, 'error': type(exc).__name__, 'message': str(exc), 'injected_code': False}
        app['native_qt_objects'] = result
        print(json.dumps({'app': name, 'native_qt': result.get('ok'), 'statistics': result.get('statistics'),
                          'classes': [c['type'] for c in result.get('classes', [])][:40], 'errors': result.get('message'), 'stopped_by': result.get('stopped_by')}, ensure_ascii=False), flush=True)
        save(report)


def cpp_probe(report):
    from native_cpp import inspect_cpp
    if not report.get('cpp_self_test', {}).get('ok'):
        raise RuntimeError('The owned native C++ RTTI self-test must pass before real apps')
    for name, modules in (('cloudmusic', ['cloudmusic.dll']), ('qq', ['wrapper.node', 'QQNT.dll'])):
        app = report['apps'][name]
        if not app.get('main'):
            continue
        try:
            result = inspect_cpp(app['main'][0]['pid'], modules)
        except Exception as exc:
            result = {'ok': False, 'error': type(exc).__name__, 'message': str(exc)}
        app['native_cpp_objects'] = result
        print(json.dumps({'app': name, 'native_cpp': result.get('ok'), 'statistics': result.get('statistics'), 'classes': [c['type'] for c in result.get('classes', [])][:35], 'error': result.get('message'), 'stopped_by': result.get('stopped_by')}, ensure_ascii=False), flush=True)
        save(report)


def explore_observations(report):
    from object_client import ObjectSession
    results = {}
    with ObjectSession(ROOT / 'objects.observations.json') as session:
        for app, target, query in (
            ('wechat', 'wechat-observed', 'chat message send'),
            ('wps', 'wps-observed', 'cache search transfer'),
            ('qq', 'qq-observed', 'service wrapper session'),
            ('cloudmusic', 'cloudmusic-observed', 'player library audio'),
        ):
            result = session.request('explore', {'target': target, 'query': query, 'max_objects': 256, 'max_depth': 8, 'max_requests': 1024, 'max_seconds': 25})
            path = OUTPUT / (app + '-exploration.json')
            path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
            data = result.get('data', {})
            results[app] = {'ok': result.get('ok'), 'path': str(path), 'statistics': data.get('statistics'),
                            'matches': len(data.get('matches', [])), 'status': data.get('status'),
                            'stopped_by': data.get('stopped_by'), 'error': result.get('error')}
            if result.get('ok'):
                assert data['statistics']['property_reads'] == data['statistics']['declared_read_calls'] == 0
                assert all(n.get('observation_only') and n.get('capabilities', {}).get('native_call') is False for n in data['nodes'])
                report['apps'][app]['observation_exploration'] = results[app]
            print(json.dumps({'app': app, 'unified_exploration': results[app]}, ensure_ascii=False), flush=True)
            save(report)
    report['unified_observation_results'] = results


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stage', choices=('snapshot', 'exports', 'archives', 'static', 'qt-self-test', 'qt', 'cpp', 'explore', 'audit', 'run'), default='static')
    args = parser.parse_args()
    report = json.loads(REPORT.read_text(encoding='utf-8')) if REPORT.exists() else {}
    report.update(observed_at=datetime.now().astimezone().isoformat(timespec='seconds'), screenshots=0, input_commands=0, business_mutations=0)
    if args.stage in ('snapshot', 'static', 'run'):
        snapshot(report)
        save(report)
    if args.stage in ('exports', 'static', 'run'):
        exports(report)
    if args.stage in ('archives', 'static', 'run'):
        archives(report)
    if args.stage in ('qt-self-test', 'run'):
        qt_self_test(report)
    if args.stage in ('qt', 'run'):
        qt_probe(report)
    if args.stage in ('cpp', 'run'):
        cpp_probe(report)
    if args.stage in ('explore', 'run'):
        explore_observations(report)
    report['audit'] = final_audit(report)
    save(report)
    print(json.dumps({'report': str(REPORT), 'original_windows_preserved': report['audit']['all_original_windows_preserved']}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
