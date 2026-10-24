"""Validate actual native observation snapshots through MCP; no process attach."""
import asyncio
import json
import os
from pathlib import Path
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main():
    root = Path(__file__).resolve().parents[1]
    params = StdioServerParameters(command=sys.executable, args=[str(root / 'appctl_mcp.py')], cwd=str(root),
                                  env={**os.environ, 'APPCTL_OBJECT_CONFIG': str(root / 'objects.observations.json'), 'PYTHONUTF8': '1'})
    observations = []
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            for target, query in (('wechat-observed', 'chat'), ('wps-observed', 'cache'), ('qq-observed', 'service'), ('cloudmusic-observed', 'player')):
                print(json.dumps({'phase': 'exploring', 'target': target}), flush=True)
                reply = await session.call_tool('application_object_explore', {'target': target, 'query': query, 'max_objects': 256, 'max_depth': 8, 'max_requests': 1024, 'max_seconds': 25})
                result = reply.structuredContent or json.loads(reply.content[0].text)
                assert result['ok'], result
                data = result['data']
                assert data['observation_only'] and data['nodes'] and data['matches']
                assert data['statistics']['property_reads'] == data['statistics']['declared_read_calls'] == 0
                assert all(n.get('capabilities', {}).get('native_call') is False for n in data['nodes'])
                observations.append({'target': target, 'objects': len(data['nodes']), 'matches': len(data['matches']), 'source_limits_preserved': data['stopped_by']})
    report = {'ok': True, 'transport': 'MCP stdio', 'observations': observations, 'native_calls': 0, 'property_values_read': 0}
    (root / 'reports/business-objects-20261023/mcp-validation.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    print(json.dumps({'phase': 'starting', 'test_pid': os.getpid()}), flush=True)
    asyncio.run(asyncio.wait_for(main(), 50))
