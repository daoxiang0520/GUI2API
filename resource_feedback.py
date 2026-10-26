"""MCP projection for resource artifacts. File references remain available."""
import base64
import json
from mcp.types import AudioContent,CallToolResult,ImageContent,ResourceLink,TextContent

RASTER={'image/png','image/jpeg','image/webp','image/gif'}


def mcp_resource_result(resources,result):
    content=[]
    if result.get('ok'):
        data=result['data'];artifact=data['artifact'];delivery=data.get('delivery_requested','reference')
        feedback={**data.get('feedback',{}),'transport':'mcp','sent_as':'resource_reference','model_understanding_verified':False}
        if delivery=='native' and artifact['bytes']<=4*1024*1024:
            if artifact['mime_type'] in RASTER or artifact['mime_type'].startswith('audio/'):
                verified,raw=resources.store.read(artifact['artifact_id'])
                encoded=base64.b64encode(raw).decode('ascii')
                if artifact['mime_type'] in RASTER:
                    content.append(ImageContent(type='image',data=encoded,mimeType=artifact['mime_type']))
                    feedback['sent_as']='image_content'
                else:
                    content.append(AudioContent(type='audio',data=encoded,mimeType=artifact['mime_type']))
                    feedback['sent_as']='audio_content'
            else:feedback['reason']='This MIME type has no native MCP content block; use the resource reference'
        elif delivery=='native':feedback['reason']='Native inline content is bounded to 4 MiB; the full artifact is retained'
        if delivery=='text' and 'text' in data:
            content.append(TextContent(type='text',text=data['text']['value']));feedback['sent_as']='text_content'
        result={**result,'data':{**data,'feedback':feedback}}
        content.append(ResourceLink(type='resource_link',uri=artifact['uri'],name=artifact.get('name') or 'resource-'+artifact['artifact_id'],
            mimeType=artifact['mime_type'],size=artifact['bytes'],description='Verified artifact bytes; original quality and model understanding are not implied'))
    content.insert(0,TextContent(type='text',text=json.dumps(result,ensure_ascii=False)))
    return CallToolResult(content=content,structuredContent=result,isError=not result.get('ok'))
