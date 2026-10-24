import json
import queue
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import appctl


def test_schema_disables_implicit_vision():
    result = appctl.schema()
    assert result["screenshots"] is False
    assert result["automatic_visual_fallback"] is False
    assert "input" in result["commands"]


def test_window_queries_are_read_only_by_default_and_raw_view_is_explicit():
    parsed = appctl.parser().parse_args(["window", "--window", "123"])
    assert parsed.state == "get"
    parsed = appctl.parser().parse_args(["inspect", "--window", "123", "--view", "raw"])
    assert parsed.view == "raw"


def test_set_accepts_explicit_empty_string_and_scoped_target():
    parsed = appctl.parser().parse_args([
        "set", "--window", "123", "--within", "SearchBox", "--automation-id", "TextBox", "--value", ""
    ])
    assert parsed.value == ""
    assert parsed.within == "SearchBox"
    assert parsed.automation_id == "TextBox"


def test_rejects_unscoped_and_unknown_input_commands():
    with pytest.raises(ValueError):
        appctl.input_command({"action": "keys", "window": True, "keys": "a"})
    with pytest.raises(ValueError):
        appctl.input_command({"action": "shell", "window": 123})
    with pytest.raises(ValueError):
        appctl.input_command({"action": "click", "window": 123})


def test_literal_input_does_not_interpret_enter_as_a_key():
    command = appctl.input_command({"action": "type", "window": 123, "text": "enter"})
    assert command[:2] == ["send-keys", "enter"]
    assert "--verbatim" in command
    assert command[command.index("--via") + 1] == "send-input"


def test_dispatch_does_not_turn_unsupported_uia_into_input():
    session = SimpleNamespace(request=Mock(return_value=appctl.error("unsupported", "No provider")))
    result = appctl.dispatch(session, {"id": "r1", "op": "invoke", "args": {"ref": "x"}})
    assert result["error"]["code"] == "unsupported"
    session.request.assert_called_once_with("invoke", {"ref": "x"}, "r1")


def test_bad_request_is_rejected_without_touching_backend():
    session = SimpleNamespace(request=Mock())
    result = appctl.dispatch(session, {"op": "set", "args": []})
    assert result["error"]["code"] == "invalid_request"
    session.request.assert_not_called()


@pytest.mark.parametrize("op,code", [("get", "timeout"), ("invoke", "outcome_unknown")])
def test_timeout_closes_worker_without_retrying(op, code):
    session = appctl.UiaSession.__new__(appctl.UiaSession)
    session.lock = __import__("threading").Lock()
    session.sequence = 0
    session.timeout = 0.001
    session.output = queue.Queue()
    session.errors = []
    session.process = Mock()
    session.process.poll.return_value = None
    session.close = Mock()
    result = session.request(op, {"ref": "e1"}, "caller")
    assert result["error"]["code"] == code
    assert result["id"] == "caller"
    session.process.stdin.write.assert_called_once()
    session.close.assert_called_once()


def test_response_ids_and_transport_timing_are_preserved():
    session = appctl.UiaSession.__new__(appctl.UiaSession)
    session.lock = __import__("threading").Lock()
    session.sequence = 0
    session.timeout = 0.1
    session.errors = []
    session.process = Mock()
    session.process.poll.return_value = None
    session.output = queue.Queue()
    session.output.put(json.dumps({"id": 1, "ok": True, "data": {"value": ""}}))
    result = session.request("get", {"ref": "e1"}, "caller")
    assert result["id"] == "caller"
    assert result["data"]["value"] == ""
    assert result["roundtrip_ms"] >= 0


@pytest.mark.parametrize("op,expected", [("get", "worker_stopped"), ("invoke", "outcome_unknown")])
def test_worker_exit_after_request_does_not_claim_a_failed_action_is_safe_to_retry(op, expected):
    session = appctl.UiaSession.__new__(appctl.UiaSession)
    session.lock = __import__("threading").Lock()
    session.sequence = 0
    session.timeout = .1
    session.errors = []
    session.process = Mock()
    session.process.poll.return_value = None
    session.output = queue.Queue()
    session.output.put(None)
    result = session.request(op, {"ref": "e1"})
    assert result["error"]["code"] == expected
    session.process.stdin.write.assert_called_once()
