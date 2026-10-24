"""Verify the registry with owned fixtures, CLI/MCP and a read-only Shell API."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

from capability_registry import CapabilityRegistry, onboard

ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "reports/capabilities-20261023"


def ok(result):
    assert result.get("ok"), result
    return result["data"]


async def mcp_check(registry):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    env = {**os.environ, "APPCTL_OBJECT_CONFIG": str(ROOT / "objects.example.json"),
           "APPCTL_CAPABILITY_REGISTRY": str(registry), "PYTHONUTF8": "1"}
    params = StdioServerParameters(command=sys.executable, args=[str(ROOT / "appctl_mcp.py")], env=env, cwd=str(ROOT))
    async with stdio_client(params) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            names = {t.name for t in (await session.list_tools()).tools}
            assert {"application_capabilities", "application_capability_describe", "application_capability_invoke"} <= names

            async def call(name, arguments):
                result = await session.call_tool(name, arguments)
                return json.loads(next(c.text for c in result.content if c.type == "text"))

            listing = ok(await call("application_capabilities", {"app_id": "counter-demo"}))
            assert len(listing["capabilities"]) == 2 and all(c["callable"] for c in listing["capabilities"])
            value = ok(await call("application_capability_invoke", {"app_id": "counter-demo", "capability_id": "counter.increment", "arguments": {"amount": 4}}))
            assert value["result"] == 4 and value["status"] == "postconditions_verified"
            assert ok(await call("application_capability_invoke", {"app_id": "counter-demo", "capability_id": "counter.read"}))["result"] == 4
            candidate = await call("application_capability_invoke", {"app_id": "counter-auto", "capability_id": "get.count"})
            assert candidate["error"]["code"] == "candidate_only"
            return {"ok": True, "tools": len(names), "named_call_readback": True, "candidate_rejected": True}


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    OUTPUT.mkdir(parents=True, exist_ok=True)
    run = OUTPUT / ("run-" + uuid.uuid4().hex[:8])
    run.mkdir()
    registry_path = run / "registry.json"
    report = {"screenshots": 0, "messages_sent": 0, "personal_documents_modified": 0,
              "registry": str(registry_path), "semantic_truth_proven": False}
    try:
        draft = run / "counter-auto.json"
        report["onboarding"] = onboard(ROOT / "objects.example.json", "python-demo", "counter-auto", draft, "counter")
        with CapabilityRegistry(registry_path) as registry:
            for manifest in (ROOT / "capabilities/counter-demo.json", ROOT / "capabilities/windows-shell.json", draft):
                ok(registry.request("register", {"path": str(manifest)}))
            assert all(not c["callable"] for c in registry.list()["capabilities"])
            for app, capability, write in (("counter-demo", "counter.read", False), ("counter-demo", "counter.increment", True), ("windows-shell", "drives.is-folder", False)):
                ok(registry.request("validate", {"app_id": app, "capability_id": capability, "allow_write": write}))
            assert ok(registry.request("invoke", {"app_id": "counter-demo", "capability_id": "counter.increment", "arguments": {"amount": 3}}))["result"] == 5
            assert ok(registry.request("invoke", {"app_id": "windows-shell", "capability_id": "drives.is-folder", "arguments": {}}))["result"] is True
            report.update(named_object_binding=True, postcondition_state_readback=True, actual_shell_object_chain=True)
            registered = registry._read()
        wire = "\n".join([json.dumps({"id": 1, "op": "list", "args": {"app_id": "windows-shell"}}),
                           json.dumps({"id": 2, "op": "invoke", "args": {"app_id": "counter-demo", "capability_id": "counter.increment", "arguments": {"amount": 5}}})]) + "\n"
        cli = subprocess.run([sys.executable, str(ROOT / "appctl.py"), "capabilities", "--registry", str(registry_path), "session"],
                             input=wire, capture_output=True, encoding="utf-8", timeout=20,
                             env={**os.environ, "PYTHONUTF8": "1"}, creationflags=subprocess.CREATE_NO_WINDOW)
        replies = [json.loads(line) for line in cli.stdout.splitlines()]
        assert cli.returncode == 0 and len(replies) == 2, (cli.stdout, cli.stderr)
        assert replies[0]["id"] == 1 and replies[1]["id"] == 2 and ok(replies[1])["result"] == 5
        report["cli_json_lines"] = True
        report["mcp"] = asyncio.run(mcp_check(registry_path))
        # Publish only the two explicitly tested examples in the workspace. Never
        # replace a pre-existing registration, and do not publish inferred drafts.
        with CapabilityRegistry() as local:
            with local._edit() as data:
                added = []
                for name in ("counter-demo", "windows-shell"):
                    if name not in data["apps"]:
                        data["apps"][name] = registered["apps"][name]
                        added.append(name)
            report["workspace_registrations_added"] = added
        report["ok"] = True
    finally:
        (OUTPUT / "validation.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
