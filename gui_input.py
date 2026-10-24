"""Trusted keyboard/mouse input, bound to a freshly resolved control object.

Discovery stays in UIA/MSAA/Win32. No screenshots, app selectors, clipboard,
or operation retries. A dispatched input is not proof of a business outcome.
"""
import ctypes
import time

from object_bridge import Fault


class _Keyboard(ctypes.Structure):
    _fields_ = [('vk', ctypes.c_uint16), ('scan', ctypes.c_uint16),
                ('flags', ctypes.c_uint32), ('time', ctypes.c_uint32), ('extra', ctypes.c_size_t)]


class _Mouse(ctypes.Structure):
    _fields_ = [('x', ctypes.c_int32), ('y', ctypes.c_int32), ('data', ctypes.c_uint32),
                ('flags', ctypes.c_uint32), ('time', ctypes.c_uint32), ('extra', ctypes.c_size_t)]


class _Union(ctypes.Union):
    _fields_ = [('keyboard', _Keyboard), ('mouse', _Mouse)]


class _Input(ctypes.Structure):
    _fields_ = [('type', ctypes.c_uint32), ('value', _Union)]


class Win32Input:
    def __init__(self):
        from ctypes import wintypes as wt
        self.user = ctypes.WinDLL('user32', use_last_error=True)
        self.user.SetThreadDpiAwarenessContext.argtypes = [wt.HANDLE]
        self.user.SetThreadDpiAwarenessContext.restype = wt.HANDLE
        signatures = {
            'GetForegroundWindow': ([], wt.HWND),
            'SetForegroundWindow': ([wt.HWND], wt.BOOL),
            'SetWindowPos': ([wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint], wt.BOOL),
            'IsIconic': ([wt.HWND], wt.BOOL),
            'IsWindowVisible': ([wt.HWND], wt.BOOL),
            'ShowWindowAsync': ([wt.HWND, ctypes.c_int], wt.BOOL),
            'GetAsyncKeyState': ([ctypes.c_int], ctypes.c_int16),
            'SendInput': ([ctypes.c_uint, ctypes.POINTER(_Input), ctypes.c_int], ctypes.c_uint),
            'SetPhysicalCursorPos': ([ctypes.c_int, ctypes.c_int], wt.BOOL),
            'WindowFromPhysicalPoint': ([wt.POINT], wt.HWND),
            'GetSystemMetrics': ([ctypes.c_int], ctypes.c_int),
            'GetAncestor': ([wt.HWND, ctypes.c_uint], wt.HWND),
        }
        for name, (arguments, result) in signatures.items():
            function = getattr(self.user, name)
            function.argtypes, function.restype = arguments, result
        self.point = wt.POINT

    def foreground(self): return int(self.user.GetForegroundWindow() or 0)

    def activate(self, window):
        if self.user.IsIconic(window): self.user.ShowWindowAsync(window, 9)
        elif not self.user.IsWindowVisible(window): self.user.ShowWindowAsync(window, 5)
        self.user.SetForegroundWindow(window)
        deadline = time.monotonic() + .5
        while (self.foreground() != window or not self.user.IsWindowVisible(window)) and time.monotonic() < deadline: time.sleep(.02)
        if self.foreground() != window or not self.user.IsWindowVisible(window):
            raise Fault('focus_unverified', 'Windows did not activate the selected window; no input was sent')

    def modifiers_down(self):
        return any(self.user.GetAsyncKeyState(key) & 0x8000 for key in (16, 17, 18, 91, 92))

    def raise_window(self, window):
        # A bound click can activate a visible window when Windows refuses a
        # background SetForegroundWindow call. Do not attach foreign input queues.
        if self.user.IsIconic(window): self.user.ShowWindowAsync(window, 9)
        if not self.user.SetWindowPos(window, None, 0, 0, 0, 0, 1 | 2 | 16 | 64):
            raise Fault('focus_unverified', 'Could not reveal the selected window for its requested click')

    def hit_window(self, x, y):
        hit = self.user.WindowFromPhysicalPoint(self.point(x, y))
        return int(self.user.GetAncestor(hit, 2) or hit or 0)

    def move(self, x, y):
        if not self.user.SetPhysicalCursorPos(x, y): raise Fault('input_unavailable', 'Could not position the pointer')

    def send(self, events):
        from ctypes import wintypes as wt
        previous=self.user.SetThreadDpiAwarenessContext(wt.HANDLE(-4))
        if not previous: raise Fault('input_unavailable','Could not establish physical screen coordinates')
        try: return self._send(events)
        finally: self.user.SetThreadDpiAwarenessContext(previous)

    def _send(self, events):
        packets = (_Input * len(events))()
        for packet, (kind, key, flags) in zip(packets, events):
            packet.type = 1 if kind in ('key', 'unicode') else 0
            if kind == 'key': packet.value.keyboard = _Keyboard(key, 0, flags, 0, 0)
            elif kind == 'unicode': packet.value.keyboard = _Keyboard(0, key, flags | 4, 0, 0)
            elif kind == 'mouse_move':
                x,y=key
                left,top,width,height=(self.user.GetSystemMetrics(i) for i in (76,77,78,79))
                if width<2 or height<2: raise Fault('input_unavailable','No usable virtual desktop bounds')
                packet.value.mouse = _Mouse(round((x-left)*65535/(width-1)),round((y-top)*65535/(height-1)),0,0xC001,0,0)
            else: packet.value.mouse = _Mouse(0, 0, key & 0xffffffff if kind == 'wheel' else 0, flags, 0, 0)
        sent = self.user.SendInput(len(packets), packets, ctypes.sizeof(_Input))
        if sent != len(packets):
            # A partial chord must not leave our modifier keys held down.
            releases = [(kind, key, flags | 2) for kind, key, flags in events[:sent] if kind == 'key' and not flags & 2]
            if releases:
                cleanup = (_Input * len(releases))()
                for packet, (_, key, flags) in zip(cleanup, releases):
                    packet.type = 1
                    packet.value.keyboard = _Keyboard(key, 0, flags, 0, 0)
                self.user.SendInput(len(cleanup), cleanup, ctypes.sizeof(_Input))
            raise Fault('outcome_unknown', 'SendInput accepted only part of the input; inspect actual state before acting again')


