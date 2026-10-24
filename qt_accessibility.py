"""Version-bound Qt accessibility bootstrap, separate from control discovery.

The Weixin profile is intentionally limited to a verified build and a code-
referenced writable byte. It never tries arbitrary candidates or changes global
Windows screen-reader flags. The layout insight is documented by the upstream
wechatauto-replica accessibility module; this implementation is independent.
"""
from __future__ import annotations

import ctypes
from pathlib import Path
import re
import struct

import psutil

from native_metadata import pe_file
from native_qt import RemoteMemory

PROFILES = {'4.1.15.13': 0x0B135C38}
SOURCE = 'https://github.com/fanyuantaier/wechatauto-replica/blob/main/wechatauto/accessibility.py'


def gate_candidates(path):
    """Locate guarded RIP-relative byte checks in executable PE sections."""
    pattern = re.compile(rb'\x48\x85\xc9\x0f\x84.{4}\x80\x3d(.{4})\x00\x0f\x84', re.S)
    with pe_file(path) as pe:
        if pe.pointer_size != 8:
            raise ValueError('Only the checked x64 layout is supported')
        candidates = []
        for section in pe.sections:
            if not section['characteristics'] & 0x20000000:
                continue
            start, end = section['raw'], section['raw'] + section['size']
            for match in pattern.finditer(pe.data, start, end):
                instruction_rva = section['rva'] + match.start() - start
                # The cmp instruction starts 9 bytes after the null check.
                target = instruction_rva + 16 + struct.unpack('<i', match.group(1))[0]
                data_section = next((s for s in pe.sections
                                     if s['rva'] <= target < s['rva'] + max(s['size'], s['virtual_size'])), None)
                if data_section and data_section['characteristics'] & 0x80000000 and not data_section['characteristics'] & 0x20000000:
                    candidates.append({'rva': target, 'instruction_rva': instruction_rva,
                                       'instruction_bytes': bytes(match.group()).hex(),
                                       'section': data_section['name']})
        return candidates


class QtAccessibilityLease:
    """Enable one checked process byte for this session, then restore it."""
    def __init__(self, pid, expected_created_at, *, expected_exe=None):
        process = psutil.Process(pid)
        if process.create_time() != expected_created_at:
            raise ValueError('Process identity changed')
        if expected_exe is not None and Path(process.exe()).resolve() != Path(expected_exe).resolve():
            raise ValueError('Process executable changed')
        self.pid, self.created_at = pid, expected_created_at
        self.memory = RemoteMemory(pid)
        self.handle = None
        self.changed = False
        self.original = None
        try:
            modules = [m for m in self.memory.modules if m['name'].lower() == 'weixin.dll']
            if len(modules) != 1:
                raise ValueError('Exactly one Weixin.dll module is required')
            self.module = modules[0]
            version = Path(self.module['path']).parent.name
            if version not in PROFILES:
                raise ValueError('No validated accessibility profile for this build')
            matches = [c for c in gate_candidates(self.module['path']) if c['rva'] == PROFILES[version]]
            if len(matches) != 1:
                raise ValueError('Expected gate instruction was not uniquely validated')
            self.candidate = matches[0]
            actual = self.memory.read(self.module['base'] + self.candidate['instruction_rva'],
                                      len(bytes.fromhex(self.candidate['instruction_bytes'])))
            if actual.hex() != self.candidate['instruction_bytes']:
                raise ValueError('Loaded gate instructions differ from the checked image')
            self.address = self.module['base'] + self.candidate['rva']
            self.original = self.memory.read(self.address, 1)
            if self.original not in (b'\0', b'\1'):
                raise ValueError('Gate value is not a boolean')
        except Exception:
            self.close()
            raise

    def status(self):
        return {'pid': self.pid, 'version': Path(self.module['path']).parent.name,
                'gate_rva': hex(self.candidate['rva']),
                'active': self.memory.read(self.address, 1) == b'\1',
                'changed_by_session': self.changed,
                'code_injected': False, 'system_settings_changed': False}

    def _write(self, value):
        if psutil.Process(self.pid).create_time() != self.created_at:
            raise RuntimeError('Process identity changed')
        kernel = self.memory.kernel
        kernel.WriteProcessMemory.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
            ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
        if self.handle is None:
            self.handle = kernel.OpenProcess(0x438, False, self.pid)
            if not self.handle:
                raise ctypes.WinError(ctypes.get_last_error())
        written = ctypes.c_size_t()
        buffer = ctypes.create_string_buffer(value)
        if not kernel.WriteProcessMemory(self.handle, self.address, buffer, 1, ctypes.byref(written)) or written.value != 1:
            raise ctypes.WinError(ctypes.get_last_error())
        if self.memory.read(self.address, 1) != value:
            raise RuntimeError('Accessibility byte read-back failed')

    def enable(self):
        if self.memory.read(self.address, 1) != self.original:
            raise RuntimeError('Gate changed concurrently; bootstrap refused')
        if self.original == b'\0':
            self.changed = True
            self._write(b'\1')
        return self.status()

    def close(self):
        try:
            if self.changed:
                current = self.memory.read(self.address, 1)
                if current == b'\1':
                    self._write(self.original)
                elif current != self.original:
                    raise RuntimeError('Concurrent gate change; original value was not overwritten')
                self.changed = False
        finally:
            if self.handle:
                self.memory.kernel.CloseHandle(self.handle)
                self.handle = None
            self.memory.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
