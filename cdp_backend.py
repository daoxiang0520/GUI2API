"""Optional Chromium channel. Attaches to existing loopback CDP endpoints only.

No app restart, browser profile access, screenshots, OCR, or arbitrary JS command
surface. Fixed DOM functions receive caller text through JSON arguments.
"""
from __future__ import annotations

from collections import deque
import ipaddress
import json
import time
import uuid
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, build_opener


class DriverFault(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def loopback_url(url: str, websocket=False) -> str:
    try:
        p = urlsplit(url)
        schemes = ("ws", "wss") if websocket else ("http", "https")
        local = p.hostname == "localhost" or ipaddress.ip_address(p.hostname or "").is_loopback
        if p.scheme not in schemes or not local or not p.port or p.username or p.password or p.fragment:
            raise ValueError()
        return url.rstrip("/")
    except ValueError:
        raise DriverFault("invalid_endpoint", "CDP requires a loopback URL with an explicit port and no credentials.")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise DriverFault("invalid_endpoint", "CDP endpoint redirects are not followed.")


def fetch_json(endpoint, path, timeout=1.0):
    endpoint = loopback_url(endpoint)
    p = urlsplit(endpoint)
    if p.path not in ("", "/") or p.query:
        raise DriverFault("invalid_endpoint", "Use the CDP server origin, without a path or query.")
    with build_opener(ProxyHandler({}), NoRedirect()).open(endpoint + path, timeout=timeout) as response:
        raw = response.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise DriverFault("response_too_large", "CDP discovery exceeded 2 MiB.")
        return json.loads(raw)


def targets(endpoint, timeout=1.0):
    result = fetch_json(endpoint, "/json/list", timeout=timeout)
    if not isinstance(result, list):
        raise DriverFault("not_cdp", "The endpoint did not return CDP targets.")
    if any(not isinstance(r, dict) for r in result):
        raise DriverFault('not_cdp', 'CDP target entries must be objects.')
    pages = [r for r in result if r.get('type') in ('page', 'webview') and r.get('webSocketDebuggerUrl')]
    if any(not isinstance(r.get('id'), str) or not r['id'] or not isinstance(r['webSocketDebuggerUrl'], str) for r in pages):
        raise DriverFault('not_cdp', 'A CDP page target lacks an ID or WebSocket URL.')
    return pages


class Connection:
    def __init__(self, url, timeout=6):
        from websockets.sync.client import connect
        self.socket = connect(loopback_url(url, True), open_timeout=2, close_timeout=.5, max_size=8 * 1024 * 1024, proxy=None)
        self.timeout = timeout
        self.sequence = 0
        self.epoch = 0
        self.instance = uuid.uuid4().hex[:8]
        self.events = deque(maxlen=1024)
        self.dropped = 0
        self.closed = False
        for method in ("Runtime.enable", "DOM.enable", "Accessibility.enable"):
            self.rpc(method)

    def _event(self, message):
        method = message.get("method", "")
        if method in ("DOM.documentUpdated", "Runtime.executionContextsCleared"):
            self.epoch += 1
        if method.startswith(("DOM.", "Accessibility.")) or method == "Runtime.executionContextsCleared":
            if len(self.events) == self.events.maxlen:
                self.dropped += 1
            # Do not copy values, page bodies, or raw event payloads into logs.
            self.events.append({"kind": "cdp", "method": method})

    def rpc(self, method, params=None):
        self.sequence += 1
        wire = self.sequence
        self.socket.send(json.dumps({"id": wire, "method": method, "params": params or {}}))
        deadline = time.monotonic() + self.timeout
        while True:
            raw = self.socket.recv(timeout=max(.001, deadline - time.monotonic()))
            message = json.loads(raw)
            if "id" not in message:
                self._event(message)
                continue
            if message["id"] != wire:
                self.close()
                raise DriverFault("protocol_error", "Unexpected CDP response ID; connection closed.")
            if "error" in message:
                raise DriverFault("cdp_error", message["error"].get("message", "CDP request failed"))
            return message.get("result", {})

    def drain(self, timeout=0):
        deadline = time.monotonic() + timeout
        while True:
            try:
                message = json.loads(self.socket.recv(timeout=max(0, deadline - time.monotonic())))
            except TimeoutError:
                return
            if "id" in message:
                self.close()
                raise DriverFault("protocol_error", "Unmatched CDP reply; connection closed.")
            self._event(message)
            # After the first event, return promptly after draining queued events.
            deadline = time.monotonic()

    def close(self):
        self.closed = True
        try:
            self.socket.close()
        except Exception:
            pass


ROLES = {"RootWebArea": "Document", "WebArea": "Document", "button": "Button", "link": "Hyperlink", "textbox": "Edit", "searchbox": "Edit", "combobox": "ComboBox", "checkbox": "CheckBox", "radio": "RadioButton", "StaticText": "Text", "InlineTextBox": "Text", "heading": "Text", "list": "List", "listitem": "ListItem", "menu": "Menu", "menubar": "MenuBar", "menuitem": "MenuItem", "tab": "TabItem", "tablist": "Tab", "tree": "Tree", "treeitem": "TreeItem", "slider": "Slider", "spinbutton": "Spinner", "img": "Image", "group": "Group", "generic": "Pane", "dialog": "Window"}

# This fixed function is the only JavaScript action surface. User values are
# passed as Runtime.callFunctionOn arguments, never interpolated into code.
DOM_ACTION = r"""function(op,value,limit,direction) {
  if (!this.isConnected) return {error:'stale_ref',message:'The DOM node was detached.'};
  const tag=(this.tagName||'').toLowerCase(), type=(this.type||'').toLowerCase();
  if(type==='password') return {error:'protected_control',message:'Password controls are not exposed.'};
  const text=()=>String(['input','textarea','select'].includes(tag)?this.value:(this.innerText??this.textContent??''));
  if(op==='get') { const v=text();return {value:v.slice(0,limit),truncated:v.length>limit,source:'DOM',label_only:!['input','textarea','select'].includes(tag)&&!this.isContentEditable}; }
  if(this.disabled||this.getAttribute?.('aria-disabled')==='true') return {error:'disabled_control',message:'The control is disabled.'};
  if(op==='focus') { this.focus({preventScroll:true});return {status:this.ownerDocument.activeElement===this?'verified':'executed_unverified'}; }
  if(op==='set') {
    if(this.readOnly||this.getAttribute?.('aria-readonly')==='true') return {error:'unsupported',message:'The control is read-only.'};
    if((tag==='input'&&!['file','checkbox','radio','button','submit','reset','image','hidden'].includes(type))||tag==='textarea'||tag==='select') {
      const proto=tag==='input'?this.ownerDocument.defaultView.HTMLInputElement.prototype:tag==='textarea'?this.ownerDocument.defaultView.HTMLTextAreaElement.prototype:this.ownerDocument.defaultView.HTMLSelectElement.prototype;
      Object.getOwnPropertyDescriptor(proto,'value').set.call(this,String(value));
    } else if(this.isContentEditable) this.textContent=String(value);
    else return {error:'unsupported',message:'The node is not an editable text control.'};
    this.dispatchEvent(new Event('input',{bubbles:true}));this.dispatchEvent(new Event('change',{bubbles:true}));
    const actual=text(); return {status:actual===String(value)?'verified':'verification_failed',value:actual};
  }
  if(op==='toggle-on'||op==='toggle-off') {
    const desired=op==='toggle-on',state=()=>type==='checkbox'?this.checked:this.getAttribute('aria-checked')==='true';
    if(type!=='checkbox'&&this.getAttribute?.('role')!=='checkbox') return {error:'unsupported',message:'Not a checkbox.'};
    if(state()!==desired)this.click();return {status:state()===desired?'verified':'verification_failed',state:state()};
  }
  if(op==='expand'||op==='collapse') {
    if(!this.hasAttribute('aria-expanded'))return {error:'unsupported',message:'No expanded state.'};
    const desired=op==='expand';if((this.getAttribute('aria-expanded')==='true')!==desired)this.click();
    return {status:(this.getAttribute('aria-expanded')==='true')===desired?'verified':'verification_failed'};
  }
  if(op==='select'&&tag==='option') { this.selected=true;this.parentElement.dispatchEvent(new Event('input',{bubbles:true}));this.parentElement.dispatchEvent(new Event('change',{bubbles:true}));return {status:this.selected?'verified':'verification_failed'}; }
  if(op==='invoke'||op==='select') {if(typeof this.click!=='function')return {error:'unsupported',message:'No DOM click method.'};this.click();return {status:'executed_unverified'};}
  if(op==='scroll') { const d={up:[0,-120],down:[0,120],left:[-120,0],right:[120,0]}[direction];if(!d)return {error:'invalid_arguments',message:'Unknown direction.'};this.scrollBy(...d);return {status:'executed_unverified'}; }
  return {error:'unsupported',message:'Unsupported DOM action.'};
}"""


class CdpBackend:
    def __init__(self):
        self.connections = {}
        self.refs = {}

    def _connection(self, args):
        endpoint = loopback_url(args.get("endpoint", ""))
        pages = targets(endpoint)
        requested = args.get("target")
        selected = [p for p in pages if p["id"] == requested] if requested else pages
        if len(selected) != 1:
            raise DriverFault("ambiguous_target" if selected else "target_not_found", "Select one exact CDP page target using targets and --target.")
        page = selected[0]
        ws = loopback_url(page["webSocketDebuggerUrl"], True)
        if urlsplit(ws).port != urlsplit(endpoint).port:
            raise DriverFault("invalid_endpoint", "CDP WebSocket must use the discovered loopback port.")
        key = (endpoint, page["id"])
        if key not in self.connections or self.connections[key].closed:
            self.connections[key] = Connection(ws)
        return self.connections[key], key

    def _inspect(self, args):
        conn, key = self._connection(args)
        depth, limit = int(args.get("depth", 6)), int(args.get("limit", 300))
        if not 0 <= depth <= 20 or not 1 <= limit <= 3000:
            raise DriverFault("invalid_arguments", "depth must be 0..20 and limit 1..3000.")
        raw = conn.rpc("Accessibility.getFullAXTree", {"depth": depth})["nodes"]
        epoch = conn.epoch
        rows = []
        mapping = {n["nodeId"]: f"{key[1]}:{conn.instance}:{epoch}:{n['backendDOMNodeId']}" for n in raw if n.get("backendDOMNodeId") and not n.get("ignored")}
        for n in raw:
            if n.get("ignored") or not n.get("backendDOMNodeId"):
                continue
            role = n.get("role", {}).get("value", "generic")
            if role == "InlineTextBox":
                continue
            props = {p["name"]: p["value"].get("value") for p in n.get("properties", [])}
            name = str(n.get("name", {}).get("value", ""))
            password = False
            attrs = {}
            editable_node = False
            if role in ("textbox", "searchbox", "combobox", "spinbutton") or props.get("editable"):
                node = conn.rpc("DOM.describeNode", {"backendNodeId": n["backendDOMNodeId"]})["node"]
                attr_list = node.get("attributes", [])
                attrs = dict(zip(attr_list[::2], attr_list[1::2]))
                password = attrs.get("type", "").lower() == "password"
                tag, kind = node.get("nodeName", "").lower(), attrs.get("type", "text").lower()
                editable_node = tag in ("textarea", "select") or tag == "input" and kind not in ("password", "file", "checkbox", "radio", "button", "submit", "reset", "image", "hidden") or "contenteditable" in attrs and attrs["contenteditable"].lower() in ("", "true", "plaintext-only")
            actions = [] if password else ["get"]
            if props.get("focusable"):
                actions.append("focus")
            if not password and not props.get("readonly") and editable_node:
                actions.append("set")
            if role in ("button", "link", "menuitem", "tab", "checkbox", "radio", "treeitem", "DisclosureTriangle"):
                actions.append("invoke")
            if role == "checkbox":
                actions += ["toggle-on", "toggle-off"]
            if role in ("tab", "option", "radio", "treeitem"):
                actions.append("select")
            if "expanded" in props:
                actions += ["expand", "collapse"]
            ref = mapping[n["nodeId"]]
            self.refs[ref] = (key, conn.instance, epoch, n["backendDOMNodeId"])
            row = {"ref": ref, "parent": mapping.get(n.get("parentId")), "role": ROLES.get(role, role), "name": name[:160], "automation_id": attrs.get("id", ""), "actions": actions, "enabled": not props.get("disabled", False), "offscreen": bool(props.get("hidden", False)), "password": password, "framework": "Chromium", "ax_role": role, "region": "content"}
            if args.get("values") and not password and "value" in n:
                v = str(n["value"].get("value", ""))
                row.update(value=v[:160], value_truncated=len(v) > 160)
            rows.append(row)
            if len(rows) >= limit:
                break
        if conn.epoch != epoch:
            raise DriverFault("stale_snapshot", "The page navigated while its controls were being inspected.")
        return {"elements": rows, "visited": len(rows), "truncated": len(rows) >= limit, "target": key[1], "depth_limit": depth, "epoch": epoch}

    def _action(self, op, args):
        conn, key = self._connection(args)
        conn.drain()
        if args.get("css"):
            doc = conn.rpc("DOM.getDocument")["root"]["nodeId"]
            found = conn.rpc("DOM.querySelectorAll", {"nodeId": doc, "selector": args["css"]})["nodeIds"]
            if len(found) != 1:
                raise DriverFault("ambiguous_target" if found else "element_not_found", "The DOM selector must match exactly one element.")
            object_id = conn.rpc("DOM.resolveNode", {"nodeId": found[0], "objectGroup": "appctl"})["object"]["objectId"]
        else:
            record = self.refs.get(args.get("ref"))
            if not record or record[0] != key or record[1] != conn.instance or record[2] != conn.epoch:
                raise DriverFault("stale_ref", "Inspect again after navigation or reconnection.")
            object_id = conn.rpc("DOM.resolveNode", {"backendNodeId": record[3], "objectGroup": "appctl"})["object"]["objectId"]
        if op == "set" and "value" not in args:
            raise DriverFault("invalid_arguments", "set requires an explicit value.")
        result = conn.rpc("Runtime.callFunctionOn", {"objectId": object_id, "functionDeclaration": DOM_ACTION, "arguments": [{"value": v} for v in [op, args.get("value"), max(1, min(100000, int(args.get("max_length", 4096)))), args.get("direction")]], "returnByValue": True, "userGesture": op != "get"})
        if "exceptionDetails" in result:
            raise DriverFault("outcome_unknown" if op != "get" else "cdp_error", "The DOM operation raised an exception; it was not retried.")
        data = result.get("result", {}).get("value", {})
        if data.get("error"):
            raise DriverFault(data["error"], data.get("message", "DOM operation failed"))
        data["backend"] = "CDP.DOM"
        return data

    def request(self, op, args, request_id=None):
        started = time.perf_counter()
        try:
            if op == "targets":
                data = {"targets": [{"id": p["id"], "title": p.get("title", ""), "type": p["type"]} for p in targets(args.get("endpoint", ""))]}
            elif op == "inspect":
                data = self._inspect(args)
            elif op in ("watch", "events"):
                conn, key = self._connection(args)
                if op == "watch":
                    if args.get("enabled", True):
                        conn.rpc("DOM.getDocument", {"depth": -1, "pierce": True})
                    conn.drain()
                    conn.events.clear()
                    data = {"watching": args.get("enabled", True), "target": key[1], "source": "CDP.DOM/Accessibility events", "contains_values": False}
                else:
                    timeout = int(args.get("timeout_ms", 0))
                    limit = int(args.get("limit", 100))
                    if not 0 <= timeout <= 5000 or not 1 <= limit <= 1024:
                        raise DriverFault("invalid_arguments", "timeout_ms must be 0..5000 and limit 1..1024.")
                    conn.drain(0 if conn.events else timeout / 1000)
                    events = [conn.events.popleft() for _ in range(min(limit, len(conn.events)))]
                    data = {"events": events, "dropped": conn.dropped, "invalidated": bool(events or conn.dropped)}
                    conn.dropped = 0
            else:
                data = self._action(op, args)
            failed = data.get("status") == "verification_failed"
            result = {"id": request_id, "ok": not failed, "data": data, "backend": "cdp"}
            if failed:
                result["error"] = {"code": "verification_failed", "message": "The expected state was not observed; do not repeat blindly."}
        except DriverFault as exc:
            result = {"id": request_id, "ok": False, "error": {"code": exc.code, "message": str(exc)}, "backend": "cdp"}
        except Exception as exc:
            # Socket loss during an action leaves its outcome unknown. Never retry.
            for conn in self.connections.values():
                conn.close()
            code = "outcome_unknown" if op not in ("inspect", "targets", "get", "watch", "events") else "cdp_unavailable"
            result = {"id": request_id, "ok": False, "error": {"code": code, "message": str(exc)}, "backend": "cdp"}
        result["roundtrip_ms"] = round((time.perf_counter() - started) * 1000, 2)
        return result

    def close(self):
        for conn in self.connections.values():
            conn.close()
