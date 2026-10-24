/* Framework adapter, not an application profile. No app names or CSS slots. */
(function createGuiDOM() {
  'use strict';
  const epoch = crypto.randomUUID(), ids = new WeakMap(), refs = new Map();
  let serial = 0, snapshot = null;
  const fail = (error, message = error) => ({error, message});
  const clip = value => String(value ?? '').slice(0, 160);
  function id(element) {
    if (!ids.has(element)) ids.set(element, epoch + ':' + (++serial));
    const key = ids.get(element);
    refs.set(key, new WeakRef(element));
    if (refs.size > 20000) refs.delete(refs.keys().next().value);
    return key;
  }
  function resolve(key) {
    const element = refs.get(key)?.deref();
    return element?.isConnected ? element : null;
  }
  function protectedNode(e) {
    return e.type === 'password' || !!e.closest('[data-appctl-private]');
  }
  function name(e) {
    const doc = e.ownerDocument;
    const labelled = (e.getAttribute('aria-labelledby') || '').split(/\s+/)
      .map(k => doc.getElementById(k)?.textContent || '').join(' ').trim();
    const candidates = [
      ['aria-label', e.getAttribute('aria-label')], ['aria-labelledby', labelled],
      ['label', e.labels && Array.from(e.labels).map(x => x.textContent).join(' ')],
      ['placeholder', e.getAttribute('placeholder')], ['title', e.getAttribute('title')],
      ['alt', e.getAttribute('alt')],
      ['direct_text', Array.from(e.childNodes).filter(n => n.nodeType === 3).map(n => n.textContent).join(' ').trim()]
    ];
    const found = candidates.find(([, value]) => value && String(value).trim());
    return found ? {name: clip(found[1]), name_source: found[0]} : {name: '', name_source: 'unlabelled'};
  }
  function editor(e) {
    const desc = e.pmViewDesc;
    const view = desc?.view;
    const doc = () => view?.state?.doc || e.pmViewDesc?.node;
    if (!doc() || typeof doc().textBetween !== 'function') return null;
    return {view, doc, read: () => doc().textBetween(0, doc().content.size, '\n')};
  }
  function read(e) {
    const model = e.isContentEditable && editor(e);
    if (model) return {value: model.read(), source: 'ProseMirror.document', label_only: false};
    if (['INPUT', 'TEXTAREA', 'SELECT'].includes(e.tagName))
      return {value: e.value, source: 'DOM.value', label_only: false};
    return {value: e.innerText ?? e.textContent ?? '', source: 'DOM.text', label_only: !e.isContentEditable};
  }
  function describe(e, parent) {
    const tag = e.tagName.toLowerCase(), type = (e.type || '').toLowerCase();
    const secret = protectedNode(e), win = e.ownerDocument.defaultView;
    const box = e.getBoundingClientRect(), style = win.getComputedStyle(e);
    const hidden = box.width === 0 || box.height === 0 || style.display === 'none' || style.visibility === 'hidden';
    const roles = {button:'Button', input: type === 'checkbox' ? 'CheckBox' : type === 'radio' ? 'RadioButton' : type === 'range' ? 'Slider' : 'Edit',
      textarea:'Edit', select:'ComboBox', option:'ListItem', a:'Hyperlink', canvas:'Canvas', iframe:'Frame'};
    const editingHost = e.isContentEditable && !e.parentElement?.isContentEditable;
    const semanticRole = e.getAttribute('role') || '';
    const ariaRoles = {button:'Button',checkbox:'CheckBox',switch:'CheckBox',radio:'RadioButton',textbox:'Edit',searchbox:'Edit',
      combobox:'ComboBox',list:'List',listitem:'ListItem',option:'ListItem',tab:'TabItem',tablist:'Tab',tree:'Tree',treeitem:'TreeItem',
      menu:'Menu',menuitem:'MenuItem',toolbar:'ToolBar',img:'Image',dialog:'Window',group:'Group',slider:'Slider'};
    const role = ariaRoles[semanticRole] || semanticRole || roles[tag] || (editingHost ? 'Edit' : 'Pane');
    const actions = secret ? [] : ['get'];
    // click() is also useful for delegated handlers; it does not prove purpose.
    if (!secret && typeof e.click === 'function' && !['html','body','script','style'].includes(tag)) actions.push('invoke');
    if (!secret && typeof e.focus === 'function') actions.push('focus');
    const textual = tag === 'textarea' || tag === 'select' || tag === 'input' &&
      !['password','file','checkbox','radio','button','submit','reset','image','hidden'].includes(type);
    if (!secret && !e.readOnly && e.getAttribute('aria-readonly') !== 'true' && (textual || editingHost)) actions.push('set');
    if (!secret && (type === 'checkbox' || ['checkbox','switch'].includes(semanticRole))) actions.push('toggle-on','toggle-off');
    if (!secret && (type === 'radio' || tag === 'option' || ['tab','option','treeitem'].includes(semanticRole))) actions.push('select');
    if (!secret && (e.hasAttribute('aria-expanded') || tag === 'details')) actions.push('expand','collapse');
    if (!secret && (e.scrollHeight > e.clientHeight || e.scrollWidth > e.clientWidth)) actions.push('scroll');
    return {ref:id(e), parent, role, semantic_role:semanticRole, tag, input_type:type, ...(secret ? {name:'',name_source:'protected'} : name(e)),
      automation_id:e.id || '', class_name:clip(typeof e.className === 'string' ? e.className : ''),
      enabled:!e.matches(':disabled') && e.getAttribute('aria-disabled') !== 'true', offscreen:hidden,
      password:secret, editable:!!editingHost, actions, action_evidence:'DOM method/property; purpose and business effects unverified',
      rect:{x:box.x,y:box.y,width:box.width,height:box.height}, framework:'DOM', region:'content',
      opaque: ['canvas','video'].includes(tag), open_shadow:!!e.shadowRoot};
  }
  function scan(request) {
    const limit = request.limit ?? 300, depth = request.depth ?? 40, max = request.max_nodes ?? 12000;
    if (!Number.isInteger(limit) || limit < 1 || limit > 3000 || !Number.isInteger(depth) || depth < 0 || depth > 100 ||
        !Number.isInteger(max) || max < 1 || max > 20000) return fail('invalid_arguments');
    if (request.cursor) {
      const [token, offsetText] = request.cursor.split('/'), offset = Number(offsetText);
      if (!snapshot || token !== snapshot.token || !Number.isInteger(offset) || offset < 0 || offset > snapshot.rows.length)
        return fail('stale_cursor');
      return page(offset, limit);
    }
    const root = request.root_ref ? resolve(request.root_ref) : document.documentElement;
    if (!root) return fail('stale_ref');
    const rows = [], gaps = [], queue = [{e:root,parent:null,depth:0}];
    let index = 0, visited = 0, depthLimited = false;
    const started = performance.now();
    while (index < queue.length && visited < max && performance.now() - started < 3000) {
      const {e,parent,depth:d} = queue[index++]; visited++;
      if (!e.isConnected) continue;
      const row = describe(e, parent), omit = ['script','style','meta','link','head','noscript','template'].includes(row.tag);
      const include = !omit && (request.include_hidden || !row.offscreen);
      if (include) rows.push(row);
      const nextParent = include ? row.ref : parent;
      if (row.opaque) gaps.push({ref:row.ref,kind:'pixel_region',detail:'No internal controls inferred from pixels'});
      if (omit || protectedNode(e)) continue;
      let children = Array.from(e.children);
      if (e.shadowRoot) children.push(...e.shadowRoot.children);
      if (row.tag === 'iframe') {
        try {
          if (e.contentDocument?.documentElement) children.push(e.contentDocument.documentElement);
          else gaps.push({ref:row.ref,kind:'inaccessible_frame'});
        } catch (_) { gaps.push({ref:row.ref,kind:'inaccessible_frame'}); }
      }
      if (d >= depth) { if (children.length) depthLimited = true; continue; }
      for (const child of children) {
        if (queue.length >= max) { depthLimited = true; break; }
        queue.push({e:child,parent:nextParent,depth:d+1});
      }
    }
    snapshot = {token:crypto.randomUUID(), rows, gaps, visited,
      truncated:index < queue.length || depthLimited, collection_ms:performance.now()-started};
    return page(0, limit);
  }
  function page(offset, limit) {
    const next = offset + limit;
    return {epoch, document_origin:location.origin, snapshot:snapshot.token, elements:snapshot.rows.slice(offset,next), total:snapshot.rows.length,
      cursor:next < snapshot.rows.length ? snapshot.token+'/'+next : null, truncated:snapshot.truncated,
      visited:snapshot.visited, collection_ms:snapshot.collection_ms, gaps:snapshot.gaps,
      coverage:'Current instantiated DOM, open shadow roots and same-origin frames only; unloaded/virtualized and closed roots not enumerated'};
  }
  async function act(request) {
    const e = resolve(request.ref);
    if (!e) return fail('stale_ref');
    const row = describe(e, null), op = request.method, args = request.arguments || {};
    if (!row.actions.includes(op)) return fail(row.password ? 'protected_control' : 'unsupported');
    if (op === 'get') {
      const result = read(e), max = args.max_length ?? 4096;
      return {...result,value:result.value.slice(0,max),truncated:result.value.length>max};
    }
    if (!row.enabled) return fail('disabled_control');
    if (op === 'focus') {
      e.focus({preventScroll:true});
      return {status:e.getRootNode().activeElement===e?'verified':'executed_unverified'};
    }
    if (op === 'set') {
      const value = String(args.value), before = read(e);
      if (Object.hasOwn(args,'expected') && before.value !== args.expected) return fail('value_conflict');
      const model = e.isContentEditable && editor(e);
      if (e.isContentEditable) {
        if (model) {
          let rich = false;
          model.doc().descendants(n => { if (n.isText && n.marks.length || !n.isText && !n.isTextblock) rich = true; });
          if (rich) return fail('rich_editor_replacement_not_supported');
        }
        e.focus({preventScroll:true});
        if (e.getRootNode().activeElement !== e) return fail('focus_unverified');
        const doc = e.ownerDocument, selection = doc.getSelection(), range = doc.createRange();
        range.selectNodeContents(e); selection.removeAllRanges(); selection.addRange(range);
        if (!e.contains(selection.anchorNode) || !e.contains(selection.focusNode)) return fail('selection_outside_editor');
        if (!doc.execCommand(value === '' ? 'delete' : 'insertText', false, value || null)) return fail('editing_command_not_accepted');
        const deadline = Date.now()+500;
        while (model && e.isConnected && model.read() !== value && Date.now()<deadline) await new Promise(r=>setTimeout(r,20));
      } else {
        const win = e.ownerDocument.defaultView;
        const proto = e.tagName === 'INPUT' ? win.HTMLInputElement.prototype : e.tagName === 'TEXTAREA' ? win.HTMLTextAreaElement.prototype : win.HTMLSelectElement.prototype;
        Object.getOwnPropertyDescriptor(proto,'value').set.call(e,value);
        e.dispatchEvent(new win.InputEvent('input',{bubbles:true,inputType:'insertText',data:value}));
        e.dispatchEvent(new win.Event('change',{bubbles:true}));
      }
      if (!e.isConnected) return fail('outcome_unknown','Control detached after one write');
      const result = read(e);
      return {...result,status:result.value === value?'verified':'verification_failed',verification:model?'editor_model':'field_value',business_completion_proven:false};
    }
    if (op === 'invoke') { e.click(); return {status:'executed_unverified',business_completion_proven:false}; }
    if (op === 'toggle-on' || op === 'toggle-off') {
      const state = () => e.type === 'checkbox' ? e.checked : e.getAttribute('aria-checked') === 'true';
      const desired = op === 'toggle-on'; if (state() !== desired) e.click();
      return {status:state()===desired?'verified':'verification_failed',state:state()};
    }
    if (op === 'select') {
      if (e.tagName === 'OPTION') {
        e.selected = true; const parent = e.closest('select'), win = e.ownerDocument.defaultView;
        parent?.dispatchEvent(new win.Event('input',{bubbles:true})); parent?.dispatchEvent(new win.Event('change',{bubbles:true}));
      } else e.click();
      const state = e.selected || e.checked || e.getAttribute('aria-selected') === 'true';
      return {status:state?'verified':'executed_unverified',selected:!!state};
    }
    if (op === 'expand' || op === 'collapse') {
      const desired = op === 'expand', state = () => e.tagName === 'DETAILS' ? e.open : e.getAttribute('aria-expanded') === 'true';
      if (state() !== desired) { if (e.tagName === 'DETAILS') e.open = desired; else e.click(); }
      return {status:state()===desired?'verified':'verification_failed',expanded:state()};
    }
    if (op === 'scroll') {
      const before = [e.scrollLeft,e.scrollTop], amount = args.amount ?? 300;
      const delta = {up:[0,-amount],down:[0,amount],left:[-amount,0],right:[amount,0]}[args.direction];
      if (!delta) return fail('invalid_arguments');
      e.scrollBy({left:delta[0],top:delta[1],behavior:'instant'});
      return {status:e.scrollLeft!==before[0] || e.scrollTop!==before[1]?'verified':'no_change',left:e.scrollLeft,top:e.scrollTop};
    }
    return fail('unsupported');
  }
  return {async dispatch(request) {
    try { return request.op === 'scan' ? scan(request) : await act(request); }
    catch (error) { return fail(request.op === 'scan' || request.method === 'get' ? 'runtime_error' : 'outcome_unknown',String(error)); }
  }};
})
