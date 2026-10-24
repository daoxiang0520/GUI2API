"""End-to-end MCP discovery tools against owned objects, with exact bindings."""
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main():
    root=Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix='discovery-mcp-') as scratch:
        params=StdioServerParameters(command=sys.executable,args=[str(root/'appctl_mcp.py')],cwd=str(root),
            env={**os.environ,'APPCTL_OBJECT_CONFIG':str(root/'objects.example.json'),
                 'GUI2API_DISCOVERY_CACHE':str(Path(scratch)/'cache.json'),'PYTHONUTF8':'1'})
        async with stdio_client(params) as (read,write):
            async with ClientSession(read,write) as session:
                await session.initialize()
                names={t.name for t in (await session.list_tools()).tools}
                assert {'gui_discover','gui_discovery_describe','gui_discovery_invoke','gui_discovery_events','gui_learning_graph'}<=names
                async def tool(name,args):
                    result=await session.call_tool(name,args)
                    return result.structuredContent or json.loads(result.content[0].text)
                missing=await tool('gui_learning_graph',{'source':'not-attached'})
                assert missing['error']['code']=='unknown_source',missing
                catalog=await tool('gui_discover',{'object_target':'javascript-demo'})
                assert catalog['ok'],catalog
                entries=catalog['data']['capabilities']
                add=next(c for c in entries if c['name']=='add' and c['operation']=='call')
                description=await tool('gui_discovery_describe',{'capability':add['id']})
                assert not description['data']['business_meaning_verified']
                result=await tool('gui_discovery_invoke',{'capability':add['id'],'arguments':{'arguments':[5]},
                    'postcondition':{'member':'count','equals':5}})
                assert result['ok'] and result['verification']['status']=='postcondition_passed',result
                child=next(c for c in entries if c['name']=='child' and c['operation']=='call')
                returned=await tool('gui_discovery_invoke',{'capability':child['id']})
                assert returned['ok'] and returned['returned_object_discovery']['added_capabilities']>0,returned
                child_catalog=await tool('gui_discover',{'object_target':'javascript-demo','query':'Child echo'})
                assert len(child_catalog['data']['capabilities'])==1,child_catalog
                echoed=await tool('gui_discovery_invoke',{'capability':child_catalog['data']['capabilities'][0]['id'],'arguments':{'arguments':['MCP child']}})
                assert echoed['data']['result']['value']=='MCP child',echoed
                refreshed=await tool('gui_discover',{'object_target':'javascript-demo','refresh':True})
                assert refreshed['ok'] and refreshed['data']['catalog']!=catalog['data']['catalog']
                rejected=await tool('gui_discovery_invoke',{'capability':add['id'],'arguments':{'arguments':[100]}})
                assert rejected['error']['code']=='stale_capability'
                print(json.dumps({'ok':True,'typed_tools':len(names),'actual_mcp_transport':True,
                    'discovery_invoke_postcondition':True,'returned_object_auto_discovery':True,'stale_capability_rejected':True,'screenshots':0,'messages_sent':0}))


if __name__=='__main__':asyncio.run(asyncio.wait_for(main(),timeout=35))
