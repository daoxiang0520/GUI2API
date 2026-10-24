"""Validate the public CLI's persistent JSON-lines protocol; performs no UI actions."""
import json
from pathlib import Path
import subprocess
import sys

root = Path(__file__).resolve().parents[1]
lines = [
    json.dumps({"id": "first", "op": "ping"}),
    "not valid json",
    json.dumps({"id": "second", "op": "ping"}),
    json.dumps({"id": "schema", "op": "schema"}),
    json.dumps({"id": "unknown", "op": "nonexistent"}),
]
process = subprocess.run(
    [sys.executable, str(root / "appctl.py"), "session"],
    input="\n".join(lines) + "\n", capture_output=True,
    text=True, encoding="utf-8", errors="replace", timeout=30,
)
assert process.returncode == 0, process.stderr + process.stdout
replies = [json.loads(line) for line in process.stdout.splitlines()]
assert len(replies) == len(lines), replies
assert replies[0]["id"] == "first" and replies[0]["ok"]
assert replies[1]["error"]["code"] == "invalid_json"
assert replies[2]["id"] == "second" and replies[2]["ok"]
assert replies[0]["data"]["session"] == replies[2]["data"]["session"]
assert replies[3]["data"]["screenshots"] is False
assert replies[4]["error"]["code"] == "unknown_command"
print(json.dumps({"ok": True, "replies": len(replies), "same_worker_reused": True, "invalid_request_recovered": True, "ui_actions": 0}))
