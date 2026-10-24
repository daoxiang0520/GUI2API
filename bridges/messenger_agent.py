"""Expose explicitly selected application fields through appctl.objects/1."""
import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from messenger_api import MessengerSession, TextField
from object_bridge import ObjectHost, PythonAdapter


class MessengerAdapter(PythonAdapter):
    def describe(self, obj):
        description = super().describe(obj)
        if isinstance(obj, MessengerSession):
            description['methods'] = [m for m in description['methods'] if m['name'] == 'status']
            description['properties'] = [p for p in description['properties'] if p['name'] in ('search', 'draft')]
            for prop in description['properties']:
                prop['writable'] = False
        if isinstance(obj, (MessengerSession, TextField)):
            session = obj if isinstance(obj, MessengerSession) else obj._session
            for member in [*description['properties'], *description['methods']]:
                member['binding_profile_digest'] = session._profile_hash
            if isinstance(obj, TextField) and session._profile['slots'][obj._name].get('write_enabled') is False:
                for method in description['methods']:
                    if method['name'] == 'replace':
                        method['callable'] = False
        return description


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--app', required=True)
    parser.add_argument('--profiles')
    args = parser.parse_args()
    with MessengerSession(args.app, args.profiles) as session:
        ObjectHost({'application': session}, adapter=MessengerAdapter()).serve()


if __name__ == '__main__':
    main()
