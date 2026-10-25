from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from appctl import error
from desktop import DesktopSession, deduplicate
from cdp_backend import CdpBackend, DriverFault, loopback_url


def row(ref, name="按钮", actions=None, **fields):
    return {"ref": ref, "parent": None, "role": "Button", "name": name, "enabled": True, "offscreen": False, "actions": actions or ["get", "invoke"], **fields}


class FakeNative:
    def __init__(self, backend):
        self.backend = backend
        self.timeout = 20
        self.process = SimpleNamespace(poll=lambda: None)
        self.calls = []
        self.failed = False

    def request(self, op, args=None):
        self.calls.append((op, args))
        if op == "describe":
            return {"ok": True, "data": {"window": 10, "pid": 5, "listeners": []}}
        if op == "window":
            return {"ok": True, "data": {"window": 10, "pid": 5}}
        if op == "inspect":
            if self.failed:
                return error("timeout", "blocked provider")
            return {"ok": True, "data": {"elements": [row(self.backend, self.backend)]}}
        if op == "invoke":
            return error("outcome_unknown", "provider stopped after invocation")
        return {"ok": True, "data": {}}

    def close(self):
        self.process = SimpleNamespace(poll=lambda: 1)


@pytest.fixture
def session():
    value = DesktopSession(native_factory=lambda backend: FakeNative(backend), cdp_factory=lambda: SimpleNamespace(close=lambda: None))
    yield value
    value.close()


def test_unresponsive_channel_does_not_hide_other_channels_or_get_retried(session):
    session._native("uia3").failed = True
    result = session.request("inspect", {"window": 10, "actionable": False})
    assert result["ok"]
    assert {r["backend"] for r in result["data"]["elements"]} == {"msaa", "win32"}
    session.request("inspect", {"window": 10, "actionable": False})
    assert [c[0] for c in session.workers["uia3"][0].calls].count("inspect") == 1


def test_mutation_stays_on_the_original_backend_and_is_never_replayed(session):
    result = session.request("inspect", {"window": 10, "actionable": False})
    target = next(r for r in result["data"]["elements"] if r["backend"] == "msaa")
    result = session.request("invoke", {"ref": target["ref"], "backend": "uia"})
    assert result["error"]["code"] == "outcome_unknown"
    assert len([c for c in session.workers["msaa"][0].calls if c[0] == "invoke"]) == 1
    assert not any(c[0] == "invoke" for c in session.workers["uia3"][0].calls)


def test_input_context_guards_check_live_names_even_when_object_identity_is_retained(session):
    observed=session.request('inspect',{'window':10,'backend':'msaa','actionable':False})
    target=observed['data']['elements'][0]['ref']
    from gui_input import NativeInput
    api=Mock();api.modifiers_down.return_value=False
    session.input_driver=NativeInput(api)
    for worker,_ in session.workers.values():
        original=worker.request
        worker.request=lambda op,args=None,original=original: {'ok':True,'data':{'desktop':{'interactive_confirmed':True}}} if op=='ping' else {'ok':True,'data':{'name':'different conversation','role':'Button'}} if op=='input_target' else original(op,args)
    result=session.request('input',{'ref':target,'method':'press_keys','arguments':{'keys':'ENTER'},'guard_refs':[target]})
    assert result['error']['code']=='context_changed'
    api.send.assert_not_called();api.activate.assert_not_called()


def test_replaced_native_worker_invalidates_its_old_references(session):
    result = session.request("inspect", {"window": 10, "backend": "msaa", "actionable": False})
    ref = result["data"]["elements"][0]["ref"]
    original = session.workers["msaa"][0]
    original.close()
    result = session.request("invoke", {"ref": ref})
    assert result["error"]["code"] == "stale_ref"
    assert session.workers["msaa"][0] is original
    assert not any(c[0] == "invoke" for c in original.calls)


@pytest.mark.parametrize('backend',['uia3','msaa','win32'])
def test_bound_observation_reads_one_original_object_without_other_channels(session,backend):
    observed=session.request('inspect',{'window':10,'backend':backend})
    ref=observed['data']['elements'][0]['ref']
    worker=session.workers[backend][0]
    before={name:len(entry[0].calls) for name,entry in session.workers.items()}
    result=session.request('observe',{'ref':ref})
    assert result['ok'] and result['data']['element']['ref']==ref
    assert result['data']['fresh'] and result['data']['scope']=='bound_control'
    operation,args=worker.calls[-1]
    assert operation=='inspect' and args['depth']==0 and args['limit']==1 and args['root_ref']==backend
    assert all(len(entry[0].calls)==before[name] for name,entry in session.workers.items() if name!=backend)


