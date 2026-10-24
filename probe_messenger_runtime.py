"""Bounded runtime inventory and owned-process checks for messenger bridges."""
from __future__ import annotations

import argparse
import ctypes
import json
from pathlib import Path
import subprocess
import sys
import time

import psutil

ROOT = Path(__file__).resolve().parent
REPORT = ROOT / 'reports' / 'messenger-runtime-20261024'


def inventory():
    from native_qt import RemoteMemory
    from native_metadata import pe_file
    user = ctypes.WinDLL('user32', use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    user.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
    rows = []

    @callback_type
    def visit(hwnd, _):
        pid = ctypes.c_uint32()
        tid = user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        title = ctypes.create_unicode_buffer(256)
        user.GetWindowTextW(ctypes.c_void_p(hwnd), title, 256)
        if title.value not in ('QQ', '微信', 'WeChat'):
            return True
        if not user.IsWindowVisible(ctypes.c_void_p(hwnd)):
            return True
        try:
            process = psutil.Process(pid.value)
            if process.name().lower() not in ('qq.exe', 'weixin.exe', 'wechat.exe'):
                return True
            record = {'pid': pid.value, 'tid': tid, 'window': hwnd,
                      'name': process.name(), 'exe': process.exe(),
                      'created_at': process.create_time(),
                      'minimized': bool(user.IsIconic(ctypes.c_void_p(hwnd)))}
            with_memory = RemoteMemory(pid.value)
            try:
                modules = [m for m in with_memory.modules if any(s in m['name'].lower()
                           for s in ('qt', 'qq.exe', 'qqnt.dll', 'wrapper.node', 'weixin.dll'))]
                record['modules'] = []
                for module in modules:
                    with pe_file(module['path']) as pe:
                        names = [e['name'] for e in pe.exports()]
                    selected = [n for n in names if n in (
                        '?instance@QCoreApplication@@SAPEAV1@XZ',
                        '?self@QCoreApplication@@0PEAV1@EA',
                        '?children@QObject@@QEBAAEBV?$QList@PEAVQObject@@@@XZ',
                        '?metacall@QMetaObject@@SAHPEAVQObject@@W4Call@1@HPEAPEAX@Z',
                        '?className@QMetaObject@@QEBAPEBDXZ',
                        '??0QString@@QEAA@XZ', '??1QString@@QEAA@XZ',
                        '??0QString@@QEAA@PEBVQChar@@H@Z',
                        'napi_get_cb_info', 'napi_create_string_utf8', 'napi_run_script',
                        'napi_open_handle_scope', 'napi_close_handle_scope',
                        'napi_get_value_string_utf8', 'napi_get_and_clear_last_exception',
                        'napi_is_exception_pending',
                    ) or n.startswith('?find@QWidget@@') or
                        (module['name'].lower() == 'weixin.dll' and any(s in n for s in ('Qt', 'QObject', 'QCore', 'Application', 'QMeta')))]
                    record['modules'].append({**module, 'export_count': len(names), 'exports': selected,
                                              'napi_exports': sum(n.startswith('napi_') for n in names)})
            finally:
                with_memory.close()
            rows.append(record)
        except (psutil.Error, OSError) as exc:
            rows.append({'pid': pid.value, 'error': str(exc)})
        return True

    user.EnumWindows(visit, 0)
    result = {'screenshots': 0, 'app_inputs': 0, 'target_code_injected': False,
              'windows': rows}
    REPORT.mkdir(parents=True, exist_ok=True)
    (REPORT / 'inventory.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return result


def napi_fixture():
    from napi_runtime import NodeApiRuntime
    REPORT.mkdir(parents=True, exist_ok=True)
    addon = REPORT / 'napi_tick.node'
    subprocess.run(['C:/msys64/ucrt64/bin/gcc.exe', '-shared', '-O2',
                    str(ROOT / 'bridges/napi_pump.c'), '-o', str(addon)], check=True,
                   capture_output=True)
    process = subprocess.Popen(['node', str(ROOT / 'tests/fixtures/napi_runtime.cjs'), str(addon)],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, encoding='utf-8', creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        ready = json.loads(process.stdout.readline())
        with NodeApiRuntime(ready['pid'], thread_id=ready['thread']) as bridge:
            bridge.start_pump()
            bridge.json('(()=>{clearInterval(appctlOwnedTimer);return true;})()')
            before = bridge.json('({count:appctlOwnedCounter.count,pid:process.pid})')
            result = bridge.json('appctlOwnedCounter.add(7)')
            after = bridge.json('appctlOwnedCounter.count')
            status = bridge.status()
            assert before['count'] == 0 and result == after == 7
            assert status['ownerThread'] == ready['thread']
        assert bridge.cleanup['pump_timer_stopped'] and bridge.cleanup['agent_detached']
        assert process.poll() is None
        report = {'ok': True, 'owned_process_only': True, 'thread_bound': True,
                  'before': before, 'after': after, 'status': status,
                  'cleanup': bridge.cleanup,
                  'detached_process_alive': True, 'screenshots': 0}
        (REPORT / 'napi-fixture.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
        print(json.dumps(report), flush=True)
    finally:
        process.terminate()
        process.communicate(timeout=5)


def qq_observe():
    from concurrent.futures import ThreadPoolExecutor
    from desktop import DesktopSession
    from napi_runtime import NodeApiRuntime
    records = inventory()['windows']
    targets = [r for r in records if r.get('name', '').lower() == 'qq.exe']
    if len(targets) != 1:
        raise RuntimeError('Exactly one visible QQ main window is required')
    target = targets[0]
    report = {'pid': target['pid'], 'screenshots': 0, 'messages_sent': 0,
              'target_code_injected': True, 'app_restarted': False}
    try:
        with NodeApiRuntime(target['pid'], expected_exe=target['exe'],
                            expected_created_at=target['created_at'], thread_id=target['tid'], api_module='QQNT.dll') as bridge:
            report['transport'] = bridge.info
            try:
                with DesktopSession() as desktop, ThreadPoolExecutor(max_workers=1) as pool:
                    future = pool.submit(bridge.start_pump, 8)
                    time.sleep(.05)
                    try:
                        if target['minimized']:
                            report['restore'] = desktop.request('window', {'window': target['window'], 'pid': target['pid'], 'state': 'restore'}).get('data')
                        result = desktop.request('inspect', {'window': target['window'], 'backend': 'auto',
                            'actionable': False, 'depth': 16, 'limit': 1200, 'time_budget_ms': 3500, 'probe_timeout': 8})
                        if result.get('ok'):
                            rows = result['data']['elements']
                            report['controls'] = [{k: r.get(k) for k in ('role', 'automation_id', 'class_name', 'backend', 'actions')}
                                for r in rows if r['role'] in ('Edit', 'Document')]
                            report['quality'] = result['data']['quality']
                        report['runtime'] = future.result(timeout=10)
                        report['electron'] = bridge.json("(()=>{const e=process.mainModule.require('electron');return {windows:e.BrowserWindow.getAllWindows().map(w=>({id:w.id,hwnd:w.getNativeWindowHandle().readBigUInt64LE(0).toString(),contents:w.webContents.id})),contents:e.webContents.getAllWebContents().map(w=>({id:w.id,type:w.getType()})),wrapperModules:Object.values(process.mainModule.require('module')._cache).filter(m=>/wrapper\\.node$/i.test(m.filename)).map(m=>({exports:Object.keys(m.exports).slice(0,100)}))};})()")
                        main = [w for w in report['electron']['windows'] if w['hwnd'] == str(target['window'])]
                        if len(main) != 1:
                            raise RuntimeError('Exactly one Electron BrowserWindow must match the selected HWND')
                        content_id = main[0]['contents']
                        renderer = "JSON.stringify(Array.from(document.querySelectorAll('input,textarea,[contenteditable=true],[role=textbox]')).slice(0,40).map(e=>({tag:e.tagName,id:e.id,classes:typeof e.className==='string'?e.className:'',role:e.getAttribute('role'),type:e.getAttribute('type'),placeholder:e.getAttribute('placeholder'),label:e.getAttribute('aria-label'),visible:!!(e.getBoundingClientRect().width&&e.getBoundingClientRect().height),editable:e.isContentEditable})))"
                        report['dom_controls'] = json.loads(bridge.await_json("process.mainModule.require('electron').webContents.fromId(" + str(content_id) + ").executeJavaScript(" + json.dumps(renderer) + ")"))
                    finally:
                        if target['minimized']:
                            report['window_restored'] = desktop.request('window', {'window': target['window'], 'pid': target['pid'], 'state': 'minimize'}).get('data', {}).get('minimized')
            finally:
                report['status'] = bridge.status()
        report['ok'] = True
    except Exception as exc:
        report['ok'] = False
        report['error'] = str(exc)
    (REPORT / 'qq-runtime.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False), flush=True)
    return report


def wechat_observe():
    from collections import Counter
    from desktop import DesktopSession
    from qt_accessibility import QtAccessibilityLease
    records = inventory()['windows']
    targets = [r for r in records if r.get('name', '').lower() in ('weixin.exe', 'wechat.exe')]
    if len(targets) != 1:
        raise RuntimeError('Exactly one visible WeChat main window is required')
    target = targets[0]
    report = {'pid': target['pid'], 'screenshots': 0, 'messages_sent': 0,
              'target_code_injected': False, 'app_restarted': False, 'values_read': False}

    def observe(desktop):
        result = desktop.request('inspect', {'window': target['window'], 'backend': 'uia3',
            'depth': 16, 'limit': 1500, 'time_budget_ms': 3500,
            'actionable': False, 'probe_timeout': 8})
        if not result.get('ok'):
            return {'ok': False, 'error': result.get('error')}
        data = result['data']
        rows = data['elements']
        controls = [{k: r.get(k) for k in ('role', 'name', 'automation_id', 'class_name', 'actions', 'offscreen')}
                    for r in rows if r.get('role') in ('Edit', 'Document') or
                    r.get('name') in ('搜索', '发送', '输入', '输入消息')]
        # No session names or message text are written to disk.
        for row in controls:
            if row.get('name') not in ('搜索', '发送', '输入', '输入消息', ''):
                row['name'] = '<label omitted>'
        return {'ok': True, 'quality': data['quality'], 'controls': controls,
                'roles': dict(Counter(r['role'] for r in rows)), 'truncated': data['truncated']}

    with DesktopSession() as desktop:
        report['before'] = observe(desktop)
        with QtAccessibilityLease(target['pid'], target['created_at'], expected_exe=target['exe']) as lease:
            report['gate_before'] = lease.status()
            report['gate_enabled'] = lease.enable()
            report['after'] = observe(desktop)
        report['restored'] = observe(desktop)
    report['ok'] = report['after'].get('ok', False)
    (REPORT / 'wechat-runtime.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False), flush=True)
    return report


def qq_editor_observe():
    from messenger_api import MessengerSession
    with MessengerSession('qq') as session:
        runtime = session._runtime
        js = """(()=>{
          const e=document.querySelector('.ProseMirror.ExEditor-qq-msg-editor');
          if(!e)return {found:false};
          const summary=o=>Object.fromEntries(Object.entries(Object.getOwnPropertyDescriptors(o||{})).map(([k,d])=>[k,'value' in d?typeof d.value:'accessor']));
          const parents=[];let p=e;
          for(let i=0;i<5&&p;i++,p=p.parentElement)parents.push({tag:p.tagName,classes:p.className,properties:summary(p),vue:p.__vueParentComponent?{type:p.__vueParentComponent.type.name||p.__vueParentComponent.type.__name,setup:summary(p.__vueParentComponent.setupState),ctx:summary(p.__vueParentComponent.ctx),exposed:summary(p.__vueParentComponent.exposed)}:null});
          return {found:true,desc:summary(e.pmViewDesc),parents};
        })()"""
        expression = "(()=>{const e=process.mainModule.require('electron');const w=e.BrowserWindow.getAllWindows().find(w=>w.getNativeWindowHandle().readBigUInt64LE(0).toString()===" + json.dumps(str(session._target['window'])) + ");return w.webContents.executeJavaScript(" + json.dumps(js) + ");})()"
        result = runtime.bridge.await_json(expression)
    (REPORT / 'qq-editor-structure.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False), flush=True)


def wechat_helper_observe(open_result=False):
    from messenger_api import MessengerSession
    report = {'screenshots': 0, 'messages_sent': 0, 'query': '文件传输助手'}
    with MessengerSession('wechat') as session:
        if session.search.text != '':
            raise RuntimeError('An initially empty search field is required')
        try:
            field = session._runtime._field('search')
            report['focus'] = session._desktop.request('focus', {'ref': field['ref']}).get('data')
            session.search.replace('文件传输助手', '')
            deadline = time.monotonic() + 3
            while True:
                rows = session._runtime._observe()
                matches = [r for r in rows if r.get('name', '').strip() == '文件传输助手']
                if matches or time.monotonic() >= deadline: break
                time.sleep(.2)
            report['search_lists'] = [{k:r.get(k) for k in ('role','class_name','automation_id')} for r in rows
                if 'search' in r.get('automation_id', '').lower() or 'search' in r.get('class_name', '').lower()]
            by_ref = {r['ref']: r for r in rows}
            report['matches'] = []
            for row in matches:
                parents = []
                parent = row.get('parent')
                for _ in range(6):
                    if parent not in by_ref: break
                    ancestor = by_ref[parent]
                    parents.append({k: ancestor.get(k) for k in ('role', 'class_name', 'automation_id')})
                    parent = ancestor.get('parent')
                report['matches'].append({**{k: row.get(k) for k in ('role', 'class_name', 'automation_id', 'actions', 'enabled')}, 'ancestors': parents})
            if open_result:
                try:
                    report['open_result'] = session._runtime.open_result('文件传输助手')
                except Exception as exc:
                    report['open_error'] = str(exc)
                finally:
                    report['status_after_open'] = session.status()
                    report['input_controls_after_open'] = [{k:r.get(k) for k in ('role', 'class_name', 'automation_id', 'actions')}
                        for r in session._runtime._observe() if any(s in r.get('class_name', '') for s in ('InputField', 'TextEdit'))]
        finally:
            current = session.search.text
            if current == '文件传输助手': session.search.replace('', '文件传输助手')
            report['search_restored'] = session.search.text == ''
    (REPORT / 'wechat-helper-structure.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False), flush=True)


def wechat_inputs():
    from messenger_api import MessengerSession
    with MessengerSession('wechat') as session:
        rows = session._runtime._observe()
        report = {'status': session.status(), 'inputs': [{**{k:r.get(k) for k in ('role', 'class_name', 'automation_id', 'actions')},
            'helper_label': r.get('name', '').strip() == '文件传输助手'} for r in rows if any(s in r.get('class_name', '') for s in ('InputField', 'TextEdit'))],
            'helper_labels': [{k:r.get(k) for k in ('role', 'class_name', 'automation_id')} for r in rows if r.get('name', '').strip() == '文件传输助手']}
    (REPORT / 'wechat-current-inputs.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False), flush=True)


def audit_cleanup():
    from messenger_api import MessengerSession
    from qt_accessibility import QtAccessibilityLease
    before = inventory()['windows']
    report = {'screenshots': 0, 'messages_sent': 0, 'app_inputs': 0}
    wechat = next(r for r in before if r.get('name') == 'Weixin.exe')
    with QtAccessibilityLease(wechat['pid'], wechat['created_at'], expected_exe=wechat['exe']) as lease:
        gate_before = lease.status()['active']
    with MessengerSession('wechat') as session:
        report['wechat_active_during_session'] = session._runtime.lease.status()['active']
    with QtAccessibilityLease(wechat['pid'], wechat['created_at'], expected_exe=wechat['exe']) as lease:
        report['wechat_gate_restored'] = lease.status()['active'] == gate_before
    with MessengerSession('qq') as session:
        runtime = session._runtime
        bridge = runtime.bridge
        keys = bridge.json("Object.getOwnPropertyNames(globalThis).filter(k=>/^__appctl_(pump|fields|job)_/.test(k))")
        report['qq_orphan_bridge_globals'] = len(set(keys) - {bridge.pump_key, runtime.key})
    report['qq_cleanup'] = bridge.cleanup
    after = inventory()['windows']
    signature = lambda rows: sorted((r['pid'], r['created_at'], r['window'], r['minimized']) for r in rows if 'name' in r)
    report['window_identity_and_minimized_state_preserved'] = signature(before) == signature(after)
    report['ok'] = all((report['wechat_gate_restored'],
        report['qq_cleanup']['pump_timer_stopped'], report['qq_cleanup']['agent_detached'],
        report['qq_orphan_bridge_globals'] == 0, report['window_identity_and_minimized_state_preserved']))
    (REPORT / 'cleanup-audit.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False), flush=True)
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--stage', choices=['inventory', 'napi-fixture', 'qq-observe', 'wechat-observe', 'qq-editor-observe', 'wechat-helper-observe', 'wechat-helper-open', 'wechat-inputs', 'audit-cleanup'], default='inventory')
    args = parser.parse_args()
    {'inventory': inventory, 'napi-fixture': napi_fixture, 'qq-observe': qq_observe,
     'wechat-observe': wechat_observe, 'qq-editor-observe': qq_editor_observe,
     'wechat-helper-observe': wechat_helper_observe, 'wechat-helper-open': lambda: wechat_helper_observe(True),
     'wechat-inputs': wechat_inputs, 'audit-cleanup': audit_cleanup}[args.stage]()
