"""Persistent NDJSON GUI CLI. Refs and attached sources belong to one session."""
import argparse
import json
import sys

from gui_api import GuiSession, PROTOCOL
from gui_contract import decorate


def dispatch_line(session, line):
    try:
        request = json.loads(line)
    except (ValueError, TypeError) as exc:
        return decorate({'ok':False,'id':None,'protocol':PROTOCOL,'error':{'code':'invalid_json','message':str(exc)}})
    request_id = request.get('id') if isinstance(request, dict) else None
    if not isinstance(request, dict) or not isinstance(request.get('op'), str) or not request['op'] or (
            'args' in request and not isinstance(request['args'], dict)):
        return decorate({'ok':False,'id':request_id,'protocol':PROTOCOL,
                'error':{'code':'invalid_request','message':'Use an object with a nonempty op string and optional args object'}})
    return {**session.request(request['op'], request.get('args', {})), 'id':request_id}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--object-config', help='Explicit object-provider configuration for discovery')
    parser.add_argument('--discovery-cache', help='Metadata-only discovery cache path')
    parser.add_argument('--learning-cache', help='Incremental metadata graph path; contains no live refs or UI values')
    commands = parser.add_subparsers(dest='operation', required=True)
    for operation in ('schema', 'apps', 'session'): commands.add_parser(operation)
    pages = commands.add_parser('targets', help='List pages on an existing loopback CDP endpoint')
    pages.add_argument('--endpoint', required=True)
    inspect = commands.add_parser('inspect', help='Read-only control discovery; use session for persistent refs')
    inspect.add_argument('--window', type=lambda value:int(value, 0))
    inspect.add_argument('--app')
    inspect.add_argument('--title')
    inspect.add_argument('--runtime', choices=['auto','native','cdp-dom','electron-dom','wechat-qt'], default='auto')
    inspect.add_argument('--endpoint')
    inspect.add_argument('--target')
    inspect.add_argument('--api-module')
    inspect.add_argument('--discovery-backend', choices=['auto','uia3','uia','msaa','win32'], default='auto')
    for field in ('name', 'role', 'automation-id', 'method', 'query', 'backend'):
        inspect.add_argument('--'+field)
    inspect.add_argument('--contains', action='store_true')
    inspect.add_argument('--exact', action='store_true')
    inspect.add_argument('--onscreen', action='store_true', default=None)
    inspect.add_argument('--enabled', action='store_true', default=None)
    inspect.add_argument('--limit', type=int, default=100)
    inspect.add_argument('--full', action='store_true', help='Include all observed node metadata')
    discover = commands.add_parser('discover', help='Automatically catalog controls/objects; no candidate methods are executed')
    group = discover.add_mutually_exclusive_group(required=True)
    group.add_argument('--window', type=lambda value:int(value, 0))
    group.add_argument('--object-target')
    group.add_argument('--registered-app')
    discover.add_argument('--query', default='')
    discover.add_argument('--exact', action='store_true')
    discover.add_argument('--role')
    discover.add_argument('--enabled', action='store_true',default=None)
    discover.add_argument('--onscreen', action='store_true',default=None)
    discover.add_argument('--layout',choices=['controls','operations'],default='controls')
    discover.add_argument('--limit', type=int, default=100)
    discover.add_argument('--offset', type=int, default=0)
    discover.add_argument('--native-metadata', action='store_true')
    discover.add_argument('--max-objects', type=int, default=32)
    discover.add_argument('--max-depth', type=int, default=3)
    discover.add_argument('--max-seconds', type=float, default=10)
    learning = commands.add_parser('learning', help='Read relations incrementally registered during normal use; no exploratory clicks')
    learning.add_argument('--window', required=True, type=lambda value:int(value,0))
    learning.add_argument('--query', default='')
    learning.add_argument('--relation', choices=['all','contains','transitions'], default='all')
    learning.add_argument('--limit', type=int, default=100)
    learning.add_argument('--offset', type=int, default=0)
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stdin.reconfigure(encoding='utf-8-sig')
    with GuiSession(object_config=args.object_config, discovery_cache=args.discovery_cache, learning_cache=args.learning_cache) as session:
        if args.operation != 'session':
            if args.operation == 'inspect':
                from gui_controls import GUI, GuiError
                try:
                    gui = GUI(session)
                    window = gui.window(window=args.window, app=args.app, title=args.title, runtime=args.runtime,
                        endpoint=args.endpoint, target=args.target, api_module=args.api_module, backend=args.discovery_backend)
                    fields = ('name','role','automation_id','method','query','backend','contains','limit','exact','enabled','onscreen')
                    result = session.request('find', {'source':window.source, 'compact':not args.full,
                        **{field:getattr(args, field) for field in fields if getattr(args, field) is not None}})
                    if result.get('ok'): result['data']['references_valid_after_exit'] = False
                except GuiError as exc:
                    result = {**exc.response,'protocol':PROTOCOL}
                except ValueError as exc:
                    result = {'ok':False,'protocol':PROTOCOL,'error':{'code':'invalid_arguments','message':str(exc)}}
            elif args.operation == 'learning':
                attached = session.request('attach', {'window':args.window})
                result = session.request('learning_graph', {'source':attached['data']['source'],
                    **{field:getattr(args,field) for field in ('query','relation','limit','offset')}}) if attached.get('ok') else attached
                if result.get('ok'): result['data']['references_valid_after_exit'] = False
            elif args.operation == 'discover':
                options = {field:getattr(args,field) for field in ('object_target','registered_app','query','limit','offset','native_metadata','max_objects','max_depth','max_seconds','exact','role','enabled','onscreen','layout')}
                if args.window:
                    attached = session.request('attach', {'window':args.window})
                    if not attached.get('ok'):
                        print(json.dumps(attached,ensure_ascii=False)); return 1
                    options['source'] = attached['data']['source']
                result = session.request('discover', options)
            else:
                result = session.request(args.operation, {'endpoint':args.endpoint} if args.operation == 'targets' else {})
            print(json.dumps(result, ensure_ascii=False))
            return 0 if result.get('ok') else 1
        for line in sys.stdin:
            result = dispatch_line(session, line)
            print(json.dumps(result, ensure_ascii=False), flush=True)
            if session.closed: break
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
