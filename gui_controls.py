"""Small control objects over the generic GUI API. No business feature registry.

Read-only discovery is cached; writes refresh the next lookup. Controls retain
their original source/ref, checked again by their provider. Refresh explicitly
after external layout changes. A returned click can be executed_unverified.
"""
from __future__ import annotations

from gui_api import GuiSession, filter_controls
from object_bridge import Fault


class GuiError(RuntimeError):
    def __init__(self, response):
        self.response = response
        error = response.get('error', {})
        self.code = error.get('code', 'gui_error')
        super().__init__(error.get('message', self.code))


def require(response):
    if not response.get('ok'):
        raise GuiError(response)
    return response['data']


class Control:
    def __init__(self, window, metadata):
        self.window = window
        self.metadata = dict(metadata)

    @property
    def id(self): return self.metadata['ref']

    @property
    def name(self): return self.metadata.get('name', '')

    @property
    def role(self): return self.metadata.get('role', '')

    @property
    def methods(self): return tuple(self.metadata.get('methods', ()))

    def perform(self, method, *, postcondition=None, **arguments):
        self.window._ensure_attached()
        response = self.window.gui.session.request('call', {'ref': self.id, 'method': method, 'arguments': arguments,
            **({'postcondition':postcondition} if postcondition is not None else {})})
        if method not in ('get','read_text'): self.window._controls = None
        return require(response)

    def read(self, max_length=4096): return self.perform('get', max_length=max_length)

    @property
    def text(self): return self.read()['value']

    @text.setter
    def text(self, value): self.set_text(value)

    def set_text(self, text, *, expected=None, commit=None, postcondition=None):
        params = {'value': text}
        if expected is not None: params['expected'] = expected
        if commit is not None: params['commit']=commit
        return self.perform('set', postcondition=postcondition, **params)

    def click(self, *, postcondition=None): return self.perform('invoke',postcondition=postcondition)
    def select(self): return self.perform('select')
    def focus(self): return self.perform('focus')
    def click_input(self, button='left'): return self.perform('click_input', button=button)
    def scroll_input(self, direction, steps=3): return self.perform('scroll_input', direction=direction, steps=steps)
    def type_text(self, text, *, replace=False): return self.perform('type_text', text=text, replace=replace)
    def press_keys(self, keys): return self.perform('press_keys', keys=keys)
    def check(self): return self.perform('toggle-on')
    def uncheck(self): return self.perform('toggle-off')
    def expand(self): return self.perform('expand')
    def collapse(self): return self.perform('collapse')

    def scroll(self, direction, *, amount=None):
        params = {'direction': direction}
        if amount is not None: params['amount'] = amount
        return self.perform('scroll', **params)

    def describe(self):
        self.window._ensure_attached()
        return require(self.window.gui.session.request('describe', {'ref': self.id}))

    def children(self, **selector):
        return [control for control in self.window.controls(**selector) if control.metadata.get('parent') == self.id]

    def descendants(self, **selector):
        return self.window.controls(within_ref=self.id, **selector)

    def __repr__(self): return f'Control(role={self.role!r}, name={self.name!r}, id={self.id!r})'


