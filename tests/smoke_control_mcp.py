"""Invoke ordinary controls through the actual MCP server on an owned Qt app."""
import asyncio
import json
from pathlib import Path
import sys
import time
import traceback
import uuid

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from probe_universality import launch_owned,close_owned


async def main():
    run=ROOT/'tests/runs'/('control-mcp-'+uuid.uuid4().hex[:8]);run.mkdir(parents=True)
    state=run/'state.json'
    owned=launch_owned([sys.executable,'-X','utf8',str(ROOT/'tests/fixtures/qt_gui_controls.py'),str(state)])
    report={'ok':False,'screenshots':0,'feature_manifests':0,'scope':'owned Qt app via actual MCP stdio server'}
    try:
        deadline=time.monotonic()+12
        while not state.exists() and time.monotonic()<deadline and owned.poll() is None:await asyncio.sleep(.05)
        def actual():return json.loads(state.read_text(encoding='utf-8'))
        params=StdioServerParameters(command=sys.executable,args=['-X','utf8',str(ROOT/'appctl_mcp.py')],cwd=str(ROOT))
        async with stdio_client(params) as (read,write):
            async with ClientSession(read,write) as client:
                await client.initialize()
                async def call(tool_name,**arguments):
                    response=await client.call_tool(tool_name,arguments)
                    result=response.structuredContent or json.loads(response.content[0].text)
                    assert result['ok'],result
                    return result['data']
                source=(await call('gui_attach',window=actual()['window']))['source']
                await call('gui_scan',source=source,limit=1)
                cache_results=[];readonly_cache_results=[]
                async def ref(name,method):
                    data=await call('gui_find',source=source,name=name,method=method,backend='uia3')
                    assert data['matched']==1,name
                    cache_results.append(data['cached'])
                    again=await call('gui_find',source=source,name=name,method=method,backend='uia3')
                    readonly_cache_results.append(again['cached'])
                    return data['elements'][0]['ref']
                clicked=await call('gui_invoke',ref=await ref('Increment','click'),method='click')
                assert clicked['status']=='executed_unverified'
                deadline=time.monotonic()+2
                while actual()['count']!=1 and time.monotonic()<deadline:await asyncio.sleep(.03)
                assert actual()['count']==1
                entry=await ref('Text entry','set_text')
                await call('gui_invoke',ref=entry,method='set_text',arguments={'text':'MCP control object'})
                assert (await call('gui_invoke',ref=entry,method='read_text'))['value']=='MCP control object'
                await call('gui_invoke',ref=await ref('Second','select'),method='select')
                deadline=time.monotonic()+2
                while actual()['selected_rows']!=[1] and time.monotonic()<deadline:await asyncio.sleep(.03)
                assert actual()['selected_rows']==[1]
                await call('gui_detach',source=source)
                assert cache_results==[False,False,False] and all(readonly_cache_results),(cache_results,readonly_cache_results)
                report.update(ok=True,operations=['gui_attach','gui_scan(partial)','gui_find','gui_invoke(click/set_text/read_text/select)','gui_detach'],
                              find_cache_results=cache_results,readonly_find_cache_results=readonly_cache_results,state_changes_independently_observed=True)
    except Exception as exc:report.update(error_type=type(exc).__name__,error=str(exc),traceback=traceback.format_exc())
    finally:report['process_cleanup']=close_owned(owned)
    output=ROOT/'reports/control-objects-20261024';output.mkdir(parents=True,exist_ok=True)
    (output/'mcp.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False),flush=True)
    return 0 if report['ok'] else 1


if __name__=='__main__':raise SystemExit(asyncio.run(main()))
