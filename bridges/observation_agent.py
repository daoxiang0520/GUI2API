"""Expose observed native object metadata as an explicitly labeled snapshot.

This bridge never attaches to a process or invokes its functions. Refresh the
underlying native probe to obtain a new observation; it is not a live call API.
"""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from object_bridge import Fault, ObjectHost, PythonAdapter


class Observation:
    def __init__(self, record):
        self.record = record


class ObservationAdapter(PythonAdapter):
    runtime = 'native-observation'

    def __init__(self, source, captured_at, backend):
        self.source, self.captured_at, self.backend = source, captured_at, backend
        self.objects = {r['id']: Observation(r) for r in source['objects']}
        self.classes = {r.get('mangled_type', r['type']): r for r in source['classes']}
        self.host = None

    def describe(self, obj):
        record = obj.record
        description = self.classes[record.get('class', record['type'])]
        hierarchy, seen = [], set()
        current = description
        while current and current['type'] not in seen:
            seen.add(current['type'])
            hierarchy.append(current)
            current = self.classes.get(current.get('base_type'))
        properties, methods, events = {}, {}, {}
        for cls in reversed(hierarchy):
            for prop in cls.get('properties', []):
                properties[prop['name']] = {**prop, 'readable': False, 'writable': False, 'observation_only': True}
            for method in cls.get('methods', []):
                name = method['signature']
                methods[name] = {**method, 'name': name, 'member': method['name'],
                                 'parameters': [{'name': f'arg{i}', 'type': t, 'required': True} for i, t in enumerate(method['parameters'])],
                                 'effect': 'unknown', 'callable': False}
            for event in cls.get('events', []):
                events[event['signature']] = {'name': event['signature'], 'observable': False}
        relationships = []
        related = [('qobject_child', child) for child in record.get('children', [])]
        if record.get('parent'):
            related.append(('qobject_parent', record['parent']))
        for relation, identity in related:
            other = self.objects.get(identity)
            if other:
                relationships.append({'relation': relation, 'name': relation + ':' + other.record['type'],
                                      'object': self.host.box(other, force_object=True)})
        return {'type': record['type'], 'properties': list(properties.values()), 'methods': list(methods.values()),
                'events': list(events.values()), 'relationships': relationships, 'observation_only': True,
                'source_pid': self.source['pid'], 'captured_at': self.captured_at,
                'source_backend': self.backend, 'native_identity': record['id'],
                'source_stopped_by': self.source.get('stopped_by', []),
                'source_scope': self.source.get('scope', 'Qt5 static metadata, selected global roots and QObject child links'),
                'virtual_slots': description.get('virtual_slots', []),
                'capabilities': {'metadata': True, 'native_get': False, 'native_set': False,
                                 'native_call': False, 'native_event_watch': False}}

    def read(self, obj, member):
        raise Fault('observation_only', 'This reference describes an observed native object; live property reads are not connected')

    def write(self, obj, member, value):
        raise Fault('observation_only', 'This provider cannot write application objects')

    def call(self, obj, member, arguments):
        raise Fault('observation_only', 'Native invocation is not implemented; no function was called')

    def subscribe(self, obj, name, listener):
        raise Fault('observation_only', 'Native event hooks are not installed')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', required=True)
    parser.add_argument('--app', required=True)
    parser.add_argument('--backend', choices=('qt', 'cpp'), required=True)
    args = parser.parse_args()
    report = json.loads(Path(args.report).read_text(encoding='utf-8-sig'))
    source = report['apps'][args.app]['native_' + args.backend + '_objects']
    if not source.get('ok'):
        raise RuntimeError('No successful native observation in the supplied report')
    adapter = ObservationAdapter(source, report['observed_at'], args.backend)
    root_ids = source.get('roots') or list(adapter.objects)
    roots = {adapter.objects[i].record['type'] + ':' + i: adapter.objects[i] for i in root_ids if i in adapter.objects}
    host = ObjectHost(roots, adapter)
    adapter.host = host
    host.serve()


if __name__ == '__main__':
    main()
