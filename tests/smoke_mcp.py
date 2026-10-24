"""Check actual MCP initialization and tool calls; no GUI input."""
import asyncio
import json
from pathlib import Path
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main():
    root = Path(__file__).resolve().parents[1]
    params = StdioServerParameters(command=sys.executable, args=[str(root / "appctl_mcp.py")], cwd=str(root))
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = (await session.list_tools()).tools
            names = {t.name for t in tools}
            assert {"desktop_apps", "desktop_inspect", "desktop_act", "desktop_events"} <= names
            result = await session.call_tool("desktop_capabilities", {})
            data = result.structuredContent or json.loads(result.content[0].text)
            assert data["screenshots"] is False
            apps = await session.call_tool("desktop_apps", {})
            data = apps.structuredContent or json.loads(apps.content[0].text)
            assert data["ok"] and isinstance(data["data"]["windows"], list)
            print(json.dumps({"ok": True, "typed_tools": len(tools), "window_count": len(data["data"]["windows"]), "input_commands": 0}))


if __name__ == "__main__":
    asyncio.run(main())
