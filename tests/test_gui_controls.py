"""Control-object contracts that matter independently of individual UI providers."""
import pytest

from gui_controls import GUI, GuiError


class Session:
    def __init__(self): self.calls = []; self.failure = None
    def request(self, op, args=None):
        self.calls.append((op, args))
        if op == 'apps':
            data = {'windows': [{'window': 1, 'app': 'example', 'title': 'One'},
                                {'window': 2, 'app': 'example', 'title': 'Two'}]}
        elif op == 'attach': data = {'source': 'source'}
        elif op in ('scan','find'):
            data = {'snapshot': 'snapshot', 'elements': [
                {'ref':'source/root','parent':None,'name':'Panel','role':'Pane','methods':['get']},
                {'ref': 'source/button', 'parent':'source/root','name': 'Press',
                'role': 'Button', 'methods': ['invoke']}, {'ref': 'source/text', 'parent':'source/root','name': 'Entry',
                'role': 'Edit', 'methods': ['get', 'set']}], 'cursor': None, 'truncated': False}
            if op=='find':
                from gui_api import filter_controls
                data['elements']=filter_controls(data['elements'],args)
        elif op == 'call':
            if self.failure: return {'ok': False, 'error': self.failure}
            data = {'status': 'executed_unverified'}
        else: data = {}
        return {'ok': True, 'data': data}
    def close(self): return []


def test_click_returns_without_business_registration_or_followup_state_reads():
    session = Session()
    with GUI(session) as gui:
        window = gui.window(window=1)
        button = window.one(role='Button')
        before = len(session.calls)
        assert button.click()['status'] == 'executed_unverified'
        assert session.calls[before:] == [('call', {'ref': 'source/button', 'method': 'invoke', 'arguments': {}})]


def test_text_assignment_has_no_required_expected_value_or_feature_profile():
    session = Session()
    with GUI(session) as gui:
        textbox = gui.window(window=1).one(role='Edit')
        textbox.text = 'Literal ${code} 中文'
        assert session.calls[-1] == ('call', {'ref': 'source/text', 'method': 'set',
                                              'arguments': {'value': 'Literal ${code} 中文'}})


def test_selectors_reuse_discovery_and_writes_refresh_the_next_lookup():
    session = Session()
    with GUI(session) as gui:
        window = gui.window(window=1)
        window.one(role='Button').click()
        window.one(role='Edit').set_text('text')
        assert [op for op, _ in session.calls].count('find') == 2
        window.refresh()
        assert [op for op, _ in session.calls].count('scan') == 1


def test_ambiguous_window_or_control_does_not_pick_an_arbitrary_target():
    session = Session()
    with GUI(session) as gui:
        with pytest.raises(GuiError, match='Expected one window'):
            gui.window(app='example')
        assert not any(op == 'attach' for op, _ in session.calls)
        window = gui.window(app='EXAMPLE', title='One')
        with pytest.raises(GuiError, match='Expected one control'):
            window.one()
        assert not any(op == 'call' for op, _ in session.calls)


def test_uncertain_action_is_not_retried_and_detached_controls_stop_locally():
    session = Session()
    with GUI(session) as gui:
        window = gui.window(window=1)
        button = window.one(role='Button')
        session.failure = {'code': 'outcome_unknown', 'message': 'Connection lost'}
        with pytest.raises(GuiError) as error: button.click()
        assert error.value.code == 'outcome_unknown'
        assert [op for op, _ in session.calls].count('call') == 1
        window.detach()
        with pytest.raises(GuiError) as error: button.click()
        assert error.value.code == 'source_closed'
        assert [op for op, _ in session.calls].count('call') == 1


def test_control_queries_share_case_insensitive_alias_and_subtree_semantics():
    with GUI(Session()) as gui:
        window = gui.window(window=1)
        root = window.one(role='pane')
        assert len(root.children()) == 2 and len(root.descendants()) == 2
        assert window.one(name='press',method='click').role == 'Button'
        assert window.one(query='entry',within_ref=root.id).role == 'Edit'
        assert window.one(name='entr',contains=True).role == 'Edit'