KEYS = {'ENTER':13, 'RETURN':13, 'TAB':9, 'ESC':27, 'ESCAPE':27, 'BACKSPACE':8, 'BKSP':8, 'DEL':46, 'DELETE':46, 'SPACE':32,
        'LEFT':37, 'UP':38, 'RIGHT':39, 'DOWN':40, 'HOME':36, 'END':35,
        'PAGEUP':33, 'PAGEDOWN':34, **{f'F{i}':111+i for i in range(1,13)},
        **{chr(i):i for i in range(48,58)}, **{chr(i):i for i in range(65,91)}}
MODIFIERS = {'CTRL':17, 'SHIFT':16, 'ALT':18}


def chord_events(chord):
    if not isinstance(chord, str) or len(chord) > 80:
        raise Fault('invalid_arguments', 'keys must be a chord such as ENTER or CTRL+A')
    names = chord.upper().split('+')
    if not names or names[-1] not in KEYS or len(set(names)) != len(names) or any(n not in MODIFIERS for n in names[:-1]):
        raise Fault('invalid_arguments', 'Unsupported key chord; use CTRL/SHIFT/ALT and one named key')
    modifiers = [MODIFIERS[n] for n in names[:-1]]
    key = KEYS[names[-1]]
    extended = 1 if names[-1] in ('DELETE','DEL','LEFT','RIGHT','UP','DOWN','HOME','END','PAGEUP','PAGEDOWN') else 0
    return [('key', k, 0) for k in modifiers] + [('key', key, extended), ('key', key, extended | 2)] + [('key', k, 2) for k in reversed(modifiers)]


