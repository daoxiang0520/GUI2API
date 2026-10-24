"""Read PE export metadata and ASAR code archives without loading their code."""
from contextlib import contextmanager
import json
import mmap
from pathlib import Path
import struct


class PE:
    def __init__(self, data):
        self.data = data
        if data[:2] != b'MZ':
            raise ValueError('Not a PE image')
        pe = self.u32(0x3c)
        if data[pe:pe + 4] != b'PE\0\0':
            raise ValueError('Invalid PE signature')
        self.machine, count = self.unpack('<HH', pe + 4)
        optional_size = self.u16(pe + 20)
        optional = pe + 24
        magic = self.u16(optional)
        if magic not in (0x10b, 0x20b):
            raise ValueError('Unsupported PE optional header')
        self.pointer_size = 8 if magic == 0x20b else 4
        self.image_base = self.unpack('<Q' if self.pointer_size == 8 else '<I', optional + (24 if self.pointer_size == 8 else 28))[0]
        self.header_size = self.u32(optional + 60)
        self.directory = optional + (112 if self.pointer_size == 8 else 96)
        self.sections = []
        for i in range(min(count, 128)):
            off = optional + optional_size + 40 * i
            name = data[off:off + 8].split(b'\0')[0].decode('ascii', 'replace')
            virtual_size, rva, size, raw = self.unpack('<IIII', off + 8)
            self.sections.append({'name': name, 'rva': rva, 'virtual_size': virtual_size, 'size': size, 'raw': raw, 'characteristics': self.u32(off + 36)})

    def unpack(self, fmt, offset):
        if offset < 0 or offset + struct.calcsize(fmt) > len(self.data):
            raise ValueError('PE offset outside image')
        return struct.unpack_from(fmt, self.data, offset)

    def u16(self, offset):
        return self.unpack('<H', offset)[0]

    def u32(self, offset):
        return self.unpack('<I', offset)[0]

    def offset(self, rva):
        if 0 <= rva < self.header_size:
            return rva
        for section in self.sections:
            delta = rva - section['rva']
            if 0 <= delta < section['size']:
                return section['raw'] + delta
        raise ValueError('RVA is not backed by file bytes')

    def cstring(self, offset, limit=1024):
        end = self.data.find(b'\0', offset, min(offset + limit, len(self.data)))
        if end < 0:
            raise ValueError('Unterminated PE string')
        return self.data[offset:end].decode('utf-8', 'replace')

    def exports(self, limit=50000):
        rva, size = self.unpack('<II', self.directory)
        if not rva:
            return []
        off = self.offset(rva)
        base, functions, names, fns, nms, ords = self.unpack('<IIIIII', off + 16)
        result = []
        for i in range(min(names, limit)):
            name = self.cstring(self.offset(self.u32(self.offset(nms) + i * 4)))
            ordinal = self.u16(self.offset(ords) + i * 2)
            if ordinal >= functions:
                continue
            address = self.u32(self.offset(fns) + ordinal * 4)
            result.append({'name': name, 'rva': address, 'ordinal': base + ordinal,
                           'forwarded': rva <= address < rva + size})
        return result

    def imports(self):
        rva, size = self.unpack('<II', self.directory + 8)
        if not rva:
            return []
        rows = []
        for index in range(min(size // 20, 2048)):
            descriptor = self.unpack('<5I', self.offset(rva) + index * 20)
            original, stamp, chain, name_rva, first = descriptor
            if not any(descriptor):
                break
            name = self.cstring(self.offset(name_rva))
            names = []
            table = self.offset(original or first)
            for i in range(30000):
                value = self.unpack('<Q' if self.pointer_size == 8 else '<I', table + i * self.pointer_size)[0]
                if not value:
                    break
                if value & (1 << (self.pointer_size * 8 - 1)):
                    names.append('#' + str(value & 65535))
                else:
                    names.append(self.cstring(self.offset(value) + 2))
            rows.append({'module': name, 'names': names})
        return rows


@contextmanager
def pe_file(path):
    with open(path, 'rb') as stream:
        with mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as data:
            yield PE(data)


class Asar:
    def __init__(self, path):
        self.path = Path(path)
        with self.path.open('rb') as stream:
            header = stream.read(16)
            if len(header) != 16:
                raise ValueError('Truncated ASAR header')
            size_size, size, payload_size, json_size = struct.unpack('<IIII', header)
            if size_size != 4 or not 2 <= json_size <= 32 * 1024 * 1024 or not json_size + 8 <= size <= json_size + 64:
                raise ValueError('Invalid ASAR header bounds')
            self.header = json.loads(stream.read(json_size))
            self.start = 8 + size
        self.entries = {}
        self._walk(self.header.get('files', {}), ())

    def _walk(self, files, path):
        if len(path) > 32 or len(self.entries) > 100000:
            raise ValueError('ASAR entry budget exceeded')
        for name, item in files.items():
            parts = path + (name,)
            if 'files' in item:
                self._walk(item['files'], parts)
            else:
                self.entries['/'.join(parts)] = item

    def read(self, name, max_bytes=8 * 1024 * 1024):
        item = self.entries[name]
        size, offset = int(item.get('size', -1)), int(item.get('offset', -1))
        if item.get('unpacked') or item.get('link') or not 0 <= size <= max_bytes or offset < 0:
            raise ValueError('ASAR entry is not an in-archive bounded file')
        if self.start + offset + size > self.path.stat().st_size:
            raise ValueError('ASAR entry outside archive')
        with self.path.open('rb') as stream:
            stream.seek(self.start + offset)
            return stream.read(size)
