"""Bounded MSVC x64 RTTI observation; never execute discovered functions."""
import hashlib
import re
import struct
import time

from native_metadata import pe_file
from native_qt import RemoteMemory


class CppInspector:
    def __init__(self, memory):
        self.memory = memory
        self.vtables = {}
        self.sections = {}

    def ptr(self, address):
        return struct.unpack('<Q', self.memory.read(address, 8))[0]

    def executable(self, address):
        module = self.memory.module(address)
        if not module:
            return False
        if module['base'] not in self.sections:
            try:
                with pe_file(module['path']) as pe:
                    self.sections[module['base']] = pe.sections
            except (OSError, ValueError):
                self.sections[module['base']] = []
        rva = address - module['base']
        return any(s['characteristics'] & 0x20000000 and s['rva'] <= rva < s['rva'] + s['virtual_size'] for s in self.sections[module['base']])

    def rtti(self, vtable):
        if vtable in self.vtables:
            return self.vtables[vtable]
        self.vtables[vtable] = None
        try:
            module = self.memory.module(vtable)
            if not module:
                return None
            locator = self.ptr(vtable - 8)
            if self.memory.module(locator) != module:
                return None
            signature, offset, cd_offset, type_rva, hierarchy_rva, self_rva = struct.unpack('<6I', self.memory.read(locator, 24, True))
            if signature != 1 or locator - self_rva != module['base'] or offset > 65536 or cd_offset > 65536:
                return None
            if type_rva >= module['size'] or hierarchy_rva >= module['size']:
                return None
            name_address = module['base'] + type_rva + 16
            raw = self.memory.read(name_address, 256, True).split(b'\0', 1)[0]
            if not re.fullmatch(rb'\.\?A[VU][ -~]{2,240}', raw):
                return None
            name = raw.decode('ascii')
            simple = re.fullmatch(r'\.\?A[VU]([A-Za-z_][A-Za-z0-9_]*(?:@[A-Za-z_][A-Za-z0-9_]*)*)@@', name)
            typename = '::'.join(reversed(simple[1].split('@'))) if simple else name
            slots = []
            for index in range(32):
                function = self.ptr(vtable + index * 8)
                if not self.executable(function):
                    break
                code_module = self.memory.module(function)
                slots.append({'slot': index, 'module': code_module['name'], 'rva': function - code_module['base'], 'signature': 'unknown', 'callable': False})
            if not slots:
                return None
            result = {'type': typename, 'mangled_type': name, 'module': module['name'], 'subobject_offset': offset,
                      'virtual_slots': slots, 'slot_limit_reached': len(slots) == 32, 'rtti_validated': True,
                      'method_names_available': False, 'field_layout_available': False}
            self.vtables[vtable] = result
            return result
        except (OSError, ValueError, struct.error):
            return None

    def object(self, address):
        if not 0x10000 <= address < 0x800000000000 or address % 8:
            return None
        try:
            return self.rtti(self.ptr(address))
        except (OSError, ValueError, struct.error):
            return None

    def scan(self, modules, max_seconds=8, max_objects=128):
        deadline = time.monotonic() + max_seconds
        objects, classes, seen, stopped = [], {}, set(), []
        for module in modules:
            with pe_file(module['path']) as pe:
                if pe.pointer_size != 8:
                    continue
                sections = pe.sections
            for section in sections:
                if section['name'] != '.data':
                    continue
                size = min(section['virtual_size'], 12 * 1024 * 1024)
                try:
                    data = self.memory.read(module['base'] + section['rva'], size)
                except (OSError, ValueError):
                    continue
                for offset in range(0, len(data) - 7, 8):
                    if time.monotonic() >= deadline or len(objects) >= max_objects:
                        stopped.append('time_budget' if len(objects) < max_objects else 'object_budget')
                        break
                    candidate = struct.unpack_from('<Q', data, offset)[0]
                    if candidate in seen or not 0x10000 <= candidate < 0x800000000000 or candidate % 8:
                        continue
                    seen.add(candidate)
                    description = self.object(candidate)
                    if description:
                        identity = hashlib.sha256(f'{self.memory.pid}:{candidate}'.encode()).hexdigest()[:20]
                        classes[description['mangled_type']] = description
                        objects.append({'id': 'cpp_' + identity, 'type': description['type'], 'class': description['mangled_type'],
                                        'source_module': module['name'], 'source_image_offset': section['rva'] + offset,
                                        'source': 'image global points to RTTI-validated live polymorphic object'})
                if stopped:
                    break
            if stopped:
                break
        return {'ok': bool(objects), 'pid': self.memory.pid, 'objects': objects, 'classes': list(classes.values()),
                'statistics': {'objects': len(objects), 'classes': len(classes), 'remote_bytes': self.memory.bytes_read, 'remote_reads': self.memory.reads},
                'stopped_by': sorted(set(stopped)), 'injected_code': False, 'native_calls': 0,
                'application_values_saved': False, 'business_meanings_verified': False,
                'scope': 'MSVC x64 RTTI; selected image global pointers only; no heap scan or field reads'}


def inspect_cpp(pid, module_names, max_seconds=8):
    memory = RemoteMemory(pid)
    try:
        wanted = {n.lower() for n in module_names}
        modules = [m for m in memory.modules if m['name'].lower() in wanted]
        return CppInspector(memory).scan(modules, max_seconds=max_seconds)
    finally:
        memory.close()
