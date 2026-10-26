/* Generic DOM resource descriptors and bounded readers. No application selectors. */
(function createGuiResources(api) {
  'use strict';
  const {id,resolve,fail}=api;
  const records = new Map(), owners = new WeakMap(), streams = new Map();
  const opaque = () => Array.from(crypto.getRandomValues(new Uint8Array(16)),b=>b.toString(16).padStart(2,'0')).join('');
  const allowed = new Set(['http:', 'https:', 'blob:', 'data:', 'file:']);
  const clip = value => String(value || '').slice(0, 256);
  const mime = value => /^[a-z0-9!#$&^_.+-]+\/[a-z0-9!#$&^_.+-]+$/i.test(String(value || '').split(';')[0])
    ? String(value).split(';')[0].toLowerCase() : null;
  function kind(type, uri = '') {
    if (type?.startsWith('image/')) return 'image';
    if (type?.startsWith('audio/')) return 'audio';
    if (type?.startsWith('video/')) return 'video';
    if (type?.startsWith('text/') || /(?:json|xml)/.test(type || '')) return 'text';
    if (/(?:pdf|officedocument|msword|ms-excel|ms-powerpoint|opendocument)/.test(type || '')) return 'document';
    const suffix = uri.split(/[?#]/)[0].split('.').at(-1)?.toLowerCase();
    if (['png','jpg','jpeg','gif','webp','svg','avif','bmp'].includes(suffix)) return 'image';
    if (['mp3','wav','ogg','m4a','aac','flac','opus'].includes(suffix)) return 'audio';
    if (['mp4','webm','mkv','mov','avi'].includes(suffix)) return 'video';
    if (['pdf','doc','docx','xls','xlsx','ppt','pptx','odt','ods'].includes(suffix)) return 'document';
    if (['txt','json','xml','vtt','srt','csv','md'].includes(suffix)) return 'text';
    return 'file';
  }
  function specs(e) {
    const tag = e.tagName?.toLowerCase(), result = [];
    const add = (slot, uri, type, fields = {}) => {
      if (!uri && !fields.blob && !fields.generated && !fields.stream_only) return;
      let url; try { if (uri) url = new URL(uri, e.baseURI); } catch (_) { return; }
      result.push({slot, uri: url?.href || '', mime_type: mime(type), kind: kind(mime(type), url?.pathname || ''),
        name: clip(e.getAttribute('download') || e.getAttribute('alt') || e.getAttribute('aria-label') ||
          (url && ['http:','https:','file:'].includes(url.protocol) ? url.pathname.split('/').at(-1) : '')),
        scheme: url?.protocol || null, ...fields});
    };
    if (tag === 'img') add('image', e.currentSrc || e.src, e.getAttribute('type'), {kind:'image',relation:'embedded',
      width:e.naturalWidth || null,height:e.naturalHeight || null,loaded:!!e.complete && e.naturalWidth>0});
    if (tag === 'audio' || tag === 'video') {
      add('media', e.currentSrc || e.src || e.querySelector('source[src]')?.src, e.getAttribute('type') || e.querySelector('source[type]')?.type,
        {kind:tag,relation:'embedded',stream_only:!!e.srcObject,duration_seconds:Number.isFinite(e.duration)?e.duration:null,
         width:e.videoWidth || null,height:e.videoHeight || null,loaded:e.readyState>0});
      if (tag === 'video' && e.poster) add('poster',e.poster,'image/*',{kind:'image',relation:'poster'});
      [...e.querySelectorAll('source[src]')].forEach((s,i)=>add('source:'+i,s.src,s.type,{kind:tag,relation:'encoding_variant'}));
      [...e.querySelectorAll('track[src]')].forEach((t,i)=>add('track:'+i,t.src,'text/vtt',{kind:'text',relation:'captions',name:clip(t.label)}));
    }
    if (tag === 'a' && e.href && (e.hasAttribute('download') || e.type || kind(null,e.href)!=='file'))
      add('download',e.href,e.type,{relation:e.hasAttribute('download')?'attachment':'linked_resource'});
    if (tag === 'object' || tag === 'embed') add('document',e.data || e.src,e.type,{relation:'embedded'});
    if (tag === 'iframe' && kind(null,e.src)==='document') add('document',e.src,'application/pdf',{kind:'document',relation:'embedded'});
    if (tag === 'canvas') add('canvas',null,'image/png',{kind:'image',relation:'rendered_surface',generated:'canvas',width:e.width,height:e.height});
    if (tag === 'svg') add('svg',null,'image/svg+xml',{kind:'image',relation:'inline_document',generated:'svg'});
    if (tag === 'input' && e.type === 'file') [...e.files].forEach((file,i)=>add('file:'+i,null,file.type,
      {kind:kind(mime(file.type),file.name),relation:'selected_file',name:clip(file.name),size_bytes:file.size,blob:file}));
    const style=e.ownerDocument.defaultView.getComputedStyle(e);
    let index=0;
    for (const match of style.backgroundImage.matchAll(/url\(\s*(?:"([^"]*)"|'([^']*)'|([^)]*))\s*\)/g))
      add('background:'+index++,match[1] || match[2] || match[3],null,{kind:'image',relation:'background'});
    return result;
  }
  function signature(spec) { return JSON.stringify([spec.slot,spec.uri,spec.mime_type,spec.generated,spec.name,spec.size_bytes,spec.stream_only]); }
  function describe(e) {
    let known=owners.get(e);if(!known){known=new Map();owners.set(e,known);}
    return specs(e).map(spec=>{
      const key=signature(spec), old=known.get(spec.slot);
      let token=old?.signature===key && old?.blob===spec.blob ? old.token : null;
      if(token && !records.has(token))token=null;
      if(!token){token=opaque();known.set(spec.slot,{token,signature:key,blob:spec.blob});
        records.set(token,{element:new WeakRef(e),slot:spec.slot,signature:key,blob:spec.blob});}
      while(records.size>4096)records.delete(records.keys().next().value);
      const readable=!!spec.blob || !!spec.generated || !!spec.uri && allowed.has(spec.scheme) && !spec.stream_only;
      return {provider_ref:token,control_ref:id(e),kind:spec.kind,mime_type:spec.mime_type,name:spec.name,relation:spec.relation,
        scheme:spec.scheme,size_bytes:spec.size_bytes ?? null,width:spec.width ?? null,height:spec.height ?? null,
        duration_seconds:spec.duration_seconds ?? null,loaded:spec.loaded ?? null,stream_only:!!spec.stream_only,
        source_readable:readable && spec.generated!=='canvas',generated:spec.generated || null,
        evidence:'DOM resource attribute or selected File object; bytes not read',original_quality_verified:false};
    });
  }
  function current(token) {
    const record=records.get(token),e=record?.element.deref();
    if(!e?.isConnected)throw Object.assign(new Error('Resource object is detached'),{code:'stale_resource'});
    const spec=specs(e).find(s=>s.slot===record.slot);
    if(!spec || signature(spec)!==record.signature || spec.blob!==record.blob)
      throw Object.assign(new Error('Resource source changed'),{code:'stale_resource'});
    return {e,spec};
  }
  async function open(request) {
    if(streams.size>=4)return fail('resource_limit','Close an existing resource stream');
    const {e,spec}=current(request.resource_ref);
    if(spec.stream_only)return fail('stream_unavailable','Live media needs an explicit stream provider; it is not recorded automatically');
    const max=request.max_bytes;
    if(!Number.isInteger(max) || max<1 || max>268435456)return fail('invalid_arguments');
    const controller=new AbortController();
    const timer=setTimeout(()=>{controller.abort();for(const [token,s] of streams)if(s.controller===controller)void close(token);},Math.min(30000,request.timeout_ms || 20000));
    let response,blob=spec.blob,scale=1;
    try {
      if(spec.generated==='canvas') {
        if(request.representation!=='rendered')return fail('representation_unavailable','Canvas exposes rendered pixels, not an original file');
        if(e.width*e.height>16777216)return fail('resource_too_large');
        const pixels=request.max_pixels ?? 1000000;
        if(!Number.isInteger(pixels)||pixels<1||pixels>16777216)return fail('invalid_arguments');
        scale=Math.min(1,Math.sqrt(pixels/(e.width*e.height)));
        let surface=e;
        if(scale<1){
          surface=e.ownerDocument.createElement('canvas');surface.width=Math.max(1,Math.floor(e.width*scale));surface.height=Math.max(1,Math.floor(e.height*scale));
          surface.getContext('2d').drawImage(e,0,0,surface.width,surface.height);
        }
        blob=await new Promise(resolve=>surface.toBlob(resolve,'image/png'));
        if(!blob)return fail('resource_unavailable','Canvas encoding failed');
      } else if(spec.generated==='svg') blob=new Blob([new XMLSerializer().serializeToString(e)],{type:'image/svg+xml'});
      if(blob && blob.size>max)return fail('resource_too_large');
      if(!blob) {
        if(!allowed.has(spec.scheme))return fail('resource_scheme_unavailable','A read-only provider is required for this application scheme');
        const url=new URL(spec.uri);if(url.username || url.password)return fail('resource_scheme_unavailable','Credential URLs are not fetched');
        if(spec.scheme==='data:' && spec.uri.length>max*1.5+2048)return fail('resource_too_large');
        response=await fetch(spec.uri,{signal:controller.signal,credentials:'same-origin',redirect:'error'});
        if(!response.ok)return fail('resource_unavailable','Resource request was rejected');
        const size=Number(response.headers.get('content-length'));
        if(Number.isFinite(size) && size>max){controller.abort();return fail('resource_too_large');}
      }
      const reader=(blob?blob.stream():response.body).getReader();
      const token=opaque();
      streams.set(token,{reader,controller,timer,resource_ref:request.resource_ref,max,total:0,pending:null,done:false});
      return {stream:token,mime_type:mime(blob?.type || response?.headers.get('content-type')) || spec.mime_type,
        size_bytes:blob?.size ?? null,representation:spec.generated==='canvas'?'canvas_pixels':spec.generated==='svg'?'dom_serialization':'source_bytes',
        ...(spec.generated==='canvas'?{scale,visible_only:false}: {})};
    } finally {
      // Successful streams own their deadline until resource_close.
      if(![...streams.values()].some(s=>s.controller===controller)){clearTimeout(timer);controller.abort();}
    }
  }
  async function chunk(request) {
    const stream=streams.get(request.stream);
    if(!stream)return fail('stale_resource','Resource stream is closed');
    current(stream.resource_ref);
    const limit=request.max_bytes ?? 524288;
    if(!Number.isInteger(limit)||limit<1||limit>524288)return fail('invalid_arguments');
    if(!stream.pending?.length && !stream.done) {
      const result=await stream.reader.read();stream.done=result.done;
      stream.pending=result.value || new Uint8Array();stream.total+=stream.pending.length;
      if(stream.total>stream.max){await close(request.stream);return fail('resource_too_large');}
    }
    const part=stream.pending.subarray(0,limit);stream.pending=stream.pending.subarray(part.length);
    const fragments=[];for(let i=0;i<part.length;i+=32768)fragments.push(String.fromCharCode(...part.subarray(i,i+32768)));
    return {data_base64:btoa(fragments.join('')),done:stream.done && stream.pending.length===0};
  }
  async function close(token) {
    const stream=streams.get(token);if(!stream)return {closed:true};streams.delete(token);
    clearTimeout(stream.timer);stream.controller.abort();try{await stream.reader.cancel();}catch(_){}
    return {closed:true};
  }
  function rect(token) {
    const {e}=current(token),win=e.ownerDocument.defaultView,b=e.getBoundingClientRect();
    if(b.width<=0 || b.height<=0 || win.getComputedStyle(e).visibility==='hidden')return fail('resource_not_visible');
    // Frame coordinate transforms need a dedicated geometry provider.
    if(win!==window)return fail('resource_capture_unavailable','Frame rendering is not captured with guessed coordinates');
    const x=Math.max(0,b.x),y=Math.max(0,b.y),right=Math.min(win.innerWidth,b.right),bottom=Math.min(win.innerHeight,b.bottom);
    if(right<=x||bottom<=y)return fail('resource_not_visible');
    return {x:x+win.scrollX,y:y+win.scrollY,width:right-x,height:bottom-y,device_scale:win.devicePixelRatio || 1,
      clipped:x!==b.x||y!==b.y||right!==b.right||bottom!==b.bottom,space:'page_css',visible_only:true};
  }
  return {describe,async dispatch(request){
    try {
      if(request.op==='resource_open')return await open(request);
      if(request.op==='resource_chunk')return await chunk(request);
      if(request.op==='resource_close')return await close(request.stream);
      if(request.op==='resource_rect')return rect(request.resource_ref);
      return fail('unknown_operation');
    } catch(error) {return fail(error.code || 'resource_unavailable',error.code?'Resource identity or availability changed':'The provider could not read this resource');}
  },dispose(){for(const token of [...streams.keys()])void close(token);records.clear();}};
})