def test_bound_observation_rejects_worker_replacement_and_different_recipient(session):
    observed=session.request('inspect',{'window':10,'backend':'msaa'})
    ref=observed['data']['elements'][0]['ref']
    worker=session.workers['msaa'][0]
    worker.request=lambda op,args=None:{'ok':True,'data':{'elements':[row('different-object')]}}
    assert session.request('observe',{'ref':ref})['error']['code']=='stale_ref'
    worker.close()
    assert session.request('observe',{'ref':ref})['error']['code']=='stale_ref'
    assert session.workers['msaa'][0] is worker


def test_missing_channel_is_not_reported_as_controls_removed(session):
    args = {"window": 10, "actionable": False}
    snapshot = session.request("inspect", args)["data"]["snapshot"]
    session.workers["uia3"][0].failed = True
    result = session.request("inspect", {**args, "since": snapshot})
    assert result["error"]["code"] == "incomparable_snapshot"
    assert "removed" not in result.get("data", {})


def test_ambiguous_cross_channel_name_does_not_invoke_any_target(session):
    for backend in ("uia3", "msaa", "win32"):
        worker = session._native(backend)
        original = worker.request
        worker.request = lambda op, args=None, original=original: {"ok": True, "data": {"elements": [row("r", "相同名字")]}} if op == "inspect" else original(op, args)
    result = session.request("invoke", {"window": 10, "name": "相同名字"})
    assert result["error"]["code"] == "ambiguous_target"
    assert not any(op == "invoke" for worker, _ in session.workers.values() for op, args in worker.calls)


def test_dedup_does_not_invent_a_combined_action_target():
    rect = {"x": 1, "y": 2, "width": 20, "height": 30}
    a = row("a", actions=["get", "invoke"], backend="uia", rect=rect)
    b = row("b", actions=["get", "set"], backend="msaa", rect=rect)
    result, count = deduplicate([a, b])
    assert count == 0 and result == [a, b]
    c = row("c", actions=["get", "invoke", "focus"], backend="msaa", rect=rect)
    result, count = deduplicate([a, c])
    assert count == 1 and result == [c]


@pytest.mark.parametrize("url", ["http://example.com:9222", "http://127.0.0.1", "http://user:secret@127.0.0.1:9222", "file:///tmp/debug", "http://0.0.0.0:9222"])
def test_cdp_endpoint_cannot_escape_to_remote_services(url):
    with pytest.raises(DriverFault) as caught:
        loopback_url(url)
    assert caught.value.code == "invalid_endpoint"


def test_cdp_explicit_loopback_supports_ipv4_and_ipv6():
    assert loopback_url("http://127.0.0.1:9222")
    assert loopback_url("http://[::1]:9222")


def test_cdp_reconnect_rejects_old_backend_node_even_if_numeric_id_is_reused():
    driver = CdpBackend()
    conn = SimpleNamespace(instance="new-instance", epoch=0, drain=Mock(), rpc=Mock())
    driver._connection = lambda args: (conn, ("http://127.0.0.1:9222", "page"))
    driver.refs["old-ref"] = (("http://127.0.0.1:9222", "page"), "old-instance", 0, 5)
    result = driver.request("invoke", {"ref": "old-ref"})
    assert result["error"]["code"] == "stale_ref"
    conn.rpc.assert_not_called()


def test_cdp_user_text_is_passed_as_data_and_never_interpolated_into_javascript():
    from cdp_backend import DOM_ACTION
    driver = CdpBackend()
    conn = SimpleNamespace(instance="instance", epoch=0, drain=Mock(), rpc=Mock(side_effect=[{"object": {"objectId": "object"}}, {"result": {"value": {"status": "verified"}}}]))
    driver._connection = lambda args: (conn, ("http://127.0.0.1:9222", "page"))
    driver.refs["ref"] = (("http://127.0.0.1:9222", "page"), "instance", 0, 5)
    literal = 'quoted " text with ${literal} and a newline\n'
    result = driver.request("set", {"ref": "ref", "value": literal})
    assert result["ok"]
    payload = conn.rpc.call_args_list[1].args[1]
    assert payload["functionDeclaration"] == DOM_ACTION
    assert payload["arguments"][1]["value"] == literal
    assert literal not in payload["functionDeclaration"]


def test_subtree_reference_pins_the_read_to_its_original_channel(session):
    observed=session.request("inspect",{"window":10,"backend":"msaa","actionable":False})
    ref=observed["data"]["elements"][0]["ref"]
    result=session.request("inspect",{"root_ref":ref,"backend":"uia3","actionable":False})
    assert result["ok"]
    assert {r["backend"] for r in result["data"]["elements"]}=={"msaa"}
    assert session.workers["msaa"][0].calls[-1][1]["root_ref"]=="msaa"
    assert "uia3" not in session.workers


