"""Explicit, process-bound transport into an already running Node-API host.

Private evaluate() is for trusted local bridge implementation, never a public
object-protocol member. Application APIs must expose fixed, typed operations.
"""
from __future__ import annotations

import json
import hashlib
from pathlib import Path
import re
import shutil
import subprocess
import time
import uuid

import frida
import psutil


class NodeApiRuntime:
    def __init__(self, pid, *, expected_exe=None, expected_created_at=None, thread_id=None, api_module=None):
        process = psutil.Process(pid)
        self.identity = {'pid': pid, 'exe': process.exe(), 'created_at': process.create_time()}
        if expected_exe is not None and Path(process.exe()).resolve() != Path(expected_exe).resolve():
            raise ValueError('Process executable changed')
        if expected_created_at is not None and process.create_time() != expected_created_at:
            raise ValueError('Process identity changed')
        self.session = self.script = None
        self.closed = False
        self.pump_key = '__appctl_pump_' + uuid.uuid4().hex
        self.api_module = api_module
        self.pump_started = False
        self.cleanup = {'pump_timer_stopped': None, 'agent_detached': False,
                        'addon_cached_until_process_exit': False}
        try:
            self.session = frida.attach(pid)
            self.script = self.session.create_script(
                Path(__file__).with_name('bridges').joinpath('napi_runtime.js').read_text(encoding='utf-8'))
            self.script.load()
            self.info = self.script.exports_sync.init(thread_id, api_module)
        except Exception:
            self.close()
            raise

    def status(self):
        return self.script.exports_sync.status()

    def start_pump(self, timeout=8):
        """Load our tiny stable-ABI addon and retain a non-owning event-loop tick."""
        host = self.api_module
        if host is not None and not re.fullmatch(r'[A-Za-z0-9_.-]+', host):
            raise ValueError('Invalid API host module name')
        source = Path(__file__).with_name('bridges').joinpath('napi_pump.c').read_text(encoding='utf-8')
        source = '#define APPCTL_NAPI_HOST ' + (json.dumps(host) if host else 'NULL') + '\n' + source
        digest = hashlib.sha256(source.encode()).hexdigest()[:16]
        directory = Path(__file__).with_name('native') / 'runtime'
        directory.mkdir(exist_ok=True)
        addon = directory / ('napi-pump-' + digest + '.node')
        if not addon.exists():
            compiler = shutil.which('gcc')
            if not compiler:
                raise RuntimeError('A local x64 C compiler is required to build the Node-API pump')
            c_file = directory / ('napi-pump-' + digest + '.c')
            c_file.write_text(source, encoding='utf-8')
            subprocess.run([compiler, '-shared', '-O2', str(c_file), '-o', str(addon)],
                           check=True, capture_output=True, timeout=30,
                           creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        expression = ('(()=>{const addon=process.mainModule.require(' + json.dumps(str(addon)) + ');'
            'const timer=setInterval(()=>addon.tick(),40);timer.unref();globalThis[' + json.dumps(self.pump_key)
            + ']={timer};return {pid:process.pid,versions:process.versions,pump:true};})()')
        # Mark before requesting: an unknown outcome still requires cleanup.
        self.pump_started = True
        result = self.json(expression, timeout)
        self.cleanup['addon_cached_until_process_exit'] = True
        return result

    def evaluate(self, source, timeout=5):
        """Execute trusted bridge code once; no retries after unknown outcomes."""
        if self.closed:
            raise RuntimeError('Bridge closed')
        current = psutil.Process(self.identity['pid'])
        if current.create_time() != self.identity['created_at']:
            raise RuntimeError('Process identity changed')
        return self.script.exports_sync.execute(uuid.uuid4().hex, source, int(timeout * 1000))

    def json(self, expression, timeout=5):
        return json.loads(self.evaluate('JSON.stringify(' + expression + ')', timeout))

    def await_json(self, expression, timeout=8):
        """Await trusted promise code; poll its result, never repeat the action."""
        key = '__appctl_job_' + uuid.uuid4().hex
        literal = json.dumps(key)
        self.json('(()=>{const k=' + literal + ';const s={done:false,cancelled:false};'
                  'globalThis[k]=s;Promise.resolve().then(()=>{if(s.cancelled)throw new Error("cancelled");return ('
                  + expression + ');}).then(v=>{s.done=true;s.value=v;},e=>{s.done=true;s.error=String(e);});return true;})()', min(timeout, 5))
        deadline = time.monotonic() + timeout
        try:
            while time.monotonic() < deadline:
                result = self.json('(()=>{const s=globalThis[' + literal + '];return s.done?{done:true,value:s.value,error:s.error}:{done:false};})()', min(3, max(.2, deadline - time.monotonic())))
                if result['done']:
                    if result.get('error'):
                        raise RuntimeError(result['error'])
                    return result.get('value')
                time.sleep(.05)
            raise TimeoutError('Runtime operation outcome unknown; no retry was attempted')
        finally:
            try:
                self.json('(()=>{const k=' + literal + ';if(globalThis[k])globalThis[k].cancelled=true;delete globalThis[k];return true;})()', .5)
            except Exception:
                pass

    def close(self):
        if self.closed:
            return self.cleanup
        if self.script and self.pump_started:
            try:
                self.cleanup['pump_timer_stopped'] = self.json('(()=>{const k=' + json.dumps(self.pump_key) + ';if(globalThis[k])clearInterval(globalThis[k].timer);delete globalThis[k];return !(k in globalThis);})()', .75)
            except Exception:
                self.cleanup['pump_timer_stopped'] = False
            self.pump_started = False
        self.closed = True
        if self.script:
            try:
                self.script.exports_sync.close()
                self.script.unload()
            except (frida.InvalidOperationError, frida.TransportError):
                pass
        if self.session:
            try:
                self.session.detach()
                self.cleanup['agent_detached'] = True
            except frida.InvalidOperationError:
                self.cleanup['agent_detached'] = True
        return self.cleanup

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
