"""Coalesced, value-free invalidation for attached native windows.

One out-of-context hook thread serves all sources. It never fetches accessible
objects, injects code, or records UI text. Events are hints, not a complete log.
"""
import threading
import time


class WinEventAPI:
    def __init__(self):
        import ctypes as C
        from ctypes import wintypes as W
        self.C, self.W = C, W
        self.user = C.WinDLL('user32', use_last_error=True)
        self.kernel = C.WinDLL('kernel32', use_last_error=True)
        self.callback_type = C.WINFUNCTYPE(None, W.HANDLE, W.DWORD, W.HWND,
                                         W.LONG, W.LONG, W.DWORD, W.DWORD)
        self.user.SetWinEventHook.argtypes = [W.DWORD, W.DWORD, W.HMODULE,
                                             self.callback_type, W.DWORD, W.DWORD, W.DWORD]
        self.user.SetWinEventHook.restype = W.HANDLE
        self.user.UnhookWinEvent.argtypes = [W.HANDLE]
        self.user.UnhookWinEvent.restype = W.BOOL
        self.user.GetAncestor.argtypes = [W.HWND, W.UINT]
        self.user.GetAncestor.restype = W.HWND
        self.user.GetWindowThreadProcessId.argtypes = [W.HWND, C.POINTER(W.DWORD)]
        self.user.GetWindowThreadProcessId.restype = W.DWORD
        self.user.PeekMessageW.argtypes = [C.POINTER(W.MSG), W.HWND, W.UINT, W.UINT, W.UINT]
        self.user.GetMessageW.argtypes = [C.POINTER(W.MSG), W.HWND, W.UINT, W.UINT]
        self.user.GetMessageW.restype = C.c_int
        self.user.PostThreadMessageW.argtypes = [W.DWORD, W.UINT, W.WPARAM, W.LPARAM]
        self.user.PostThreadMessageW.restype = W.BOOL
        self.kernel.GetCurrentThreadId.restype = W.DWORD
        self.thread_id = None

    def identity(self, window):
        root = self.user.GetAncestor(window, 2) or window
        owner = self.W.DWORD()
        self.user.GetWindowThreadProcessId(root, self.C.byref(owner))
        return int(root), owner.value

    def run(self, emit, ready, stopping):
        self.thread_id = self.kernel.GetCurrentThreadId()
        message = self.W.MSG()
        self.user.PeekMessageW(self.C.byref(message), None, 0, 0, 0)
        # Keep the callback alive until UnhookWinEvent on this same thread.
        callback = self.callback_type(lambda hook, event, hwnd, obj, child, tid, tick:
                                      emit(event, int(hwnd or 0), obj))
        hook = self.user.SetWinEventHook(0x8000, 0x8030, None, callback, 0, 0, 0)
        if not hook:
            raise self.C.WinError(self.C.get_last_error())
        try:
            ready.set()
            while not stopping.is_set():
                result = self.user.GetMessageW(self.C.byref(message), None, 0, 0)
                if result == -1:
                    raise self.C.WinError(self.C.get_last_error())
                if result == 0:
                    break
        finally:
            self.user.UnhookWinEvent(hook)

    def stop(self):
        if self.thread_id:
            self.user.PostThreadMessageW(self.thread_id, 0x12, 0, 0)


class WindowEvents:
    """Bounded per-source revisions; consumers keep independent cursors."""
    def __init__(self, api_factory=WinEventAPI):
        self.api_factory, self.api = api_factory, None
        self.condition = threading.Condition(threading.RLock())
        self.ready, self.stopping = threading.Event(), threading.Event()
        self.sources, self.thread = {}, None
        self.failure, self.closed = None, False

    def _run(self):
        try:
            self.api = self.api_factory()
            self.api.run(self._emit, self.ready, self.stopping)
            if not self.stopping.is_set():
                self.failure = 'event_thread_stopped'
        except Exception as exc:
            self.failure = type(exc).__name__
        finally:
            self.ready.set()
            with self.condition:
                self.condition.notify_all()

    def subscribe(self, source, window, pid):
        with self.condition:
            if self.closed:
                raise RuntimeError('Event monitor is closed')
            if source not in self.sources:
                if len(self.sources) >= 64:
                    return {'watching':False, 'revision':None, 'reason':'event_source_limit'}
                self.sources[source] = {'window':window, 'pid':pid, 'revision':0, 'structure_revision':0}
            if self.thread is None:
                self.thread = threading.Thread(target=self._run, name='gui2api-events', daemon=True)
                self.thread.start()
        if not self.ready.wait(2):
            self.failure = 'event_start_timeout'
        return self.read(source)

    def _emit(self, event, window, object_id):
        # Caret/cursor blinking must not cause continuous whole-tree refreshes.
        if not window or object_id in (-7, -8, -9):
            return
        try:
            root, owner = self.api.identity(window)
            with self.condition:
                changed = False
                for record in self.sources.values():
                    if record['window'] == root and record['pid'] == owner:
                        record['revision'] += 1
                        # Property/selection changes still invalidate searches,
                        # but an exact bound object can be checked independently.
                        # Unknown events conservatively count as structural.
                        if event not in range(0x8005, 0x800F):
                            record['structure_revision'] += 1
                        changed = True
                if changed:
                    self.condition.notify_all()
        except Exception:
            # An unhealthy observer must not silently validate a cached tree.
            with self.condition:
                self.failure = 'event_callback_failed'
                self.condition.notify_all()

    def read(self, source, *, after=None, timeout_ms=0):
        deadline = time.monotonic() + timeout_ms / 1000
        with self.condition:
            while True:
                record = self.sources.get(source)
                watching = bool(record and not self.closed and not self.failure and
                                self.thread and self.thread.is_alive() and self.ready.is_set())
                changed = bool(record and after is not None and record['revision'] != after)
                if not watching or changed or time.monotonic() >= deadline:
                    return {'watching':watching, 'revision':record['revision'] if record else None,
                            'structure_revision':record['structure_revision'] if record else None,
                            'changed':changed, 'provider':'WinEventHook', 'contains_values':False,
                            'coalesced':True, 'complete':False,
                            **({'reason':self.failure or 'not_subscribed'} if not watching else {})}
                self.condition.wait(deadline - time.monotonic())

    def unsubscribe(self, source):
        with self.condition:
            self.sources.pop(source, None)
            self.condition.notify_all()

    def close(self):
        with self.condition:
            self.closed = True
            self.sources.clear()
            self.stopping.set()
            self.condition.notify_all()
        if self.api:
            self.api.stop()
        if self.thread:
            self.thread.join(2)
        return {'closed':not self.thread or not self.thread.is_alive()}