class NativeInput:
    def __init__(self, api=None): self.api = api if api is not None else Win32Input()

    def window_escape(self, window, inspect):
        # Explicit recovery action only. It cannot be generalized into arbitrary
        # window typing/Enter, and does not focus/click an occluded editor.
        for _ in range(2):
            current=inspect()
            if not current.get('ok') or not current.get('data',{}).get('foreground') or self.api.foreground()!=window:
                raise Fault('focus_unverified','Selected window is not foreground; no Escape was sent')
            if self.api.modifiers_down(): raise Fault('input_unavailable','Release held modifier keys before Escape')
        self.api.send(chord_events('ESC'))
        return {'status':'executed_unverified','window':window,'keys':'ESC','trusted_input':True,'automatic_retries':0,
                'verification':'Explicit window Escape, no editor focus or hit-test; observe whether the popup closed'}

    def execute(self, method, params, inspect, focus):
        # Validate the complete request before activation, focus, selection, or input.
        if method == 'type_text':
            text = params.get('text')
            if not isinstance(text, str) or not 1 <= len(text) <= 10000 or any(ord(c) < 32 or 0xD800 <= ord(c) <= 0xDFFF for c in text):
                raise Fault('invalid_arguments', 'type_text requires 1..10000 single-line characters; send Enter separately')
            if not isinstance(params.get('replace', False), bool): raise Fault('invalid_arguments', 'replace must be boolean')
            encoded = text.encode('utf-16-le')
            events = [(kind, int.from_bytes(encoded[i:i+2], 'little'), flag) for i in range(0,len(encoded),2) for kind,flag in [('unicode',0),('unicode',2)]]
        elif method == 'press_keys': events = chord_events(params.get('keys'))
        elif method == 'click_input':
            button = params.get('button', 'left')
            if button not in ('left', 'right'):
                raise Fault('invalid_arguments', 'button must be left or right')
            down, up = (2, 4) if button == 'left' else (8, 16)
            events = [('mouse',0,down),('mouse',0,up)]
        elif method == 'scroll_input':
            direction, steps = params.get('direction'), params.get('steps', 3)
            if direction not in ('up', 'down', 'left', 'right') or not isinstance(steps, int) or isinstance(steps, bool) or not 1 <= steps <= 20:
                raise Fault('invalid_arguments', 'Use direction up/down/left/right and steps 1..20')
            delta = steps * 120 * (1 if direction in ('up', 'right') else -1)
            events = [('wheel',delta,0x800 if direction in ('up','down') else 0x1000)]
        else: raise Fault('unsupported', 'Unknown native input method')
        keyboard = method in ('type_text', 'press_keys')
        target = inspect()
        if target.get('password'): raise Fault('protected_control', 'Protected control')
        if not target.get('enabled') or target.get('offscreen'): raise Fault('target_unavailable', 'Control must be enabled and on screen')
        if method == 'type_text' and not target.get('text_input_candidate'):
            raise Fault('unsupported', 'The resolved control is not an editable keyboard target')
        if keyboard and not target.get('keyboard_focusable'):
            raise Fault('unsupported', 'The resolved control does not advertise keyboard focus')
        if self.api.modifiers_down(): raise Fault('input_busy', 'A user modifier key is held; no input was sent')
        window, pid = target['window'], target['pid']
        try: self.api.activate(window)
        except Fault as exc:
            if method != 'click_input' or exc.code != 'focus_unverified': raise
            self.api.raise_window(window)
        if keyboard:
            focus()
            deadline = time.monotonic() + .4
            while True:
                current = inspect()
                if current.get('keyboard_focused'): break
                if time.monotonic() >= deadline: raise Fault('focus_unverified', 'Control focus was not confirmed; no keys were sent')
                time.sleep(.02)
        dispatched = False
        def guard():
            current = inspect()
            code = 'outcome_unknown' if dispatched else 'focus_unverified'
            if current.get('window') != window or current.get('pid') != pid or current.get('password') or method != 'click_input' and self.api.foreground() != window or not current.get('enabled') or current.get('offscreen'):
                raise Fault(code, 'Target/window changed; input stopped and was not retried')
            if keyboard and not current.get('keyboard_focused'):
                raise Fault(code, 'Control lost focus; input stopped and was not retried')
            return current
        if method in ('click_input', 'scroll_input'):
            current = guard()
            rect = current['rect']
            if rect['width'] <= 0 or rect['height'] <= 0: raise Fault('target_unavailable', 'No live control bounds')
            x, y = round(rect['x']+rect['width']/2), round(rect['y']+rect['height']/2)
            if self.api.hit_window(x,y) != window: raise Fault('target_occluded', 'Another window covers the resolved control; no click was sent')
            self.api.move(x,y)
            guard()
            if self.api.hit_window(x,y) != window: raise Fault('target_occluded', 'Target became covered before the click')
            # Keep the final move and button pair in one ordered SendInput batch.
            events.insert(0,('mouse_move',(x,y),0))
        if method == 'type_text' and params.get('replace'):
            guard(); dispatched = True; self.api.send(chord_events('CTRL+A'))
        for start in range(0, len(events), 32):
            try:
                guard(); dispatched = True; self.api.send(events[start:start+32])
            except Fault as exc:
                if dispatched and exc.code != 'outcome_unknown': raise Fault('outcome_unknown', str(exc)) from exc
                raise
        return {'status':'executed_unverified', 'backend':'Win32.SendInput', 'trusted_input':True,
                **({'click_point':{'x':x,'y':y,'space':'physical_screen'}, 'button':button} if method == 'click_input' else {}),
                **({'scroll_point':{'x':x,'y':y,'space':'physical_screen'}, 'direction':direction, 'steps':steps} if method == 'scroll_input' else {}),
                'window':window, 'method':method, 'verification':'Input dispatched once; observe the control/application result'}
