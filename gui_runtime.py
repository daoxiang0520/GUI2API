"""Two transports for the exact same, app-independent DOM adapter."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import time
import uuid

from object_bridge import Fault

ROOT = Path(__file__).resolve().parent
ENGINE = ROOT / 'bridges/gui_dom.js'


class CdpDOM:
    def __init__(self, endpoint, target):
        from cdp_backend import CdpBackend
        self.backend = CdpBackend()
        self.context = {'endpoint': endpoint, 'target': target}
        self.key = '__appctl_gui_' + uuid.uuid4().hex
        self.object_id = None
        self.connection = None
        self.epoch = None

    def request(self, request):
        from cdp_backend import DriverFault
        dispatched = False
        try:
            conn, _ = self.backend._connection(self.context)
            conn.drain()
            if request['op'] == 'scan' and not request.get('cursor'):
                source = 'globalThis[' + json.dumps(self.key) + ']??=((' + ENGINE.read_text(encoding='utf-8') + ')())'
                result = conn.rpc('Runtime.evaluate', {'expression': source})
                self.object_id = result.get('result', {}).get('objectId')
                if not self.object_id:
                    raise Fault('runtime_unavailable', 'The DOM adapter could not be installed')
                self.connection, self.epoch = conn.instance, conn.epoch
            if not self.object_id or conn.instance != self.connection or conn.epoch != self.epoch:
                raise Fault('stale_ref', 'The page navigated or the connection was replaced; scan again')
            dispatched = True
            result = conn.rpc('Runtime.callFunctionOn', {
                'objectId': self.object_id, 'functionDeclaration': 'function(request){return this.dispatch(request)}',
                'arguments': [{'value': request}], 'returnByValue': True, 'awaitPromise': True,
                'userGesture': request.get('method') not in (None, 'get')})
            if 'exceptionDetails' in result:
                raise Fault('outcome_unknown' if request['op'] != 'scan' and request.get('method') != 'get' else 'runtime_error',
                            'The runtime context rejected the request; it was not retried')
            return result.get('result', {}).get('value', {})
        except DriverFault as exc:
            code = 'outcome_unknown' if dispatched and request.get('method') not in (None, 'get') else exc.code
            raise Fault(code, str(exc)) from exc

    def close(self):
        cleanup = {'renderer_global_removed': False}
        try:
            if self.backend.connections:
                conn, _ = self.backend._connection(self.context)
                conn.rpc('Runtime.evaluate', {'expression': 'delete globalThis[' + json.dumps(self.key) + ']'})
                cleanup['renderer_global_removed'] = True
        except Exception:
            pass
        finally:
            self.backend.close()
        return cleanup


class ElectronDOM:
    """Bootstrap only binds a process/window; it contains no feature selectors."""
    def __init__(self, desktop, target, api_module=None):
        from napi_runtime import NodeApiRuntime
        self.desktop, self.target = desktop, target
        self.bridge = None
        self.key = '__appctl_gui_' + uuid.uuid4().hex
        self.restored = False
        try:
            self.bridge = NodeApiRuntime(target['pid'], expected_exe=target['exe'],
                expected_created_at=target['created_at'], thread_id=target['tid'], api_module=api_module)
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(self.bridge.start_pump, 8)
                time.sleep(.05)
                if target['minimized']:
                    self._checked(desktop.request('window', {'window': target['window'], 'pid': target['pid'], 'state': 'restore'}))
                    self.restored = True
                self._checked(desktop.request('inspect', {'window': target['window'], 'backend': 'auto',
                    'actionable': False, 'depth': 10, 'limit': 500, 'time_budget_ms': 2000, 'probe_timeout': 8}))
                future.result(timeout=10)
            factory = r'''(function(hwnd,key,engine){
              const electron=process.mainModule.require('electron');
              const windows=electron.BrowserWindow.getAllWindows().filter(w=>w.getNativeWindowHandle().readBigUInt64LE(0).toString()===hwnd);
              if(windows.length!==1)throw new Error('ambiguous_native_window');
              const contents=windows[0].webContents;
              return {
                async dispatch(request){
                  if(contents.isDestroyed())throw new Error('stale_window');
                  const prefix=request.op==='scan'&&!request.cursor
                    ? 'globalThis['+JSON.stringify(key)+']??=(('+engine+')());' : '';
                  return contents.executeJavaScript(prefix+
                    '(async()=>{const api=globalThis['+JSON.stringify(key)+'];if(!api)return {error:"stale_ref",message:"Renderer navigated; scan again"};return api.dispatch('+JSON.stringify(request)+');})()',request.op!=='scan'&&request.method!=='get');
                },
                async close(){if(!contents.isDestroyed())await contents.executeJavaScript('delete globalThis['+JSON.stringify(key)+']');return true;}
              };
            })'''
            expression = '(' + factory + ')(' + ','.join(json.dumps(v, ensure_ascii=True) for v in
                (str(target['window']), self.key, ENGINE.read_text(encoding='utf-8'))) + ')'
            self.bridge.json('(()=>{globalThis[' + json.dumps(self.key) + ']=' + expression + ';return true;})()')
        except Exception:
            self.close()
            raise

    @staticmethod
    def _checked(result):
        if not result.get('ok'):
            e = result['error']
            raise Fault(e['code'], e['message'])
        return result['data']

    def request(self, request):
        expression = 'globalThis[' + json.dumps(self.key) + '].dispatch(' + json.dumps(request, ensure_ascii=True) + ')'
        try:
            return self.bridge.await_json(expression)
        except Exception as exc:
            raise Fault('outcome_unknown' if request.get('method') not in (None, 'get') else 'runtime_error', str(exc)) from exc

    def close(self):
        cleanup = {'renderer_global_removed': False, 'main_global_removed': False}
        try:
            if self.bridge:
                try:
                    self.bridge.await_json('globalThis[' + json.dumps(self.key) + ']?.close()', 2)
                    cleanup['renderer_global_removed'] = True
                except Exception:
                    pass
                try:
                    self.bridge.json('(()=>{delete globalThis[' + json.dumps(self.key) + '];return true;})()', 1)
                    cleanup['main_global_removed'] = True
                except Exception:
                    pass
                cleanup.update(self.bridge.close())
                self.bridge = None
        finally:
            if self.restored:
                state = self.desktop.request('window', {'window': self.target['window'], 'pid': self.target['pid'], 'state': 'minimize'})
                cleanup['window_restored'] = bool(state.get('ok'))
                self.restored = False
        return cleanup