def test_partial_tree_cannot_report_controls_as_removed(session):
    worker=session._native("msaa")
    original=worker.request
    def request(op,args=None):
        result=original(op,args)
        if op=="inspect":result["data"]["truncated"]=True
        return result
    worker.request=request
    args={"window":10,"backend":"msaa","actionable":False}
    snapshot=session.request("inspect",args)["data"]["snapshot"]
    result=session.request("inspect",{**args,"since":snapshot})
    assert result["error"]["code"]=="incomparable_snapshot"


def test_lazy_accessibility_bridge_gets_only_one_read_only_warmup(session):
    worker=session._native("uia3")
    original=worker.request
    count=0
    def request(op,args=None):
        nonlocal count
        if op=="inspect":
            count+=1
            if count==1:return {"ok":True,"data":{"elements":[row("cold","window",actions=["get"])]}}
            return {"ok":True,"data":{"elements":[row("warm","搜索",actions=["get","set"]),row("button")]}}
        return original(op,args)
    worker.request=request
    result=session.request("inspect",{"window":10,"actionable":False})
    assert result["ok"]
    assert any(r["name"]=="搜索" and "set" in r["actions"] for r in result["data"]["elements"])
    assert count==2
    assert any(c.get("phase")=="read_only_warmup" for c in result["data"]["channels"])


def test_cdp_absence_reports_what_was_checked_without_claiming_support_is_disabled(session):
    result, diagnostic = session._discover_cdp({'pid':5, 'listeners':[]})
    assert result is None
    assert diagnostic['error']['code'] == 'cdp_not_discovered'
    assert diagnostic['reason'] == 'no_listening_sockets'
    assert diagnostic['scope'] == 'selected_window_pid_listeners_only'
    assert diagnostic['other_processes_checked'] is False


@pytest.mark.parametrize('response,reason', [('failure','no_valid_cdp_response'), ([], 'no_page_targets')])
def test_cdp_diagnostics_distinguish_invalid_endpoints_and_empty_targets(session, monkeypatch, response, reason):
    def discover(endpoint, timeout):
        if response == 'failure':
            raise DriverFault('not_cdp', 'untrusted response content must not be logged')
        return response
    monkeypatch.setattr('desktop.targets', discover)
    result, diagnostic = session._discover_cdp({'listeners':[{'port':1234}]})
    assert result is None and diagnostic['reason'] == reason
    assert len(diagnostic['attempts']) == 1
    assert 'untrusted response content' not in str(diagnostic)


def test_cdp_multiple_endpoints_are_ambiguous_instead_of_not_exposed(session, monkeypatch):
    monkeypatch.setattr('desktop.targets', lambda endpoint, timeout:[{'id':'one'}])
    result, diagnostic = session._discover_cdp({'listeners':[{'port':1234}, {'port':1235}]})
    assert result is None and diagnostic['error']['code'] == 'ambiguous_target'
    assert diagnostic['candidate_count'] == 2


def test_cdp_probe_budget_cannot_turn_an_incomplete_search_into_unique_target(session, monkeypatch):
    def discover(endpoint, timeout):
        assert 0 < timeout <= 1
        return [{'id':'one'}] if endpoint.endswith(':1000') else []
    monkeypatch.setattr('desktop.targets', discover)
    result, diagnostic = session._discover_cdp({'listeners':[{'port':p} for p in range(1000,1009)]})
    assert result is None and diagnostic['error']['code'] == 'cdp_probe_incomplete'
    assert diagnostic['candidate_count'] == 1
    assert len(diagnostic['attempts']) == 8 and diagnostic['budget']['stopped']


def test_cdp_duplicate_ports_do_not_inflate_candidates(session, monkeypatch):
    discover = Mock(return_value=[{'id':'one'}])
    monkeypatch.setattr('desktop.targets', discover)
    result, diagnostic = session._discover_cdp({'listeners':[{'port':1234}, {'port':1234}]})
    assert diagnostic is None and result['target'] == 'one'
    assert discover.call_count == 1


@pytest.mark.parametrize('payload', [{}, [None], ['wrong'], [{'type':'page','webSocketDebuggerUrl':'ws://127.0.0.1:1/x'}]])
def test_cdp_target_list_validates_shape_before_binding(monkeypatch, payload):
    from cdp_backend import targets
    monkeypatch.setattr('cdp_backend.fetch_json', lambda *args, **kwargs:payload)
    with pytest.raises(DriverFault) as caught:
        targets('http://127.0.0.1:1234')
    assert caught.value.code == 'not_cdp'
