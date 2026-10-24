"""Observe native Qt 5 object metadata using ReadProcessMemory only.

No injected code, native calls, hooks, heap-wide scans or property getters.
All layouts are validated before use; unsupported/dynamic metadata is skipped.
"""
import bisect
import ctypes
import hashlib
from pathlib import Path
import re
import struct
import time

from native_metadata import pe_file


class RemoteMemory:
    def __init__(self, pid, max_bytes=64 * 1024 * 1024):
        self.pid = pid
        self.bytes_read = self.reads = 0
        self.max_bytes = max_bytes
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        self.psapi = ctypes.WinDLL('psapi', use_last_error=True)
        self.kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_bool, ctypes.c_uint32]
        self.kernel.OpenProcess.restype = ctypes.c_void_p
        self.kernel.ReadProcessMemory.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
        self.kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        self.handle = self.kernel.OpenProcess(0x400 | 0x10, False, pid)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            self.modules = self._modules()
        except Exception:
            self.close()
            raise
        self.bases = [m['base'] for m in self.modules]
        self.pages = {}

    def _modules(self):
        class MODULEINFO(ctypes.Structure):
            _fields_ = [('base', ctypes.c_void_p), ('size', ctypes.c_uint32), ('entry', ctypes.c_void_p)]
        self.psapi.EnumProcessModulesEx.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32), ctypes.c_uint32]
        self.psapi.GetModuleInformation.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(MODULEINFO), ctypes.c_uint32]
        self.psapi.GetModuleFileNameExW.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint32]
        handles = (ctypes.c_void_p * 2048)()
        required = ctypes.c_uint32()
        if not self.psapi.EnumProcessModulesEx(self.handle, handles, ctypes.sizeof(handles), ctypes.byref(required), 3):
            raise ctypes.WinError(ctypes.get_last_error())
        rows = []
        for handle in list(handles)[:min(required.value // ctypes.sizeof(ctypes.c_void_p), 2048)]:
            info = MODULEINFO()
            name = ctypes.create_unicode_buffer(32768)
            if self.psapi.GetModuleInformation(self.handle, handle, ctypes.byref(info), ctypes.sizeof(info)) and self.psapi.GetModuleFileNameExW(self.handle, handle, name, len(name)):
                rows.append({'base': info.base, 'size': info.size, 'path': name.value, 'name': Path(name.value).name})
        return sorted(rows, key=lambda row: row['base'])

    def module(self, address):
        i = bisect.bisect_right(self.bases, address) - 1
        if i >= 0 and address < self.modules[i]['base'] + self.modules[i]['size']:
            return self.modules[i]
        return None

    def read(self, address, size, cache=False):
        if not 0x10000 <= address < 0x800000000000 or not 0 <= size <= 16 * 1024 * 1024:
            raise ValueError('Invalid remote read bounds')
        if cache and size <= 256 and address // 4096 == (address + size - 1) // 4096:
            page = address & ~4095
            if page not in self.pages:
                self.pages[page] = self.read(page, 4096)
            return self.pages[page][address - page:address - page + size]
        if self.bytes_read + size > self.max_bytes:
            raise ValueError('Remote read byte budget exceeded')
        buffer = ctypes.create_string_buffer(size)
        actual = ctypes.c_size_t()
        self.reads += 1
        if not self.kernel.ReadProcessMemory(self.handle, address, buffer, size, ctypes.byref(actual)) or actual.value != size:
            raise OSError('Remote memory no longer readable')
        self.bytes_read += size
        return buffer.raw

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


class Qt5Inspector:
    def __init__(self, memory, pointer_size, max_objects=256, max_seconds=8):
        from capstone import Cs, CS_ARCH_X86, CS_MODE_32, CS_MODE_64
        self.memory, self.width = memory, pointer_size
        self.disassembler = Cs(CS_ARCH_X86, CS_MODE_64 if pointer_size == 8 else CS_MODE_32)
        self.disassembler.detail = True
        self.max_objects, self.deadline = max_objects, time.monotonic() + max_seconds
        self.meta_cache, self.function_cache, self.objects = {}, {}, {}
        self.meta_export_cache = {}
        self.stopped = []
        self.classes = {}

    def ptr(self, address, cache=False):
        return struct.unpack('<Q' if self.width == 8 else '<I', self.memory.read(address, self.width, cache))[0]

    def uint(self, address):
        return struct.unpack('<I', self.memory.read(address, 4, True))[0]

    def text(self, strings, index):
        if not 0 <= index <= 8192:
            raise ValueError('String index outside metadata budget')
        entry = strings + index * (24 if self.width == 8 else 16)
        size = self.uint(entry + 4)
        if not 0 <= size <= 256:
            raise ValueError('Metadata string too long')
        off = struct.unpack('<q' if self.width == 8 else '<i', self.memory.read(entry + (16 if self.width == 8 else 12), self.width, True))[0]
        address = entry + off
        if not self.memory.module(address):
            raise ValueError('String data is not static module metadata')
        return self.memory.read(address, size, True).decode('utf-8', 'strict')

    def meta(self, address):
        if address in self.meta_cache:
            return self.meta_cache[address]
        self.meta_cache[address] = None
        try:
            if not self.memory.module(address):
                return None
            parent, strings, data = [self.ptr(address + self.width * i, True) for i in range(3)]
            if not self.memory.module(strings) or not self.memory.module(data):
                return None
            header = struct.unpack('<14I', self.memory.read(data, 56, True))
            if header[0] not in (6, 7, 8) or header[4] > 2048 or header[6] > 2048 or header[2] > 1024:
                return None
            name = self.text(strings, header[1])
            if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_:<> ,*&]{0,180}', name):
                return None
            value = {'address': address, 'parent': parent, 'strings': strings, 'data': data, 'header': header, 'name': name}
            self.meta_cache[address] = value
            return value
        except (OSError, ValueError, UnicodeError, struct.error):
            return None

    def meta_function(self, address):
        if address in self.function_cache:
            return self.function_cache[address]
        self.function_cache[address] = None
        from capstone.x86_const import X86_OP_IMM, X86_OP_MEM, X86_REG_RIP
        try:
            current = address
            for jump in range(3):
                code = self.memory.read(current, 128, True)
                instructions = list(self.disassembler.disasm(code, current, count=40))
                follow = None
                padding = 0
                for i, ins in enumerate(instructions):
                    padding = padding + 1 if ins.mnemonic == 'int3' else 0
                    if padding >= 3:
                        break
                    if ins.mnemonic == 'jmp' and i < 3 and ins.operands[0].type == X86_OP_IMM:
                        follow = ins.operands[0].imm
                        break
                    candidate = None
                    if ins.mnemonic in ('mov', 'movabs') and len(ins.operands) == 2 and ins.operands[1].type == X86_OP_IMM:
                        candidate = ins.operands[1].imm & ((1 << (8 * self.width)) - 1)
                    elif ins.mnemonic == 'lea' and len(ins.operands) == 2 and ins.operands[1].type == X86_OP_MEM:
                        mem = ins.operands[1].mem
                        if mem.base == X86_REG_RIP:
                            candidate = ins.address + ins.size + mem.disp
                        elif self.width == 4 and not mem.base and not mem.index:
                            candidate = mem.disp & 0xffffffff
                    if candidate and self.memory.module(candidate):
                        meta = self.meta(candidate)
                        if meta:
                            self.function_cache[address] = meta
                            return meta
                    if ins.mnemonic in ('call', 'jmp') and ins.operands and ins.operands[0].type == X86_OP_MEM:
                        mem = ins.operands[0].mem
                        slot = ins.address + ins.size + mem.disp if mem.base == X86_REG_RIP else mem.disp & 0xffffffff if not mem.base and not mem.index else None
                        if slot:
                            imported = self.ptr(slot, True)
                            module = self.memory.module(imported)
                            if module and 'qt5' in module['name'].lower():
                                if module['base'] not in self.meta_export_cache:
                                    with pe_file(module['path']) as pe:
                                        self.meta_export_cache[module['base']] = {module['base'] + e['rva'] for e in pe.exports() if '?metaObject@' in e['name']}
                                if imported in self.meta_export_cache[module['base']]:
                                    meta = self.meta_function(imported)
                                    if meta:
                                        self.function_cache[address] = meta
                                        return meta
                if follow is None or not self.memory.module(follow):
                    break
                current = follow
        except (OSError, ValueError):
            pass
        return None

    def qobject(self, address):
        try:
            if address < 0x10000 or address >= (0x100000000 if self.width == 4 else 0x800000000000) or address % self.width:
                return None
            vtable, private = struct.unpack('<QQ' if self.width == 8 else '<II', self.memory.read(address, self.width * 2))
            if not self.memory.module(vtable) or private < 0x10000 or private % self.width:
                return None
            # QObjectData has its own virtual destructor, followed by q_ptr.
            if self.ptr(private + self.width) != address:
                return None
            # Qt's Q_OBJECT virtual declarations precede QObject's destructor.
            # In the MSVC ABI used here, metaObject() is the first vtable slot.
            function = self.ptr(vtable, True)
            if not self.memory.module(function):
                return None
            meta = self.meta_function(function)
            if not meta:
                return None
            ancestor, visited = meta, set()
            while ancestor and ancestor['address'] not in visited and len(visited) < 24:
                visited.add(ancestor['address'])
                if ancestor['name'].split('::')[-1] == 'QObject':
                    return {'address': address, 'private': private, 'meta': meta}
                ancestor = self.meta(ancestor['parent']) if ancestor['parent'] else None
        except (OSError, ValueError, struct.error):
            pass
        return None

    def description(self, meta):
        if meta['address'] in self.classes:
            return self.classes[meta['address']]
        h, data, strings = meta['header'], meta['data'], meta['strings']
        properties, methods, events = [], [], []
        def typename(typeid):
            return self.text(strings, typeid & 0x7fffffff) if typeid & 0x80000000 else f'QMetaType:{typeid}'
        for i in range(min(h[4], 256)):
            name, argc, params, tag, flags = struct.unpack('<5I', self.memory.read(data + (h[5] + i * 5) * 4, 20, True))
            if argc > 32:
                continue
            types = [typename(self.uint(data + (params + j) * 4)) for j in range(argc + 1)]
            method = {'name': self.text(strings, name), 'parameters': types[1:], 'returns': types[0],
                      'access': ['private', 'protected', 'public', 'unknown'][flags & 3],
                      'effect': 'unknown', 'callable': False, 'source': 'native_qt_static_metadata'}
            method['signature'] = method['name'] + '(' + ','.join(types[1:]) + ')'
            (events if flags & 12 == 4 else methods).append(method)
        for i in range(min(h[6], 256)):
            name, typeid, flags = struct.unpack('<3I', self.memory.read(data + (h[7] + i * 3) * 4, 12, True))
            properties.append({'name': self.text(strings, name), 'type': typename(typeid),
                               'native_readable': bool(flags & 1), 'native_writable': bool(flags & 2),
                               'getter_executed': False})
        description = {'type': meta['name'], 'properties': properties, 'methods': methods, 'events': events,
                       'metadata_revision': h[0], 'metadata_only': True}
        self.classes[meta['address']] = description
        ancestor = self.meta(meta['parent']) if meta['parent'] else None
        description['base_type'] = ancestor['name'] if ancestor else None
        if ancestor:
            self.description(ancestor)
        return description

    def opaque(self, address):
        return 'qt_' + hashlib.sha256(f'{self.memory.pid}:{address}'.encode()).hexdigest()[:20]

    def walk(self, roots):
        pending = [(address, None) for address in roots]
        while pending and len(self.objects) < self.max_objects:
            if time.monotonic() >= self.deadline:
                self.stopped.append('time_budget')
                break
            address, parent = pending.pop(0)
            if address in self.objects:
                continue
            obj = self.qobject(address)
            if not obj:
                continue
            try:
                self.description(obj['meta'])
                record = {'id': self.opaque(address), 'type': obj['meta']['name'], 'parent': self.opaque(parent) if parent else None,
                          'metadata_class': obj['meta']['name'], 'children': []}
                self.objects[address] = record
                children = self.ptr(obj['private'] + self.width * 3)
                alloc, begin, end = struct.unpack('<iii', self.memory.read(children + 4, 12))
                if not 0 <= begin <= end <= alloc <= 16384:
                    continue
                for i in range(begin, min(end, begin + self.max_objects)):
                    child = self.ptr(children + 16 + i * self.width)
                    child_object = self.qobject(child)
                    if child_object and self.ptr(child_object['private'] + 2 * self.width) == address:
                        record['children'].append(self.opaque(child))
                        pending.append((child, address))
            except (OSError, ValueError, UnicodeError, struct.error):
                continue
        if pending and len(self.objects) >= self.max_objects:
            self.stopped.append('object_budget')

    def scan(self, selected_modules, global_scan_bytes=12 * 1024 * 1024):
        roots, sources = [], []
        for module in selected_modules:
            try:
                with pe_file(module['path']) as pe:
                    for export in pe.exports():
                        if '?self@QCoreApplication' in export['name']:
                            candidate = self.ptr(module['base'] + export['rva'])
                            if self.qobject(candidate):
                                roots.append(candidate)
                                sources.append({'module': module['name'], 'kind': 'exported_application_instance'})
                    if roots:
                        continue
                    # A static-linked Qt build may hide qApp. Inspect only image
                    # .data pointers, never arbitrary heap contents or strings.
                    for section in pe.sections:
                        if section['name'] != '.data':
                            continue
                        size = min(section['virtual_size'], global_scan_bytes)
                        data = self.memory.read(module['base'] + section['rva'], size)
                        seen = set()
                        for offset in range(0, len(data) - self.width + 1, self.width):
                            if time.monotonic() >= self.deadline:
                                self.stopped.append('global_scan_time_budget')
                                break
                            candidate = struct.unpack_from('<Q' if self.width == 8 else '<I', data, offset)[0]
                            if candidate in seen or candidate < 0x10000 or candidate % self.width:
                                continue
                            seen.add(candidate)
                            if self.qobject(candidate):
                                roots.append(candidate)
                                sources.append({'module': module['name'], 'kind': 'validated_global_qobject_pointer', 'image_offset': section['rva'] + offset})
                                if len(roots) >= 32:
                                    break
            except (OSError, ValueError):
                continue
        # Traversal gets its own bounded period after candidate scanning.
        self.deadline = max(self.deadline, time.monotonic() + 3)
        self.walk(roots)
        return {'ok': bool(self.objects), 'pid': self.memory.pid, 'pointer_size': self.width,
                'source': 'ReadProcessMemory; validated Qt5 QObject and static QMetaObject layouts',
                'root_sources': sources, 'roots': [self.opaque(p) for p in dict.fromkeys(roots)],
                'objects': list(self.objects.values()), 'classes': list(self.classes.values()),
                'statistics': {'objects': len(self.objects), 'classes': len(self.classes),
                               'remote_bytes': self.memory.bytes_read, 'remote_reads': self.memory.reads},
                'stopped_by': sorted(set(self.stopped)), 'native_calls': 0, 'injected_code': False,
                'property_values_read': 0, 'business_meanings_verified': False}


def inspect_qt(pid, module_names=None, max_objects=256, max_seconds=8):
    memory = RemoteMemory(pid)
    try:
        modules = [m for m in memory.modules if m['name'].lower() in {n.lower() for n in module_names}] if module_names else [m for m in memory.modules if 'qt5core' in m['name'].lower()]
        if not modules:
            return {'ok': False, 'pid': pid, 'reason': 'No selected native Qt module', 'injected_code': False}
        with pe_file(modules[0]['path']) as pe:
            width = pe.pointer_size
        return Qt5Inspector(memory, width, max_objects, max_seconds).scan(modules)
    finally:
        memory.close()
