"""Dynamic GUI-to-API: sources -> all exposed nodes -> generic typed methods.

No app/feature registration. Bootstraps select a framework transport, while
source-bound references ensure actions never silently switch target or channel.
"""
from collections import OrderedDict
import math
import threading
import time
import uuid
import copy

from appctl import ACTIONS
from desktop import DesktopSession
from object_bridge import Fault
from gui_contract import decorate
from gui_identity import annotate, group_controls, public_locator

PROTOCOL = 'appctl.gui/1'
SHORTCUTS = {'click':'invoke', 'set_text':'set', 'read_text':'get', 'select':'select',
             'check':'toggle-on', 'uncheck':'toggle-off', 'expand':'expand',
             'collapse':'collapse', 'scroll':'scroll', 'focus':'focus'}
NO_ARGS = {'type': 'object', 'properties': {}, 'additionalProperties': False}
METHODS = {
    name: {'inputSchema': NO_ARGS, 'effect': 'read' if name == 'get' else 'unknown',
           'verification': 'provider-advertised; business effect unverified'} for name in ACTIONS
}
METHODS['get'] = {'effect': 'read', 'inputSchema': {'type': 'object', 'properties': {
    'max_length': {'type': 'integer', 'minimum': 1, 'maximum': 100000}}, 'additionalProperties': False}}
METHODS['invoke'] = {'effect':'unknown','inputSchema':{'type':'object','properties':{
    'allow_unreliable':{'type':'boolean'}},'additionalProperties':False},
    'verification':'Legacy/Qt default actions require an explicit postcondition or allow_unreliable=true; prefer advertised click_input. Never automatically replay with another method.'}
METHODS['set'] = {'effect': 'write', 'inputSchema': {'type': 'object', 'properties': {
    'value': {'type': ['string', 'number'], 'maxLength': 100000},
    'expected': {'type': ['string', 'number']}, 'commit':{'enum':['none','enter']}}, 'required': ['value'], 'additionalProperties': False},
    'verification': 'Value read-back is not application submission. commit=enter explicitly sends one Enter after a successful set, with verified native focus; no retries.'}
METHODS['scroll'] = {'effect': 'write', 'inputSchema': {'type': 'object', 'properties': {
    'direction': {'enum': ['up', 'down', 'left', 'right']},
    'amount': {'type': 'number', 'minimum': 1, 'maximum': 10000}}, 'required': ['direction'], 'additionalProperties': False}}
INPUT_METHODS = ('click_input', 'type_text', 'press_keys', 'scroll_input')
SHORTCUTS.update({method:method for method in INPUT_METHODS})
METHODS['click_input'] = {'effect':'write','inputSchema':{'type':'object','properties':{
    'button':{'enum':['left','right']}},'additionalProperties':False},
    'verification':'SendInput once at freshly resolved control bounds; no automatic fallback from invoke'}
METHODS['scroll_input'] = {'effect':'write','inputSchema':{'type':'object','properties':{
    'direction':{'enum':['up','down','left','right']}, 'steps':{'type':'integer','minimum':1,'maximum':20}},
    'required':['direction'],'additionalProperties':False},
    'verification':'One trusted wheel batch at freshly resolved bounds in the active window; observe the resulting content'}
METHODS['type_text'] = {'effect':'write','inputSchema':{'type':'object','properties':{
    'text':{'type':'string','minLength':1,'maxLength':10000},'replace':{'type':'boolean'}},'required':['text'],'additionalProperties':False},
    'verification':'Trusted Unicode input to confirmed control focus; observe text afterward. replace selects all first.'}
METHODS['press_keys'] = {'effect':'write','inputSchema':{'type':'object','properties':{
    'keys':{'type':'string','maxLength':80}},'required':['keys'],'additionalProperties':False},
    'verification':'One CTRL/SHIFT/ALT chord or named key: ESC/ESCAPE, TAB, DELETE, BACKSPACE, F1-F12, LEFT/RIGHT/UP/DOWN, HOME/END/PAGEUP/PAGEDOWN; no retry'}
for method in INPUT_METHODS:
    METHODS[method]['inputSchema'] = {**METHODS[method]['inputSchema'], 'properties':{
        **METHODS[method]['inputSchema']['properties'],
        'guard_refs':{'type':'array','items':{'type':'string'},'maxItems':4}}}


def checked(result):
    if not result.get('ok'):
        e = result.get('error', {})
        raise Fault(e.get('code', 'runtime_error'), e.get('message', 'Request failed'))
    return result['data']


def validate_selector(args):
    for field in ('name', 'role', 'automation_id', 'backend', 'method', 'query', 'within_ref'):
        value = args.get(field)
        if value is not None and (not isinstance(value, str) or len(value) > 1024):
            raise Fault('invalid_arguments', field + ' must be a string of at most 1024 characters')
    for field in ('contains', 'exact', 'enabled', 'offscreen', 'onscreen'):
        if args.get(field) is not None and not isinstance(args[field], bool):
            raise Fault('invalid_arguments', field + ' must be a boolean')
    method = args.get('method')
    if args.get('onscreen') is not None and args.get('offscreen') is not None and args['onscreen']==args['offscreen']:
        raise Fault('invalid_arguments','onscreen and offscreen filters conflict')
    if method is not None and SHORTCUTS.get(method, method) not in METHODS:
        raise Fault('invalid_arguments', 'Unknown control method')


def filter_controls(rows, args):
    """Shared metadata selectors for Python, CLI and MCP. Never reads values."""
    validate_selector(args)
    by_ref = {row['ref']: row for row in rows}
    within = args.get('within_ref')
    if within is not None and within not in by_ref:
        raise Fault('stale_ref', 'The subtree root is absent from this observation; refresh or scan the full window')
    def descendant(row):
        parent, seen = row.get('parent'), set()
        while parent and parent not in seen:
            if parent == within: return True
            seen.add(parent)
            parent = by_ref.get(parent, {}).get('parent')
        return False
    method = SHORTCUTS.get(args.get('method'), args.get('method'))
    query = (args.get('query') or '').casefold()
    result = []
    for row in rows:
        matched = True
        for field in ('name', 'role', 'automation_id', 'backend'):
            wanted = args.get(field)
            if wanted is None: continue
            actual, wanted = str(row.get(field, '')).casefold(), wanted.casefold()
            if (wanted not in actual if field == 'name' and args.get('contains') else wanted != actual):
                matched = False; break
        if not matched: continue
        if any(args.get(field) is not None and row.get(field, False) != args[field] for field in ('enabled', 'offscreen')): continue
        if args.get('onscreen') is not None and row.get('offscreen',False)==args['onscreen']: continue
        if method is not None and method not in row.get('methods', row.get('actions', [])): continue
        texts=[str(row.get(field,'')).casefold() for field in ('name','role','automation_id','class_name')]
        if query and (not any(query==s for s in texts[:3]) if args.get('exact') else not all(any(term in s for s in texts) for term in query.split())): continue
        if within is not None and not descendant(row): continue
        result.append(row)
    if query:
        result.sort(key=lambda r:(str(r.get('name','')).casefold()!=query,
            str(r.get('automation_id','')).casefold()!=query,not str(r.get('name','')).casefold().startswith(query),
            r.get('offscreen',False),not r.get('enabled',True),len(r.get('name',''))))
    return result


