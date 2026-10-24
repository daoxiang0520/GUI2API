"""Application profiles over reusable runtime bootstraps and field adapters.

No screenshots, input simulation, account initialization or message sending.
Objects re-resolve semantic fields before each call rather than caching tree IDs.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import threading
import time
import uuid

import psutil

from desktop import DesktopSession
from object_bridge import Fault, exposed

ROOT = Path(__file__).resolve().parent


class ProcessLease:
    """One active mutating bridge per process, including across CLI/MCP clients."""
    def __init__(self, pid, created_at, directory=None):
        import msvcrt
        directory = Path(directory or ROOT / 'native/runtime')
        directory.mkdir(parents=True, exist_ok=True)
        key = hashlib.sha256(f'{pid}:{created_at}'.encode()).hexdigest()[:20]
        self._stream = (directory / ('application-' + key + '.lock')).open('a+b')
        self._locked = False
        try:
            self._stream.seek(0, 2)
            if self._stream.tell() == 0:
                self._stream.write(b'\0')
                self._stream.flush()
            self._stream.seek(0)
            msvcrt.locking(self._stream.fileno(), msvcrt.LK_NBLCK, 1)
            self._locked = True
        except OSError:
            self._stream.close()
            raise Fault('application_busy', 'Another AppCtl session owns this process; close it before connecting again')

    def close(self):
        import msvcrt
        if not self._stream.closed:
            try:
                if self._locked:
                    self._stream.seek(0)
                    msvcrt.locking(self._stream.fileno(), msvcrt.LK_UNLCK, 1)
            finally:
                self._stream.close()
                self._locked = False


def checked(result):
    if not result.get('ok'):
        fault = result.get('error', {})
        raise Fault(fault.get('code', 'runtime_error'), fault.get('message', 'Runtime request failed'))
    return result['data']


def validate_text(text):
    if not isinstance(text, str) or len(text) > 4096 or any(c in text for c in ('\0', '\r', '\n')):
        raise Fault('invalid_arguments', 'Only bounded single-line text is currently verified')


class UiaFields:
    def __init__(self, desktop, target, profile):
        from qt_accessibility import QtAccessibilityLease
        self.desktop, self.target, self.profile = desktop, target, profile
        self.lease = None
        self.restored_window = False
        try:
            if profile.get('bootstrap') == 'qt-accessibility-byte':
                self.lease = QtAccessibilityLease(target['pid'], target['created_at'], expected_exe=target['exe'])
                self.lease.enable()
            if profile.get('restore_window') and target['minimized']:
                checked(desktop.request('window', {'window': target['window'], 'pid': target['pid'], 'state': 'restore'}))
                self.restored_window = True
        except Exception:
            self.close()
            raise

    def _observe(self):
        return checked(self.desktop.request('inspect', {'window': self.target['window'],
            'backend': 'uia3', 'depth': 16, 'limit': 1500, 'time_budget_ms': 3500,
            'actionable': False, 'probe_timeout': 8}))['elements']

    def _field(self, name, rows=None):
        if name not in self.profile['slots']:
            raise Fault('unknown_field', 'This application profile has no such field')
        spec = self.profile['slots'][name]
        matches = []
        for row in rows if rows is not None else self._observe():
            if row.get('role') not in spec['roles'] or row.get('password') or not row.get('enabled'):
                continue
            # Either a strong automation ID, or the declared class + label.
            by_id = bool(row.get('automation_id')) and row['automation_id'] in spec.get('automation_ids', [])
            by_class = row.get('class_name') in spec.get('classes', [])
            by_name = 'names' not in spec or row.get('name') in spec['names']
            if (by_id or by_class and by_name) and 'get' in row.get('actions', []):
                matches.append(row)
        if len(matches) != 1:
            raise Fault('ambiguous_field' if matches else 'field_unavailable', 'The profile must resolve to exactly one field')
        return matches[0]

    def status(self):
        rows, fields = self._observe(), {}
        for name in self.profile['slots']:
            try:
                row = self._field(name, rows)
                fields[name] = {'available': True, 'source': 'UIA3', 'set_advertised': 'set' in row['actions']}
            except Fault as exc:
                fields[name] = {'available': False, 'reason': exc.code}
        return {'fields': fields, 'backend': 'uia3+qt-bootstrap', 'send_supported': False}

    def _read(self, field):
        data = checked(self.desktop.request('get', {'ref': field['ref'], 'max_length': 65536}))
        if data.get('label_only') or data.get('truncated') or not isinstance(data.get('value'), str):
            raise Fault('text_read_unavailable', 'A complete field value is required; a control label is insufficient')
        return data

    def read(self, name):
        return self._read(self._field(name))

    def replace(self, name, text, expected):
        field = self._field(name)
        before = self._read(field)
        if before['value'] != expected:
            raise Fault('value_conflict', 'The current field differs from the expected value')
        if self.profile['slots'][name].get('focus_before_replace'):
            activated = checked(self.desktop.request('window', {'window': self.target['window'],
                'pid': self.target['pid'], 'state': 'activate'}))
            if activated.get('status') != 'verified':
                raise Fault('window_activation_unverified', 'No text was written because the application did not become foreground')
            focused = checked(self.desktop.request('focus', {'ref': field['ref']}))
            if focused.get('status') != 'verified':
                raise Fault('focus_unverified', 'No text was written because field focus was not verified')
            if self._read(field)['value'] != expected:
                raise Fault('value_conflict', 'The field changed while acquiring focus')
        if 'set' not in field['actions']:
            raise Fault('unsupported', 'The current field does not advertise an equivalent setter')
        result = checked(self.desktop.request('set', {'ref': field['ref'], 'value': text}))
        if result.get('status') != 'verified':
            raise Fault('outcome_unknown', 'The setter was attempted once; its result was not verified')
        after = self.read(name)
        if after['value'] != text:
            raise Fault('postcondition_failed', 'Fresh field read-back did not match; no retry was attempted')
        return {'value': after['value'], 'source': after['source'], 'status': 'field_value_verified',
                'message_sent': False, 'business_completion_proven': False}

    def open_result(self, name):
        spec = self.profile.get('navigation', {}).get('search_result')
        if not spec:
            raise Fault('unsupported', 'Search-result navigation is not configured')
        if self.read('search')['value'] != name:
            raise Fault('value_conflict', 'Populate this exact search query before selecting a result')
        rows = self._observe()
        by_ref = {r['ref']: r for r in rows}
        def in_scope(row):
            parent = row.get('parent')
            for _ in range(12):
                ancestor = by_ref.get(parent)
                if not ancestor: return False
                if ancestor.get('automation_id') == spec['ancestor_id']: return True
                parent = ancestor.get('parent')
            return False
        matches = [r for r in rows if r.get('name', '').strip() == name and
                   r.get('role') in spec['roles'] and r.get('class_name') in spec['classes'] and in_scope(r)]
        if len(matches) != 1:
            raise Fault('ambiguous_result' if matches else 'result_unavailable', 'Exactly one scoped exact result is required')
        row, action = matches[0], spec['action']
        if action not in row['actions']:
            raise Fault('unsupported', 'The result does not advertise the configured action')
        checked(self.desktop.request(action, {'ref': row['ref']}))
        deadline = time.monotonic() + 2
        while True:
            try:
                field = self._field('draft')
                if field.get('name', '').strip() == name:
                    return {'status': 'draft_context_verified', 'message_sent': False}
            except Fault:
                pass
            if time.monotonic() >= deadline:
                raise Fault('outcome_unknown', 'Result was activated once; the draft context was not confirmed')
            time.sleep(.1)

    def close(self):
        try:
            if self.lease:
                self.lease.close()
                self.lease = None
        finally:
            if self.restored_window:
                checked(self.desktop.request('window', {'window': self.target['window'], 'pid': self.target['pid'], 'state': 'minimize'}))
                self.restored_window = False


class ElectronFields:
    def __init__(self, desktop, target, profile):
        from napi_runtime import NodeApiRuntime
        self.desktop, self.target, self.profile = desktop, target, profile
        self.bridge = NodeApiRuntime(target['pid'], expected_exe=target['exe'],
            expected_created_at=target['created_at'], thread_id=target['tid'], api_module=profile.get('api_module'))
        self.key = '__appctl_fields_' + uuid.uuid4().hex
        self.restored_window = False
        try:
            # Prime existing application activity while the first request waits
            # for a real Node callback. Subsequent calls use our bounded pump.
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(self.bridge.start_pump, 8)
                time.sleep(.05)
                if target['minimized']:
                    checked(desktop.request('window', {'window': target['window'], 'pid': target['pid'], 'state': 'restore'}))
                    self.restored_window = True
                checked(desktop.request('inspect', {'window': target['window'], 'backend': 'auto',
                    'actionable': False, 'depth': 12, 'limit': 500, 'time_budget_ms': 2500, 'probe_timeout': 8}))
                future.result(timeout=10)
            factory = (ROOT / 'bridges/electron_fields.js').read_text(encoding='utf-8')
            self.bridge.json('(()=>{globalThis[' + json.dumps(self.key) + ']=(' + factory + ')('
                + json.dumps(str(target['window'])) + ',' + json.dumps(profile, ensure_ascii=True) + ');return true;})()')
        except Exception:
            self.close()
            raise

    def request(self, op, **args):
        expression = 'globalThis[' + json.dumps(self.key) + '].dispatch(' + json.dumps({'op': op, **args}, ensure_ascii=True) + ')'
        try:
            return self.bridge.await_json(expression)
        except Exception as exc:
            raise Fault('outcome_unknown' if isinstance(exc, TimeoutError) else 'runtime_error', str(exc)) from exc

    def status(self):
        return self.request('status')

    def read(self, name):
        return self.request('read', slot=name)

    def replace(self, name, text, expected):
        return self.request('replace', slot=name, text=text, expected=expected)

    def close(self):
        try:
            if self.bridge:
                try:
                    self.bridge.json('(()=>{delete globalThis[' + json.dumps(self.key) + '];return true;})()', .75)
                except Exception:
                    pass
                self.bridge.close()
                self.bridge = None
        finally:
            if self.restored_window:
                checked(self.desktop.request('window', {'window': self.target['window'], 'pid': self.target['pid'], 'state': 'minimize'}))
                self.restored_window = False


class TextField:
    def __init__(self, session, name):
        self._session, self._name = session, name

    @property
    def text(self) -> str:
        return self.read()['value']

    @exposed(read_only=True)
    def read(self) -> dict:
        return self._session._request('read', self._name)

    @exposed()
    def replace(self, text: str, expected: str) -> dict:
        if self._session._profile['slots'][self._name].get('write_enabled') is False:
            raise Fault('unverified_operation', 'This field write is disabled until an application-level test passes')
        validate_text(text)
        if not isinstance(expected, str):
            raise Fault('invalid_arguments', 'An explicit expected string is required')
        return self._session._request('replace', self._name, text, expected)


class MessengerSession:
    def __init__(self, app, profiles=None, *, desktop_factory=DesktopSession):
        path = Path(profiles or ROOT / 'messengers.profiles.json')
        config = json.loads(path.read_text(encoding='utf-8'))
        if config.get('format') != 'appctl.application-profiles/1' or app not in config.get('apps', {}):
            raise ValueError('Unknown application profile')
        self._profile, self._app = config['apps'][app], app
        source_files = ['messenger_api.py', 'bridges/messenger_agent.py']
        source_files += (['qt_accessibility.py', 'native/Uia3.cs'] if self._profile['backend'] == 'uia3'
                         else ['napi_runtime.py', 'bridges/napi_runtime.js', 'bridges/napi_pump.c', 'bridges/electron_fields.js'])
        self._profile_hash = hashlib.sha256(json.dumps({
            'profile': self._profile,
            'engine': {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in source_files}
        }, sort_keys=True, ensure_ascii=True).encode()).hexdigest()
        self._desktop = desktop_factory()
        self._runtime = None
        self._process_lease = None
        self._lock = threading.RLock()
        self._closed = False
        try:
            windows = checked(self._desktop.request('apps'))['windows']
            matches = [w for w in windows if w['app'].lower() in {n.lower() for n in self._profile['process_names']}
                       and w.get('title') in self._profile['window_titles']]
            if len(matches) != 1:
                raise Fault('ambiguous_application', 'Exactly one current application window is required')
            self._target = dict(matches[0])
            process = psutil.Process(self._target['pid'])
            self._target.update(exe=process.exe(), created_at=process.create_time())
            self._process_lease = ProcessLease(self._target['pid'], self._target['created_at'])
            state = checked(self._desktop.request('window', {'window': self._target['window'], 'pid': self._target['pid']}))
            self._target['minimized'] = state['minimized']
            import win32process
            self._target['tid'] = win32process.GetWindowThreadProcessId(self._target['window'])[0]
            backend = self._profile['backend']
            runtime_type = {'uia3': UiaFields, 'electron-node-api': ElectronFields}.get(backend)
            if runtime_type is None:
                raise Fault('unsupported_runtime', 'The profile names an unsupported runtime')
            self._runtime = runtime_type(self._desktop, self._target, self._profile)
            self.search = TextField(self, 'search')
            self.draft = TextField(self, 'draft')
        except Exception:
            self.close()
            raise

    def _request(self, op, *args):
        with self._lock:
            if self._closed:
                raise Fault('session_closed', 'Reconnect before using application objects')
            if psutil.Process(self._target['pid']).create_time() != self._target['created_at']:
                raise Fault('stale_process', 'The application process was replaced')
            return getattr(self._runtime, op)(*args)

    @exposed(read_only=True)
    def status(self) -> dict:
        return {'app': self._app, 'pid': self._target['pid'], **self._request('status')}

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            if self._runtime:
                self._runtime.close()
        finally:
            try:
                self._desktop.close()
            finally:
                if self._process_lease:
                    self._process_lease.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