class Window:
    def __init__(self, gui, attachment):
        self.gui, self.source = gui, attachment['source']
        self.attachment = attachment
        self.snapshot = None
        self._controls = None
        self.detached = False

    def _ensure_attached(self):
        if self.detached or self.gui.closed:
            raise GuiError({'error': {'code': 'source_closed', 'message': 'Attach a new window before calling controls'}})

    def refresh(self, *, page_size=300, depth=40, max_nodes=12000, include_hidden=False):
        self._ensure_attached()
        self._controls, self.snapshot = None, None
        rows, cursor, snapshot, pages = [], None, None, 0
        while True:
            data = require(self.gui.session.request('scan', {'source': self.source,
                'limit': page_size, 'depth': depth, 'max_nodes': max_nodes,
                'cursor': cursor, 'include_hidden': include_hidden}))
            rows.extend(data['elements'])
            pages += 1
            if snapshot is not None and data.get('snapshot') != snapshot:
                raise GuiError({'error': {'code': 'stale_snapshot', 'message': 'The snapshot changed between pages'}})
            snapshot = data.get('snapshot')
            cursor = data.get('cursor')
            if not cursor:
                break
            if pages >= 100 or len(rows) >= 20000:
                raise GuiError({'error': {'code': 'collection_limit', 'message': 'Narrow the source before collecting more controls'}})
        self.snapshot = {k: v for k, v in data.items() if k != 'elements'}
        self.snapshot['pages'] = pages
        self._controls = [Control(self, row) for row in rows]
        return self

    def controls(self, *, refresh=False, name=None, role=None, automation_id=None, method=None, backend=None,
                 query=None, contains=False, enabled=None, offscreen=None, within_ref=None, exact=False, onscreen=None):
        self._ensure_attached()
        # Use the session's shared metadata cache/events/TTL. A second wrapper
        # cache must not conceal external GUI changes from the freshness policy.
        options={'source':self.source,'name':name,'role':role,'automation_id':automation_id,
            'method':method,'backend':backend,'query':query,'contains':contains,'enabled':enabled,
            'offscreen':offscreen,'within_ref':within_ref,'exact':exact,'onscreen':onscreen,
            'refresh':refresh,'compact':False,'limit':3000}
        result=[];offset=0
        while True:
            data=require(self.gui.session.request('find',{**options,'offset':offset}))
            result.extend(Control(self,row) for row in data['elements'])
            if not data.get('has_more'): return result
            offset+=len(data['elements']);options['refresh']=False
            if not data['elements'] or offset>20000: raise GuiError({'error':{'code':'collection_limit','message':'Narrow the control query'}})

    def one(self, **selector):
        matches = self.controls(**selector)
        if len(matches) != 1:
            raise GuiError({'error': {'code': 'ambiguous_control' if matches else 'control_not_found',
                'message': f'Expected one control; got {len(matches)}. Add role/backend/automation_id or refresh.'},
                'matches': [m.metadata for m in matches]})
        return matches[0]

    def learning_graph(self, **options):
        self._ensure_attached()
        return require(self.gui.session.request('learning_graph', {'source':self.source, **options}))

    def rebind(self, node_id, **options):
        self._ensure_attached()
        return require(self.gui.session.request('rebind',{'source':self.source,'node_id':node_id,**options}))

    def window_action(self, action):
        self._ensure_attached()
        self._controls=None
        return require(self.gui.session.request('window_action',{'source':self.source,'action':action}))

    def detach(self):
        if self.detached: return None
        result = require(self.gui.session.request('detach', {'source': self.source}))
        self.detached = True
        return result

    def __enter__(self): return self
    def __exit__(self, *_): self.detach()


class GUI:
    def __init__(self, session=None):
        self.session = session if session is not None else GuiSession()
        self.closed, self.cleanup = False, None

    def apps(self): return require(self.session.request('apps'))['windows']

    def targets(self, endpoint):
        return require(self.session.request('targets', {'endpoint':endpoint}))['targets']

    def window(self, *, window=None, app=None, title=None, runtime='auto', endpoint=None, target=None, api_module=None, backend=None):
        if self.closed:
            raise GuiError({'error': {'code': 'session_closed', 'message': 'Create a new GUI session'}})
        if runtime == 'cdp-dom' and any(v is not None for v in (window, app, title)):
            raise ValueError('A CDP page is selected by endpoint and target, not a window selector')
        if window is not None and (app is not None or title is not None):
            raise ValueError('Use a window handle or app/title selectors, not both')
        if runtime != 'cdp-dom' and window is None:
            if app is None and title is None:
                raise ValueError('Select a window handle, app or exact title')
            matches = [w for w in self.apps() if
                (app is None or w['app'].casefold() == app.casefold()) and (title is None or w['title'] == title)]
            if len(matches) != 1:
                raise GuiError({'error': {'code': 'ambiguous_window' if matches else 'window_not_found',
                    'message': f'Expected one window; got {len(matches)}. Use an exact title or HWND.'}})
            window = matches[0]['window']
        result = require(self.session.request('attach', {k: v for k, v in {
            'window': window, 'runtime': runtime, 'endpoint': endpoint, 'target': target,
            'api_module': api_module, 'backend':backend}.items() if v is not None}))
        return Window(self, result)

    def close(self):
        if not self.closed:
            self.cleanup = self.session.close()
            self.closed = True
        return self.cleanup

    def __enter__(self): return self
    def __exit__(self, *_): self.close()