class GuiSession:
    def __init__(self, desktop_factory=DesktopSession, *, object_config=None, discovery_cache=None, events_factory=None, learning_cache=None,
                 resource_root=None, resource_capture=None):
        self.desktop_factory, self._desktop = desktop_factory, None
        self.sources, self.refs = {}, OrderedDict()
        self.lock, self.closed = threading.RLock(), False
        self.cleanup = None
        self._discovery = None
        self.object_config, self.discovery_cache = object_config, discovery_cache
        self.events_factory, self._events = events_factory, None
        self.learning_cache, self._learning = learning_cache, None
        self.learning_error = None
        self.action_health = {}
        self.cache_stats = {'binding_hits':0, 'binding_misses':0, 'bound_reads':0}
        self.resource_root,self.resource_capture,self._resources=resource_root,resource_capture,None

    @property
    def resources(self):
        if self._resources is None:
            from gui_resources import Resources
            self._resources=Resources(self,self.resource_root,self.resource_capture)
        return self._resources

    @property
    def learning(self):
        if self._learning is None:
            import os
            from pathlib import Path
            from interaction_learning import InteractionLearning
            discovery_path = self.discovery.cache.path
            path = self.learning_cache or os.environ.get('GUI2API_LEARNING_CACHE') or discovery_path.with_name(discovery_path.stem+'.learning.json')
            self._learning = InteractionLearning(self, Path(path))
        return self._learning

    def _learn(self, operation, *args):
        # Learning is passive bookkeeping. Failure must never prevent an action,
        # change its result, or cause a dispatched action to be tried again.
        try:
            return getattr(self.learning, operation)(*args)
        except Exception as exc:
            self.learning_error = type(exc).__name__
            return None

    def _event_state(self, source, *, after=None, timeout_ms=0):
        target = source['target']
        if source.get('adapter') or not target.get('window') or not target.get('pid'):
            return {'watching':False, 'revision':None, 'changed':False,
                    'reason':'native_window_events_unavailable', 'complete':False}
        if self._events is None:
            from gui_events import WindowEvents
            self._events = (self.events_factory or WindowEvents)()
        if not source.get('event_registered'):
            state = self._events.subscribe(source['id'], target['window'], target['pid'])
            source['event_registered'] = True
            if not state['watching']:
                return state
        return self._events.read(source['id'], after=after, timeout_ms=timeout_ms)

    def _check_external(self, source, *, after=None, timeout_ms=0):
        collection = source.get('collection')
        baseline = collection.get('event_revision') if collection else None
        state = self._event_state(source, after=baseline if after is None else after, timeout_ms=timeout_ms)
        source['event_state'] = state
        observer_lost = bool(collection and collection.get('event_watching') and not state['watching'])
        observed_changed = baseline is not None and state['revision'] != baseline
        if collection and (observed_changed or observer_lost):
            if not source.get('dirty') and self._discovery:
                self._discovery.mark_dirty('gui:' + source['id'], 'gui_event' if not observer_lost else 'event_observer_lost')
            source['dirty'] = True
        return state

    def _binding_cache_fresh(self, source):
        """Topology is a bounded hint; every reused object is also read live."""
        state = self._check_external(source)
        collection = source.get('collection')
        if not collection or collection.get('cursor') or collection['options'].get('root_ref'):
            return False
        if time.monotonic()-collection['created'] > (30 if state['watching'] else 5):
            return False
        if collection.get('event_watching') and not state['watching']:
            return False
        structural = collection.get('structure_revision')
        if state['watching']:
            if structural is not None and state.get('structure_revision') is not None:
                return structural == state['structure_revision']
            return not state.get('changed') and not source.get('dirty')
        # Event-less adapters still require a fresh exact object observation.
        return True

    def _observe_bound(self, ref):
        """Read one original object; do not replace/validate the whole snapshot."""
        record = self._record(ref)
        source, old = self.sources[record['source']], record['row']
        if source.get('adapter'):
            if not getattr(source['adapter'], 'supports_bound_observation', False):
                return None
            response = source['adapter'].request({'op':'observe', 'ref':old['native_ref']})
            self._runtime_error(response)
        else:
            response = checked(self.desktop.request('observe', {'ref':old['native_ref']}))
        raw = response.get('element')
        if not response.get('fresh') or not isinstance(raw, dict) or raw.get('ref') != old['native_ref']:
            return None
        if not isinstance(raw.get('role'), str) or not isinstance(raw.get('actions'), list):
            return None
        current = {**raw, 'ref':ref, 'native_ref':old['native_ref'], 'source':source['id'],
                   'parent':old.get('parent'), 'methods':list(raw['actions'])}
        if not source.get('adapter') and not current.get('password'):
            rect = current.get('rect',{})
            live = not current.get('offscreen') and rect.get('width',0)>0 and rect.get('height',0)>0
            inputs = ['click_input','scroll_input'] if live else []
            if 'focus' in current['methods']:
                inputs.append('press_keys')
                if current['role'] in ('Edit','ComboBox','Group','Document','Custom') and not current.get('msaa_state',0)&0x40:
                    inputs.append('type_text')
            current['methods'].extend(inputs)
        if 'invoke' in current['methods']:
            weak=current.get('backend')=='msaa' or 'Qt' in current.get('framework','') or str(current.get('invoke_reliability','')).startswith('unreliable')
            current['invoke_reliability']='unreliable' if weak else 'provider_contract_only'
            current['recommended_method']='click_input' if weak and 'click_input' in current['methods'] else 'invoke_with_postcondition'
        self.cache_stats['bound_reads'] += 1
        return current

    @staticmethod
    def _same_bound_identity(old, current, *, include_label=True):
        fields = ('role','automation_id','class_name','backend','hwnd','invoke_reliability')
        same = all(old.get(field)==current.get(field) for field in fields)
        same &= bool(old.get('password')) == bool(current.get('password'))
        if include_label and old.get('role') not in ('Edit','ComboBox','Document','Group'):
            same &= old.get('name','') == current.get('name','')
        return bool(same)

    def _reuse_binding(self, ref, method=None):
        """A cache hit only selects a candidate. Fresh metadata permits reuse."""
        try:
            record = self._record(ref)
            source = self.sources[record['source']]
            if self._binding_cache_fresh(source):
                current = self._observe_bound(ref)
                if current is not None and self._same_bound_identity(record['row'],current) and (method is None or method in current['methods']) and self._binding_cache_fresh(source):
                    record['bound_observation'] = current
                    self.cache_stats['binding_hits'] += 1
                    return True
        except Exception:
            # No operation has been dispatched. Fall back to a normal observation,
            # which must still resolve the old capability or reject it.
            pass
        self.cache_stats['binding_misses'] += 1
        return False

    @property
    def discovery(self):
        if self._discovery is None:
            from discovery import DiscoveryManager
            self._discovery = DiscoveryManager(self, object_config=self.object_config, cache_path=self.discovery_cache)
        return self._discovery

    @property
    def desktop(self):
        if self._desktop is None:
            self._desktop = self.desktop_factory()
        return self._desktop

    def _attach(self, args):
        runtime = args.get('runtime', 'auto')
        if runtime not in ('auto', 'native', 'cdp-dom', 'electron-dom', 'wechat-qt'):
            raise Fault('invalid_arguments', 'runtime must be auto, native, cdp-dom, electron-dom or wechat-qt')
        backend = args.get('backend','auto')
        if backend not in ('auto','uia3','uia','msaa','win32'):
            raise Fault('invalid_arguments', 'backend must be auto, uia3, uia, msaa or win32')
        target, adapter, lease, bootstrap, info = {}, None, None, None, {}
        try:
            if runtime == 'cdp-dom':
                from gui_runtime import CdpDOM
                if not args.get('endpoint') or not args.get('target'):
                    raise Fault('invalid_arguments', 'An existing loopback endpoint and exact target are required')
                adapter = CdpDOM(args['endpoint'], args['target'])
                target = {'endpoint': args['endpoint'], 'target': args['target']}
            else:
                import psutil
                import win32process
                hwnd = args.get('window')
                if not isinstance(hwnd, int) or isinstance(hwnd, bool) or hwnd <= 0:
                    raise Fault('invalid_arguments', 'Select a current window handle from apps')
                info = checked(self.desktop.request('describe', {'window': hwnd}))
                process = psutil.Process(info['pid'])
                state = checked(self.desktop.request('window', {'window': hwnd, 'pid': process.pid}))
                target = {'window': hwnd, 'pid': process.pid, 'exe': process.exe(), 'created_at': process.create_time(),
                          'minimized': state['minimized'], 'tid': win32process.GetWindowThreadProcessId(hwnd)[0]}
                if runtime in ('electron-dom', 'wechat-qt'):
                    from messenger_api import ProcessLease
                    lease = ProcessLease(target['pid'], target['created_at'])
                if runtime == 'electron-dom':
                    from gui_runtime import ElectronDOM
                    adapter = ElectronDOM(self.desktop, target, args.get('api_module'))
                elif runtime == 'wechat-qt':
                    from qt_accessibility import QtAccessibilityLease
                    bootstrap = QtAccessibilityLease(target['pid'], target['created_at'], expected_exe=target['exe'])
                    bootstrap.enable()
            key = 'g' + uuid.uuid4().hex[:12]
            source = {'id': key, 'runtime': runtime, 'target': target, 'adapter': adapter,
                      'lease': lease, 'bootstrap': bootstrap, 'snapshot': None, 'backend':backend,
                      'discovery_info':info}
            self.sources[key] = source
            return {'source': key, 'runtime': runtime, 'target': target, 'feature_profiles': 0,
                    'architecture':'native discovery + explicit trusted input; runtime injection is opt-in',
                    'next': 'discover(source) builds a bounded control/object catalog; find also indexes control capabilities automatically; describe(ref) returns method schemas'}
        except Exception:
            try:
                if adapter: adapter.close()
            finally:
                try:
                    if bootstrap: bootstrap.close()
                finally:
                    if lease: lease.close()
            raise

    def _source(self, key):
        if not isinstance(key, str):
            raise Fault('invalid_arguments', 'source must be an attached source string')
        if key not in self.sources:
            raise Fault('unknown_source', 'Attach a source in this session first')
        source = self.sources[key]
        if 'pid' in source['target']:
            import psutil
            import win32process
            target = source['target']
            try:
                current_pid = win32process.GetWindowThreadProcessId(target['window'])[1]
                if current_pid != target['pid'] or psutil.Process(current_pid).create_time() != target['created_at']:
                    raise Fault('stale_process', 'The process/window identity changed')
            except (psutil.NoSuchProcess, psutil.AccessDenied) as exc:
                raise Fault('stale_process', str(exc)) from exc
        return source

    def _scan(self, args):
        source = self._source(args.get('source'))
        limit, depth, maximum = args.get('limit', 300), args.get('depth', 40), args.get('max_nodes', 12000)
        if any(not isinstance(n, int) or isinstance(n, bool) for n in (limit, depth, maximum)) or not 1 <= limit <= 3000 or not 0 <= depth <= 100 or not 1 <= maximum <= 20000:
            raise Fault('invalid_arguments', 'limit 1..3000, depth 0..100, max_nodes 1..20000')
        if not isinstance(args.get('include_hidden', False), bool):
            raise Fault('invalid_arguments', 'include_hidden must be a boolean')
        if args.get('cursor') is not None and not isinstance(args['cursor'], str):
            raise Fault('invalid_arguments', 'cursor must be a string')
        # Capture before enumeration: a change during a scan must cause the
        # following find to refresh rather than bless an inconsistent snapshot.
        event_state = self._event_state(source) if not args.get('cursor') else None
        if not args.get('cursor'):
            source.pop('collection', None)
        root = args.get('root_ref')
        if root:
            record = self._record(root)
            if record['source'] != source['id']:
                raise Fault('invalid_target', 'The subtree belongs to another source')
            root = record['row']['native_ref']
        if source['adapter']:
            data = source['adapter'].request({'op': 'scan', 'limit': limit, 'depth': depth, 'max_nodes': maximum,
                'include_hidden': bool(args.get('include_hidden')), 'cursor': args.get('cursor'), 'root_ref': root})
            self._runtime_error(data)
        else:
            cursor = args.get('cursor')
            if not cursor:
                backend = 'uia3' if source['bootstrap'] else source.get('backend','auto')
                preferred = self.discovery.preferred_backend(source) if backend == 'auto' and not args.get('all_channels') and not root else None
                request = {'window': source['target']['window'], 'backend': preferred or backend, 'view': 'raw', 'actionable': False,
                    'preserve_bindings':True,
                    'root_ref': root, 'depth': depth, 'limit': min(maximum,3000), 'time_budget_ms': 5000, 'probe_timeout': 8}
                result = self.desktop.request('inspect', request)
                if preferred and (not result.get('ok') or not any(set(row.get('actions', [])) - {'get','focus'} for row in result.get('data',{}).get('elements', []))):
                    result = self.desktop.request('inspect', {**request, 'backend':'auto'})
                    preferred = None
                raw = checked(result)
                raw['preferred_channel_only'] = preferred
                source['snapshot'] = {**raw, 'token': uuid.uuid4().hex}
                offset = 0
            else:
                try:
                    token, offset = cursor.split('/')
                    offset = int(offset)
                    if not source['snapshot'] or token != source['snapshot']['token'] or not 0 <= offset <= len(source['snapshot']['elements']):
                        raise ValueError()
                except (ValueError, AttributeError):
                    raise Fault('stale_cursor', 'Take a new scan; only its latest cursor is valid')
            raw = source['snapshot']
            all_rows = raw['elements']
            data = {k: v for k, v in raw.items() if k not in ('elements', 'token')}
            data.update(elements=all_rows[offset:offset+limit], total=len(all_rows),
                        cursor=raw['token']+'/'+str(offset+limit) if offset+limit < len(all_rows) else None,
                        collection_bounds={'depth':depth,'native_depth_limits':{'msaa':100,'uia3':20,'uia':20,'win32':20},'per_channel_nodes':min(maximum,3000),'time_budget_ms':5000,
                                           'depth_completeness':'unmeasured; providers may stop without counting deeper descendants'},
                        gaps=[{'kind':'provider_boundary', 'detail':'Raw UIA/MSAA/Win32 nodes only; virtualized/unexposed drawing objects may be absent'}])
        if isinstance(data.get('document_origin'),str) and len(data['document_origin'])<=4096:
            source['learning_document_origin'] = data['document_origin']
        rows = []
        for row in data['elements']:
            ref = source['id'] + '/' + row['ref']
            item = {**row, 'ref': ref, 'native_ref': row['ref'], 'parent': source['id']+'/'+row['parent'] if row.get('parent') else None,
                    'source': source['id'], 'methods': list(row.get('actions', [])), 'capability_state': 'advertised',
                    'purpose_evidence': row.get('name_source', 'provider name/role; inferred purpose is unverified')}
            declarations=item.pop('resources',[])
            if isinstance(declarations,list) and declarations:
                item['_resources']=declarations
                item['resource_kinds']=sorted({d['kind'] for d in declarations if isinstance(d,dict) and d.get('kind') in ('image','audio','video','document','file','text')})
                item['resource_count']=len(declarations)
            elif item.get('role') in ('Image','Canvas'):
                item.update(resource_kinds=['image'],resource_count=1)
            if not source['adapter'] and not item.get('password'):
                rect = item.get('rect',{})
                live = not item.get('offscreen') and rect.get('width',0) > 0 and rect.get('height',0) > 0
                inputs = ['click_input', 'scroll_input'] if live else []
                if 'focus' in item['methods']:
                    inputs.append('press_keys')
                    if item.get('role') in ('Edit','ComboBox','Group','Document','Custom') and not item.get('msaa_state',0) & 0x40:
                        inputs.append('type_text')
                item['methods'].extend(inputs)
                item['input_methods'] = inputs
                item['input_evidence'] = 'OS input; live identity, focus and occlusion checks at dispatch'
            if 'invoke' in item['methods']:
                weak=item.get('backend')=='msaa' or 'Qt' in item.get('framework','') or str(item.get('invoke_reliability','')).startswith('unreliable')
                item['invoke_reliability']='unreliable' if weak else 'provider_contract_only'
                item['recommended_method']='click_input' if weak and 'click_input' in item['methods'] else 'invoke_with_postcondition'
            self.refs[ref] = {'row': item, 'source': source['id'], 'seen': time.monotonic()}
            self.refs.move_to_end(ref)
            rows.append({k: v for k, v in item.items() if k != 'native_ref' and not k.startswith('_')})
        while len(self.refs) > 20000:
            self.refs.popitem(last=False)
        data['gaps'] = [{**gap, **({'ref':source['id']+'/'+gap['ref']} if gap.get('ref') else {})}
                        for gap in data.get('gaps', [])]
        collection = source.get('collection')
        if not args.get('cursor'):
            collection = source['collection'] = {'rows':OrderedDict(), 'snapshot':data.get('snapshot'),
                'observation_id':uuid.uuid4().hex,
                'options':{k:v for k,v in args.items() if k != 'cursor'}, 'created':time.monotonic(),
                'event_revision':event_state['revision'], 'event_watching':event_state['watching'],
                'structure_revision':event_state.get('structure_revision')}
            source['dirty'] = False
        if collection is None or collection['snapshot'] != data.get('snapshot'):
            raise Fault('stale_snapshot', 'The indexed observation changed between pages; scan again')
        for row in rows: collection['rows'][row['ref']] = row
        collection.update(cursor=data.get('cursor'), metadata={k:v for k,v in data.items() if k != 'elements'})
        if not data.get('cursor'):
            indexed=list(collection['rows'].values())
            annotate(indexed,source['target'].get('window'))
            collection['controls']=group_controls(indexed)
            for row in indexed:
                self.refs[row['ref']]['row'].update(row)
            collection['learning'] = self._learn('observe', source, collection)
        data['learning'] = collection.get('learning')
        return {**data, 'elements': rows, 'source': source['id'], 'runtime': source['runtime'], 'feature_profiles': 0}

    def _find(self, args, *, explicit=False):
        validate_selector(args)
        source = self._source(args.get('source'))
        limit, offset = args.get('limit', 100), args.get('offset', 0)
        if any(not isinstance(n, int) or isinstance(n, bool) for n in (limit, offset)) or not 1 <= limit <= 3000 or not 0 <= offset <= 20000:
            raise Fault('invalid_arguments', 'find limit must be 1..3000 and offset 0..20000')
        if not isinstance(args.get('refresh', False), bool) or not isinstance(args.get('compact', True), bool):
            raise Fault('invalid_arguments', 'refresh and compact must be booleans')
        state = self._check_external(source)
        max_age = 30 if state['watching'] else 5
        collection = source.get('collection')
        cached = bool(collection and not collection.get('cursor'))
        if args.get('refresh') or source.get('dirty') or not collection or collection['options'].get('root_ref') or time.monotonic()-collection['created'] > max_age:
            retained_backends={r.get('backend') for r in collection.get('rows',{}).values()} if collection else set()
            self._scan({'source':source['id'], 'limit':300, 'all_channels':args.get('refresh',False) or len(retained_backends)>1})
            collection, cached = source['collection'], False
        pages = 0
        while collection.get('cursor'):
            if pages >= 100 or len(collection['rows']) >= 20000:
                raise Fault('collection_limit', 'Narrow the source before indexing more controls')
            self._scan({**collection['options'], 'limit':300, 'cursor':collection['cursor']})
            collection, cached = source['collection'], False
            pages += 1
        rows = filter_controls(list(collection['rows'].values()), args)
        if not rows and collection['metadata'].get('preferred_channel_only') and not source.get('discovery_broadened_snapshot') == collection['snapshot']:
            # A preferred channel is a hint, never evidence that another provider
            # cannot expose the requested control. Only repeat a read observation.
            self._scan({'source':source['id'], 'limit':3000, 'all_channels':True})
            collection, cached = source['collection'], False
            while collection.get('cursor'):
                self._scan({**collection['options'], 'limit':3000, 'cursor':collection['cursor']})
                collection = source['collection']
            source['discovery_broadened_snapshot'] = collection['snapshot']
            rows = filter_controls(list(collection['rows'].values()), args)
        catalog = self.discovery.observe_gui(source, collection, rearm=bool(explicit and args.get('refresh')))
        collection['learning'] = self._learn('observe',source,collection)
        selected = rows[offset:offset+limit]
        if args.get('compact', True):
            selected = [public_locator(row) for row in selected]
        return {'source':source['id'], 'snapshot':collection['snapshot'], 'elements':selected,
                'matched':len(rows), 'returned':len(selected), 'offset':offset,
                'match_status':'found' if rows else 'not_observed',
                'absence_proven':False,'absence_explanation':None if rows else 'Control may be unrendered, virtualized, minimized, unexposed, or absent; this observation cannot distinguish them',
                'has_more':offset+len(selected)<len(rows), 'cached':cached,
                'nodes_indexed':len(collection['rows']), 'collection_truncated':collection['metadata'].get('truncated', False),
                'metadata_is_observation':True, 'observation_age_ms':round((time.monotonic()-collection['created'])*1000, 2),
                'freshness':{'watching':state['watching'], 'max_cache_age_ms':max_age*1000,
                             'event_source':state.get('provider'), 'complete':False,
                             'policy':'event invalidation plus bounded age; providers may omit events'},
                'discovery':{'catalog':catalog['id'], 'controls':len(catalog.get('controls',[])), 'operations':len(catalog['entries']),
                             'capabilities':len(catalog['entries']),'capabilities_unit':'legacy_operation_entries','trigger':catalog['reason']},
                'learning':collection.get('learning'),
                'coverage':collection['metadata'].get('coverage', 'provider-exposed objects only')}

    @staticmethod
    def _runtime_error(data):
        if data.get('error'):
            raise Fault(data['error'], data.get('message', data['error']))

    def _record(self, ref):
        if not isinstance(ref, str):
            raise Fault('invalid_arguments', 'ref must be a reference string from this session')
        record = self.refs.get(ref)
        if not record or time.monotonic()-record['seen'] > 600:
            raise Fault('stale_ref', 'Scan again; refs are session-bound and expire after ten minutes')
        self._source(record['source'])
        return record

    def _call(self, args):
        method, params = args.get('method'), args.get('arguments', {})
        if not isinstance(method, str) or not isinstance(params, dict):
            raise Fault('invalid_arguments', 'method must be a string and arguments an object')
        if method in ('set_text','set') and 'text' in params:
            if 'value' in params: raise Fault('invalid_arguments','Use value or text, not both')
            params={**params,'value':params['text']}; del params['text']
        method = SHORTCUTS.get(method, method)
        record = self._record(args.get('ref'))
        row, source = record['row'], self.sources[record['source']]
        if row.get('password'):
            raise Fault('protected_control', 'Protected controls cannot be invoked')
        if method not in row['methods']:
            raise Fault('protected_control' if row.get('password') else 'unsupported', 'This node does not advertise this method')
        from jsonschema import Draft202012Validator
        failures = list(Draft202012Validator(METHODS[method]['inputSchema']).iter_errors(params))
        if failures or any(isinstance(v, float) and not math.isfinite(v) for v in params.values()):
            raise Fault('invalid_arguments', failures[0].message if failures else 'Numbers must be finite')
        condition=self._validate_postcondition(source,args.get('postcondition'))
        if method=='get' and condition is not None:
            raise Fault('invalid_arguments','GUI postcondition applies to writes; inspect a read response directly')
        if method=='invoke' and row.get('invoke_reliability')=='unreliable' and condition is None and not params.get('allow_unreliable',False):
            raise Fault('unreliable_invoke','Provider default action may route to an ancestor or do nothing; choose advertised click_input, supply an explicit postcondition, or deliberately set allow_unreliable=true. Nothing was dispatched')
        commit=params.get('commit','none') if method=='set' else 'none'
        if commit=='enter' and (source.get('adapter') or 'press_keys' not in row['methods']):
            raise Fault('unsupported_argument','commit=enter requires an advertised native press_keys method; set alone does not submit')
        params={k:v for k,v in params.items() if k not in ('commit','allow_unreliable')}
        ticket = None
        def dispatch(operation, operation_method=None):
            nonlocal ticket
            dispatched_method=operation_method or method
            if dispatched_method != 'get':
                ticket = self._learn('begin',source,row,dispatched_method)
                source['dirty'] = True
                if self._discovery: self._discovery.mark_dirty('gui:' + source['id'], 'gui_mutation')
            return operation()
        try:
            response = self._execute_control(source,row,method,params,dispatch)
            if response.get('ok') and commit=='enter':
                if ticket: self._learn('finish',ticket,True,None)
                ticket=None
                try:
                    committed=self._execute_control(source,row,'press_keys',{'keys':'ENTER'},lambda operation:dispatch(operation,'press_keys'))
                except Exception as exc:
                    committed={'ok':False,'error':{'code':getattr(exc,'code','outcome_unknown'),'message':str(exc)}}
                if not committed.get('ok'):
                    response={'ok':False,'data':{**response['data'],'value_written':True,'commit_result':committed},
                              'error':{'code':'outcome_unknown','message':'Value was set; Enter was not confirmed. Observe actual state; no replay.'}}
                else: response['data'].update(commit='enter',commit_result=committed['data'],status='executed_unverified')
        except Exception as exc:
            if ticket: self._learn('finish',ticket,False,getattr(exc,'code','unexpected_exception'))
            raise
        if ticket: self._learn('finish',ticket,response.get('ok'),response.get('error',{}).get('code'))
        if response.get('ok') and method!='get':
            data=response['data']
            data.update(automatic_retries=0,suggested_verification={'source':source['id'],
                'ref':row['ref'],'operation':'get' if method in ('set','type_text') else 'find',
                'fields':['value'] if method in ('set','type_text') else ['name','enabled','offscreen','structure'],
                'business_completion_proven':False})
            if method=='set': data['submission']={'value_set':True,'committed':commit=='enter',
                'guidance':'A setter may not trigger application navigation; use type_text followed by explicit ENTER when the app requires keyboard input'}
            if method=='invoke':
                backend=data.get('backend','')
                weak=row.get('invoke_reliability')=='unreliable' or 'msaa'==row.get('backend') or 'Legacy' in backend or 'accDoDefaultAction' in backend or 'Qt' in row.get('framework','')
                data['invocation']={'requested_ref':row['ref'],'dispatch_binding':'original_provider_object',
                    'recipient':data.get('action_target',{'kind':'requested_object','provider_acceptance_only':True}),
                    'internal_recipient_verified':False,'ancestor_bubbling':'unknown',
                    'reliability':'unreliable' if weak or self.action_health.get(row['ref']) else 'provider_contract_only',
                    'recommended_method':'click_input' if weak and 'click_input' in row['methods'] else 'invoke_with_postcondition'}
                recipient=data.get('action_target',{})
                if recipient.get('kind')=='ancestor' or recipient.get('matches_requested') is False:
                    response={'ok':False,'data':data,'error':{'code':'invoke_target_mismatch',
                        'message':'Provider reported an ancestor/different recipient; inspect actual state. No fallback or retry.'}}
                    self.action_health[row['ref']]='recipient_mismatch'
            if condition is not None:
                verification=self._verify_postcondition(source,condition)
                data['postcondition']=verification
                if verification['passed']:
                    data['status']='postcondition_passed'
                else:
                    if method=='invoke': self.action_health[row['ref']]='postcondition_failed'
                    response={'ok':False,'data':data,'error':{'code':'postcondition_failed',
                        'message':'Action dispatched once; requested postcondition was not observed. Inspect actual state; do not replay.'}}
        return response

    def _validate_postcondition(self, source, condition):
        if condition is None: return None
        import json
        allowed={'ref','selector','field','equals','timeout_ms','interval_ms'}
        if not isinstance(condition,dict) or set(condition)-allowed or 'equals' not in condition or ('ref' in condition)==('selector' in condition):
            raise Fault('invalid_arguments','postcondition requires exactly one ref/selector, field, equals; optional timeout_ms/interval_ms')
        field=condition.get('field','value')
        if field not in ('value','name','enabled','offscreen','exists'): raise Fault('invalid_arguments','Unsupported postcondition field')
        try: json.dumps(condition,allow_nan=False)
        except (TypeError,ValueError) as exc: raise Fault('invalid_arguments','Postcondition must be finite JSON') from exc
        timeout,interval=condition.get('timeout_ms',1000),condition.get('interval_ms',100)
        if type(timeout) is not int or not 0<=timeout<=5000 or type(interval) is not int or not 50<=interval<=1000:
            raise Fault('invalid_arguments','Postcondition timeout_ms 0..5000, interval_ms 50..1000')
        if field in ('exists','enabled','offscreen') and type(condition['equals']) is not bool:
            raise Fault('invalid_arguments','Boolean postcondition field requires boolean equals')
        if 'ref' in condition:
            if self._record(condition['ref'])['source']!=source['id']: raise Fault('invalid_target','Postcondition must use the action source')
        else:
            selector=condition['selector']
            if not isinstance(selector,dict) or not selector or set(selector)-{'name','role','automation_id','backend','method','query','within_ref','contains','exact','enabled','offscreen','onscreen'}:
                raise Fault('invalid_arguments','Postcondition selector must contain metadata filters')
            validate_selector(selector)
        return {**copy.deepcopy(condition),'field':field,'timeout_ms':timeout,'interval_ms':interval}

    def _verify_postcondition(self, source, condition):
        from capability_registry import same_json
        deadline=time.monotonic()+condition['timeout_ms']/1000
        attempts=0; observation={}
        while True:
            attempts+=1
            try:
                bound = None; bound_read = None
                if 'ref' in condition and condition['field'] != 'exists':
                    if condition['field']=='value' and (not source.get('adapter') or getattr(source['adapter'],'supports_bound_observation',False)):
                        # Native/DOM get resolves and checks the original object
                        # itself, including protection and current value. A second
                        # metadata read would add a redundant provider round trip.
                        bound_read=self._call({'ref':condition['ref'],'method':'get','arguments':{'max_length':100000}})
                        bound={'value':bound_read.get('data',{}).get('value')}
                    else:
                        try: bound = self._observe_bound(condition['ref'])
                        except Exception: pass
                        if bound is not None and not self._same_bound_identity(self.refs[condition['ref']]['row'],bound,include_label=False):
                            bound = None
                if bound is not None:
                    field=condition['field']; observable=field in bound; value=bound.get(field)
                    if field=='value':
                        read=bound_read
                        value=read.get('data',{}).get('value')
                        observable=read.get('ok') and 'value' in read.get('data',{}) and not read.get('data',{}).get('label_only') and not read.get('data',{}).get('truncated')
                    observation={'observable':bool(observable),'matches':1,'passed':bool(observable and same_json(value,condition['equals'])),
                                 'read_scope':'bound_control','cached_value_used':False}
                    if observation['passed'] or time.monotonic()>=deadline:
                        return {**observation,'field':field,'read_attempts':attempts,'automatic_retries':0,
                                'business_completion_proven':False,'causality_verified':False}
                    time.sleep(min(condition['interval_ms']/1000,max(0,deadline-time.monotonic())))
                    continue
                data=self._find({'source':source['id'],'refresh':True,'compact':False,'limit':3000,**condition.get('selector',{})})
                rows=data['elements']
                if 'ref' in condition:
                    # The complete observation, not just its first result page.
                    current=source['collection']['rows'].get(condition['ref'])
                    rows=[current] if current is not None else []
                field=condition['field']; count=len(rows)
                observable=True; value=None
                if field=='exists':
                    # Missing in a partial tree is inconclusive, never proof of absence.
                    observable=bool(count) or not data.get('collection_truncated')
                    value=count>0
                elif count==1:
                    if field=='value':
                        read=self._call({'ref':rows[0]['ref'],'method':'get','arguments':{'max_length':100000}})
                        value=read.get('data',{}).get('value')
                        observable=read.get('ok') and 'value' in read.get('data',{}) and not read.get('data',{}).get('label_only') and not read.get('data',{}).get('truncated')
                    else: value=rows[0].get(field); observable=field in rows[0]
                else: observable=False
                observation={'observable':bool(observable),'matches':count,'passed':bool(observable and same_json(value,condition['equals'])),
                             'read_scope':'window_snapshot','cached_value_used':False}
            except Exception as exc:
                observation={'passed':False,'observable':False,'error':{'code':getattr(exc,'code','observation_failed')}}
            if observation['passed'] or time.monotonic()>=deadline:
                return {**observation,'field':condition['field'],'read_attempts':attempts,'automatic_retries':0,
                        'business_completion_proven':False,'causality_verified':False}
            time.sleep(min(condition['interval_ms']/1000,max(0,deadline-time.monotonic())))

    def _window_action(self, args):
        source=self._source(args.get('source')); action=args.get('action','get')
        if source.get('adapter') or not source['target'].get('window'):
            raise Fault('unsupported','Window actions require an attached native window')
        target=source['target']
        if action in ('get','bring_to_front','restore'):
            data=checked(self.desktop.request('window',{'window':target['window'],'pid':target['pid'],
                'state':'activate' if action=='bring_to_front' else action}))
        elif action=='read_console':
            # TextPattern is the first choice; a label-only get is never output.
            observed=self._find({'source':source['id'],'refresh':True,'compact':False,'limit':3000})
            texts=[r for r in observed['elements'] if r.get('text_pattern') and 'get' in r.get('methods',[])]
            texts=[r for r in texts if not r.get('offscreen')]
            logical={r.get('control_id',r['ref']):r for r in texts}
            if len(logical)>1:
                return {'source':source['id'],'action':action,'status':'ambiguous','read_performed':False,
                        'candidates':[public_locator(r) for r in logical.values()],
                        'next_action':'get_selected_TextPattern_ref','automatic_retries':0}
            if texts:
                read=self._call({'ref':texts[0]['ref'],'method':'get','arguments':{'max_length':100000}})
                data=checked(read)
                if data.get('label_only'): raise Fault('console_unavailable','TextPattern did not expose terminal output')
                data.update(read_only=True,coverage='partial')
            else:
                import subprocess, sys
                from pathlib import Path
                result=subprocess.run([sys.executable,str(Path(__file__).with_name('console_reader.py')),str(target['pid']),str(target['created_at'])],
                    capture_output=True,text=True,encoding='utf-8',timeout=5,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                import json
                if result.returncode or len(result.stdout)>1024*1024: raise Fault('console_unavailable','Isolated console read failed')
                data=checked(json.loads(result.stdout))
        elif action in ('dismiss_popup','request_accessibility','escape'):
            data=checked(self.desktop.request(action,{'window':target['window'],'pid':target['pid']}))
        else: raise Fault('invalid_arguments','window_action: get, bring_to_front, restore, dismiss_popup, escape, request_accessibility, read_console')
        if action not in ('get','read_console'):
            source['dirty']=True
            if self._discovery: self._discovery.mark_dirty('gui:'+source['id'],'window_'+action)
        return {**data,**({'output_source':data.get('source')} if action=='read_console' else {}),
                'source':source['id'],'action':action,'automatic_retries':0}

    def _execute_control(self, source, row, method, params, dispatch):
        if method in INPUT_METHODS:
            guards = []
            for guard_ref in params.get('guard_refs',[]):
                guard = self._record(guard_ref)
                if guard['source'] != source['id']: raise Fault('invalid_target','Input guards must belong to the same GUI source')
                guards.append(guard['row']['native_ref'])
            data = checked(dispatch(lambda:self.desktop.request('input', {'ref':row['native_ref'],'method':method,
                'arguments':{k:v for k,v in params.items() if k != 'guard_refs'}, 'guard_refs':guards})))
        elif source['adapter']:
            data = dispatch(lambda:source['adapter'].request({'op':'call', 'ref':row['native_ref'], 'method':method, 'arguments':params}))
            self._runtime_error(data)
        else:
            if method == 'scroll' and 'amount' in params:
                raise Fault('unsupported_argument', 'Native scroll uses the provider step; amount is available on DOM sources')
            if method == 'set' and 'expected' in params:
                before = checked(self.desktop.request('get', {'ref':row['native_ref'], 'max_length':100000}))
                if before.get('label_only') or before.get('truncated') or before.get('value') != params['expected']:
                    raise Fault('value_conflict', 'The full current value did not match expected; nothing was written')
            result = dispatch(lambda:self.desktop.request(method, {'ref':row['native_ref'], **{k:v for k,v in params.items() if k != 'expected'}}))
            if method == 'set' and result.get('error',{}).get('code') == 'verification_failed':
                # Chromium accessibility can publish the new value after SetValue returns.
                # Poll only reads. Never repeat the setter after a delayed result.
                deadline = time.monotonic() + .6
                while time.monotonic() < deadline:
                    time.sleep(.04)
                    observed = self.desktop.request('get', {'ref':row['native_ref'],'max_length':100000})
                    value = observed.get('data',{})
                    if observed.get('ok') and not value.get('label_only') and not value.get('truncated') and value.get('value') == params['value']:
                        result = {'ok':True,'data':{**value,'status':'verified','verification':'delayed read-back; setter dispatched once'}}
                        break
            if not result.get('ok') and result.get('data'):
                return {'ok':False,'data':result['data'],'error':result['error']}
            data = checked(result)
        if data.get('status') == 'verification_failed':
            return {'ok':False,'data':data,'error':{'code':'verification_failed','message':'Read-back did not match; the action was not repeated'}}
        return {'ok':True, 'data':{**data, 'ref':row['ref'], 'method':method,'observation_invalidated':method != 'get'}}

    def _detach(self, key):
        source = self.sources.pop(key, None)
        if source is None:
            raise Fault('unknown_source', 'Source is not attached')
        if self._resources:self._resources.release_source(key)
        if self._discovery: self._discovery.release_source(key)
        if self._events: self._events.unsubscribe(key)
        if self._learning: self._learn('release',key)
        cleanup = {}
        try:
            if source['adapter']: cleanup.update(source['adapter'].close() or {})
        finally:
            try:
                if source['bootstrap']:
                    original = source['bootstrap'].original == b'\1'
                    source['bootstrap'].close()
                    cleanup['bootstrap'] = {'close_completed':True, 'original_active':original}
            finally:
                if source['lease']: source['lease'].close()
                self.refs = OrderedDict((k,v) for k,v in self.refs.items() if v['source'] != key)
        return {'source':key, 'detached':True, 'cleanup':cleanup}

    def request(self, op, args=None):
        return decorate(self._request(op,args))

    def _request(self, op, args=None):
        if not isinstance(op, str) or not op or args is not None and not isinstance(args, dict):
            return {'ok':False,'error':{'code':'invalid_arguments','message':'op must be a nonempty string and args an object'},'protocol':PROTOCOL}
        args = {} if args is None else args
        with self.lock:
            try:
                if op == 'close':
                    if args: raise Fault('invalid_arguments', 'close does not accept arguments')
                    return {'ok':True,'data':{'cleanup':self.close()},'protocol':PROTOCOL}
                if self.closed: raise Fault('session_closed', 'Create a new GUI session')
                if op in SHORTCUTS:
                    params = {k:v for k,v in args.items() if k not in ('ref','postcondition')}
                    op, args = 'call', {'ref':args.get('ref'), 'method':op, 'arguments':params,'postcondition':args.get('postcondition')}
                if op == 'schema':
                    data = {'protocol':PROTOCOL, 'operations':['apps','targets','attach','scan','find','describe','call','detach','close',
                            'discover','discovery_describe','discovery_invoke','discovery_events','learning_graph','rebind','learning_export','learning_import','window_action',
                            'resources','resource_describe','resource_read','resource_release',*SHORTCUTS],
                            'shortcuts':SHORTCUTS,
                            'methods':METHODS, 'app_feature_registration_required':False, 'universal_coverage':False,
                            'selectors':{'fields':['name','role','automation_id','backend','method','query','within_ref','enabled','offscreen','onscreen'],
                                'exact':'exact query name/id match; otherwise all space-separated terms with exact-name relevance first',
                                'zero_matches':'not_observed; never proof of absent functionality'},
                            'postcondition':{'arguments':['ref OR selector','field','equals','timeout_ms (0..5000)','interval_ms (50..1000)'],
                                'fields':['value','name','enabled','offscreen','exists'],
                                'scope':'current provider observation; no proof of application-wide absence or causality',
                                'checks':'bounded reads only; invalid conditions rejected before dispatch; no mutation retries'},
                            'window_action':{'actions':['get','restore','bring_to_front','dismiss_popup','escape','request_accessibility','read_console'],
                                'escape':'one ESC to verified foreground window, without editor focus; no arbitrary window typing',
                                'accessibility':'documented client handshake only; Chromium launch flag returned as explicit relaunch plan, never auto-run'},
                            'input_methods':list(INPUT_METHODS),
                            'resources':{'format':'appctl.resources/1','kinds':['image','audio','video','document','file','text'],
                                'discovery':'metadata only; no automatic byte fetch, recording or screenshot',
                                'read':'source bytes or explicit rendered representation; immutable artifacts with SHA256',
                                'native_limits':'image/canvas roles expose rendered candidates, not original files',
                                'retention':'artifact files remain until explicit cleanup; live refs are session-bound',
                                'model_support':'native delivery depends on host and model; a file reference is not perception'},
                            'learning':{'format':'appctl.learning/1','mode':'incremental from requested actions and later observations',
                                'automatic_exploration_actions':0,'graph_query':'learning_graph(source)',
                                'persistent_binding_authority':False,'stored_ui_values':False},
                            'discovery':{'format':'appctl.discovery/1','automatic_control_index':'first find',
                                'bound_cache':'bounded topology plus fresh exact-object metadata; miss triggers observation, never mutation replay',
                                'postcondition_cache':'ref value/name/enabled/offscreen reads one live object; selector/exists keeps full observation',
                                'object_probes':'discover(source) probes OBJID_NATIVEOM; object_target explores configured runtime roots',
                                'default_business_calls':0,'native_metadata':'explicit read-only opt-in',
                                'cache':'schema hashes and channel hints; no persisted refs or application values',
                                'unknown_results':'invalidate and reobserve; no mutation retries',
                                'gui_freshness':'native window events plus 30s maximum cache age; other sources 5s',
                                'gui_events':'automatic coalesced invalidation; incomplete provider event coverage'},
                            'bootstraps':{'auto':'UIA/MSAA/Win32 discovery plus explicit trusted input; no runtime injection', 'native':'UIA/MSAA/Win32; no injection', 'cdp-dom':'existing CDP endpoint',
                                'electron-dom':'Node-API injection, local addon retained until process exit; api_module may be required',
                                'wechat-qt':'version-specific Qt accessibility data-byte lease; generic UIA enumeration afterward'}}
                elif op == 'apps': data = checked(self.desktop.request('apps'))
                elif op == 'targets': data = checked(self.desktop.request('targets', args))
                elif op == 'attach': data = self._attach(args)
                elif op == 'scan': data = self._scan(args)
                elif op == 'find': data = self._find(args,explicit=True)
                elif op in ('resources','resource_describe','resource_read','resource_release'):
                    import inspect
                    function={'resources':self.resources.list,'resource_describe':self.resources.describe,
                              'resource_read':self.resources.read,'resource_release':self.resources.release}[op]
                    try:inspect.signature(function).bind(**args)
                    except TypeError as exc:raise Fault('invalid_arguments',str(exc)) from exc
                    data=function(**args)
                elif op in ('learning_graph','rebind','learning_export','learning_import'):
                    import inspect
                    function={'learning_graph':self.learning.query,'rebind':self.learning.rebind,
                              'learning_export':self.learning.export,'learning_import':self.learning.import_candidates}[op]
                    try: inspect.signature(function).bind(**args)
                    except TypeError as exc: raise Fault('invalid_arguments',str(exc)) from exc
                    data = function(**args)
                elif op == 'window_action': data=self._window_action(args)
                elif op in ('discover','discovery_describe','discovery_invoke','discovery_events'):
                    return {**self.discovery.request(op,args), 'protocol':PROTOCOL}
                elif op == 'describe':
                    row = self._record(args.get('ref'))['row']
                    data = {k:v for k,v in row.items() if k != 'native_ref' and not k.startswith('_')}
                    data.update(methods={m:METHODS[m] for m in row['methods']}, metadata_is_last_observation=True,
                                aliases={k:v for k,v in SHORTCUTS.items() if k != v and v in row['methods']})
                elif op == 'call': return {**self._call(args), 'protocol':PROTOCOL}
                elif op == 'detach': data = self._detach(args.get('source'))
                else: raise Fault('unknown_operation', 'See schema for available operations')
                return {'ok':True,'data':data,'protocol':PROTOCOL}
            except Fault as exc:
                return {'ok':False,'error':{'code':exc.code,'message':str(exc)},'protocol':PROTOCOL}
            except Exception as exc:
                mutation = op == 'discovery_invoke' or op == 'call' and args.get('method') != 'get' or op=='window_action' and args.get('action','get') not in ('get','read_console')
                return {'ok':False,'error':{'code':'outcome_unknown' if mutation else 'runtime_error','message':str(exc)},'protocol':PROTOCOL}

    def close(self):
        with self.lock:
            if self.closed: return self.cleanup
            outcomes = []
            if self._discovery:
                try: self._discovery.close()
                except Exception as exc: outcomes.append({'discovery_cleanup_error':str(exc)})
            for key in list(self.sources):
                try: outcomes.append(self._detach(key))
                except Exception as exc: outcomes.append({'source':key,'cleanup_error':str(exc)})
            if self._events:
                try:
                    result = self._events.close()
                    if not result.get('closed'): outcomes.append({'event_cleanup_error':'event thread did not stop'})
                except Exception as exc: outcomes.append({'event_cleanup_error':str(exc)})
            try:
                if self._desktop: self._desktop.close()
            except Exception as exc:
                outcomes.append({'desktop_cleanup_error':str(exc)})
            finally:
                self.closed, self.cleanup = True, outcomes
                if self._resources:self._resources.close()
                self.refs.clear()
            return self.cleanup

    def __enter__(self): return self
    def __exit__(self, *_): self.close()
