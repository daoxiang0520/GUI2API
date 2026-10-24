"""Exercise object tools through an actual MCP stdio client; no GUI worker."""
import asyncio
import json
import os
from pathlib import Path
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main():
    root = Path(__file__).resolve().parents[1]
    params = StdioServerParameters(command=sys.executable, args=[str(root / "appctl_mcp.py")], cwd=str(root), env={**os.environ, "APPCTL_OBJECT_CONFIG": str(root / "objects.example.json"), "PYTHONUTF8": "1"})
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            names = {t.name for t in (await session.list_tools()).tools}
            assert {"application_objects", "application_object_explore", "application_object_call", "application_object_events", "desktop_apps"} <= names

            async def tool(name, args):
                result = await session.call_tool(name, args)
                data = result.structuredContent or json.loads(result.content[0].text)
                assert data.get("ok"), data
                return data["data"]

            targets = await tool("application_object_targets", {})
            assert len(targets["targets"]) == 5
            roots = await tool("application_objects", {"target": "python-demo"})
            ref = roots["objects"][0]["ref"]
            await tool("application_object_describe", {"ref": ref})
            graph = await tool("application_object_explore", {"ref": ref, "query": "echo"})
            assert graph["matches"] and graph["statistics"]["objects"] == 1
            assert graph["statistics"]["property_reads"] == graph["statistics"]["declared_read_calls"] == 0
            await tool("application_object_watch", {"ref": ref, "names": ["changed"]})
            result = await tool("application_object_call", {"ref": ref, "member": "add", "arguments": [7]})
            assert result["result"]["value"] == 7
            result = await tool("application_object_read", {"ref": ref, "member": "count"})
            assert result["result"]["value"] == 7
            events = await tool("application_object_events", {"target": "python-demo"})
            assert len(events["events"]) == 1
            result = await tool("application_object_set", {"ref": ref, "member": "label", "value": None})
            assert result["status"] == "verified" and result["result"]["value"] is None
            windows = await tool("desktop_apps", {})
            assert isinstance(windows["windows"], list)
            report = {"ok": True, "typed_tools": len(names), "object_exploration": True, "object_call_readback": True, "object_event": True, "explicit_null_set": True, "desktop_api_also_works": True, "desktop_inputs": 0}
            (root / "object-mcp-validation.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
            print(json.dumps(report))


if __name__ == "__main__":
    asyncio.run(asyncio.wait_for(main(), timeout=35))
