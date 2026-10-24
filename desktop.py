"""Window discovery, independent channel negotiation, and stable action routing."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import time
import uuid

from appctl import ACTIONS, BACKENDS, UiaSession, error
from cdp_backend import CdpBackend, targets


def metrics(rows):
    operational = lambda r: set(r.get("actions", [])) - {"get", "focus"}
    return {
        "nodes": len(rows),
        "named": sum(bool(r.get("name", "").strip()) for r in rows),
        "interactive": sum(bool(operational(r)) for r in rows),
        "named_interactive": sum(bool(operational(r)) and bool(r.get("name", "").strip()) for r in rows),
        "content_interactive": sum(bool(operational(r)) and r.get("region") == "content" for r in rows),
        "enabled_interactive": sum(bool(operational(r)) and r.get("enabled", True) for r in rows),
        "settable": sum("set" in r.get("actions", []) for r in rows),
    }


def deduplicate(rows):
    """Suppress only exact, named, geometrically identical weaker duplicates.

    Never merge actions between references: every returned action keeps exactly
    one backend and exactly one original target. Uncertain duplicates stay visible.
    """
    result, seen, suppressed = [], {}, 0
    for row in rows:
        rect, name = row.get("rect", {}), row.get("name", "").strip()
        valid_rect = rect.get("width", 0) > 0 and rect.get("height", 0) > 0
        if not name or not valid_rect:
            result.append(row)
            continue
        key = (row.get("role"), name, row.get("enabled"), row.get("offscreen"), row.get("password", False), *(round(rect.get(k, 0), 1) for k in ("x", "y", "width", "height")))
        if key not in seen:
            seen[key] = len(result)
            result.append(row)
            continue
        previous = result[seen[key]]
        a, b = set(previous.get("actions", [])), set(row.get("actions", []))
        if previous["backend"] != row["backend"] and (a <= b or b <= a):
            if a < b:
                result[seen[key]] = row
            suppressed += 1
        else:
            result.append(row)
    return result, suppressed


def matches(row, args):
    for field in ("name", "role", "automation_id"):
        wanted = args.get(field)
        if wanted is None:
            continue
        actual = str(row.get(field, ""))
        if args.get("contains") and field == "name":
            if str(wanted).casefold() not in actual.casefold():
                return False
        elif actual.casefold() != str(wanted).casefold():
            return False
    return True


class DesktopSession:
    def __init__(self, native_factory=UiaSession, cdp_factory=CdpBackend, input_factory=None):
        self.factory = native_factory
        self.workers = {}
        self.cdp = cdp_factory()
        self.cooldowns = {}
        self.refs = {}
        self.reverse_refs = {}
        self.snapshots = {}
        self.watch_context = None
        self.metadata = {}
        self.primed = set()
        self.input_factory, self.input_driver = input_factory, None
        self.sequence = 0
        self.nonce = uuid.uuid4().hex[:8]
        started = time.perf_counter()
        self._native("win32")
        self.startup_ms = round((time.perf_counter() - started) * 1000, 2)

    def _native(self, backend):
        entry = self.workers.get(backend)
        if entry is None or entry[0].process.poll() is not None:
            self.workers[backend] = (self.factory(backend=backend), uuid.uuid4().hex)
        return self.workers[backend][0]

    def _call(self, backend, op, args, timeout=20):
        try:
            if backend == "cdp":
                return self.cdp.request(op, args)
            worker = self._native(backend)
            previous = worker.timeout
            try:
                worker.timeout = timeout
                return worker.request(op, args)
            finally:
                worker.timeout = previous
        except (OSError, RuntimeError, ValueError) as exc:
            return error("channel_unavailable", str(exc))

    def _describe(self, args):
        cached = self.metadata.get(args.get("window"))
        if cached and not args.get("refresh") and time.monotonic()-cached[0]<60:
            identity = self._call("win32", "window", {"window": args["window"]}, 3)
            if not identity.get("ok"):
                return identity
            if identity["data"].get("pid")==cached[1]["pid"]:
                data={**cached[1],**{k:identity["data"][k] for k in ("minimized","foreground") if k in identity["data"]}}
                if not data.get("minimized"):
                    data.pop("visibility_hint",None)
                return {"ok": True, "data": data}
        result = self._call("win32", "describe", args, 6)
        if not result.get("ok"):
            return result
        data = result["data"]
        evidence = " ".join([data.get("class_name", ""), *data.get("child_classes", []), *data.get("framework_modules", [])]).lower()
        frameworks = []
        for name, signatures in (("Qt", ("qt5", "qt6", "qwindow")), ("Chromium", ("chrome_", "chrome.dll", "libcef", "webview2")), ("Java", ("jvm.dll", "accessbridge")), (".NET", ("clr.dll", "presentationframework"))):
            if any(s in evidence for s in signatures):
                frameworks.append(name)
        data["framework_hints"] = frameworks
        data["framework_detection"] = "module/class evidence; hints are not compatibility guarantees"
        if data.get("minimized"):
            data["visibility_hint"] = "A minimized app may suspend its accessibility tree. Restore or activate the window explicitly before concluding that controls are unsupported."
        self.metadata[args["window"]] = (time.monotonic(), data)
        return result

    def _discover_cdp(self, info):
        # This is not a port scan. Try at most 8 listening sockets owned by the
        # selected window process, and only GET the standard CDP discovery URL.
        discovered, attempts, seen = [], [], set()
        started = time.monotonic()
        listeners = info.get('listeners', [])
        budget_stopped = False
        for listener in listeners:
            port = listener.get('port')
            if type(port) is not int or not 1 <= port <= 65535 or port in seen:
                continue
            remaining = 2.0 - (time.monotonic() - started)
            if len(seen) >= 8 or remaining <= 0:
                budget_stopped = True
                break
            seen.add(port)
            address = listener.get('address', '127.0.0.1')
            host = '[::1]' if address in ('::', '::1') else '127.0.0.1'
            endpoint = f"http://{host}:{port}"
            try:
                pages = targets(endpoint, timeout=min(1.0, remaining))
            except Exception as exc:
                attempts.append({'endpoint':endpoint, 'ok':False,
                                 'error':{'code':getattr(exc, 'code', 'discovery_request_failed'),
                                          'type':type(exc).__name__}})
                continue
            attempts.append({'endpoint':endpoint, 'ok':True, 'target_count':len(pages)})
            discovered.extend({'endpoint':endpoint, 'target':page['id']} for page in pages)
        # An unfinished probe cannot establish that the one observed page is
        # unique. An explicit endpoint/target remains available to the caller.
        if len(discovered) == 1 and not budget_stopped:
            return discovered[0], None
        code = 'ambiguous_target' if len(discovered) > 1 else 'cdp_probe_incomplete' if budget_stopped else 'cdp_not_discovered'
        reason = 'multiple_page_candidates' if len(discovered) > 1 else 'probe_budget_reached' if budget_stopped else 'no_listening_sockets' if not seen else 'no_page_targets' if any(a['ok'] for a in attempts) else 'no_valid_cdp_response'
        return None, {'backend':'cdp', 'ok':False,
                      'error':{'code':code, 'message':'Existing CDP discovery was inconclusive; this does not establish that the app lacks CDP support or only needs a startup flag. Use an explicit endpoint and target when known.'},
                      'reason':reason, 'scope':'selected_window_pid_listeners_only',
                      'other_processes_checked':False, 'app_restarted':False,
                      'attempts':attempts, 'candidate_count':len(discovered),
                      'budget':{'max_listeners':8, 'seconds':2, 'stopped':budget_stopped}}

    def _collect(self, args):
        if args.get("root_ref"):
            record=self.refs.get(args["root_ref"])
            if not record or time.monotonic()-record["seen"]>600:
                return None, {}, [error("stale_ref","The subtree reference expired. Inspect again.")]
            if record["backend"] not in ("uia3","uia","msaa"):
                return None, {}, [error("unsupported_scope","This channel does not support ref-based subtree reads.")]
            entry=self.workers.get(record["backend"])
            if not entry or entry[1]!=record["generation"] or entry[0].process.poll() is not None:
                return None, {}, [error("stale_ref","The source worker was replaced.")]
            if args.get("window") not in (None,record["window"]):
                return None, {}, [error("invalid_target","The scope belongs to a different window.")]
            args={**args,"backend":record["backend"],"window":record["window"],"root_ref":record["native_ref"]}
        backend = args.get("backend", "auto")
        if backend not in BACKENDS:
            return None, {}, [error("invalid_arguments", "Unknown backend")]
        timeout = float(args.get("probe_timeout", 6))
        if not 1 <= timeout <= 15:
            return None, {}, [error("invalid_arguments", "probe_timeout must be 1..15 seconds")]
        info = {}
        if args.get("window"):
            description = self._describe(args)
            if not description.get("ok"):
                return None, {}, [description]
            info = description["data"]
        elif backend != "cdp" and not args.get("endpoint"):
            return None, {}, [error("invalid_arguments", "Specify a current window handle, or an explicit CDP endpoint and target.")]
        native_args = {k: v for k, v in args.items() if k not in ("since", "refresh", "backend", "probe_timeout", "name", "role", "automation_id", "contains")}
        native_args["actionable"] = False
        requested = [backend] if backend != "auto" else (["uia3"] if args.get("within") else ["win32", "uia3", "msaa"] if info else [])
        if args.get("diagnostic_all") and backend=="auto" and "uia" not in requested:
            requested.append("uia")
        diagnostics = []
        cdp_args = None
        if args.get("endpoint"):
            cdp_args = {**native_args, "endpoint": args["endpoint"], "target": args.get("target")}
        elif not args.get("within") and backend in ("auto", "cdp") and info:
            cdp_args, diag = self._discover_cdp(info)
            if diag:
                diagnostics.append(diag)
            if cdp_args:
                cdp_args = {**native_args, **cdp_args}
        if cdp_args and backend in ("auto", "cdp") and "cdp" not in requested:
            requested.append("cdp")
        if backend == "cdp" and cdp_args is None:
            return info, {}, diagnostics
        identity = (info.get("window"), info.get("pid"))
        if args.get("refresh"):
            self.cooldowns = {k: v for k, v in self.cooldowns.items() if k[:2] != identity}
            self.primed.discard(identity)
        results = {}
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {}
            for channel in requested:
                cooldown = self.cooldowns.get((*identity, channel), 0)
                if time.monotonic() < cooldown and backend == "auto":
                    diagnostics.append({"backend": channel, "ok": False, "error": {"code": "channel_cooldown", "message": "A recent observation timed out. Use probe --refresh to try again."}})
                    continue
                channel_args = dict(cdp_args if channel == "cdp" else native_args)
                if channel in ('uia','uia3','win32') and 'depth' in channel_args:
                    channel_args['depth'] = min(channel_args['depth'],20)
                futures[channel] = pool.submit(self._call, channel, "inspect", channel_args, timeout)
            for channel, future in futures.items():
                result = future.result()
                result["backend"] = channel
                if result.get("ok"):
                    result["quality"] = metrics(result["data"].get("elements", []))
                    result["context"] = cdp_args if channel == "cdp" else {}
                    results[channel] = result
                elif result.get("error", {}).get("code") in ("timeout", "provider_timeout", "channel_unavailable"):
                    self.cooldowns[(*identity, channel)] = time.monotonic() + 60
                diagnostics.append({k: v for k, v in result.items() if k not in ("data", "context", "id")})
        # Accessibility bridges can start lazily after the first MSAA/UIA client
        # connects. One bounded read-only warm-up avoids concluding "unsupported"
        # from a cold container-only tree. Never repeat a mutating operation.
        initial=results.get("uia3")
        other_actions=any(r["quality"]["interactive"] for b,r in results.items() if b!="uia3")
        if backend=="auto" and identity not in self.primed and initial and initial["quality"]["nodes"]<=12 and not initial["quality"]["interactive"] and (other_actions or "Chromium" in info.get("framework_hints",[])):
            self.primed.add(identity)
            time.sleep(.1)
            warm=self._call("uia3","inspect",{**native_args,'depth':min(native_args.get('depth',6),20)},timeout)
            warm["backend"]="uia3"
            diag={"backend":"uia3","phase":"read_only_warmup","ok":warm.get("ok"),"roundtrip_ms":warm.get("roundtrip_ms")}
            if warm.get("ok"):
                warm["quality"]=metrics(warm["data"].get("elements",[]))
                warm["context"]={}
                diag["quality"]=warm["quality"]
                score=lambda r:(r["quality"]["named_interactive"],r["quality"]["named"],r["quality"]["interactive"],r["quality"]["nodes"])
                if score(warm)>score(initial):
                    results["uia3"]=warm
            else:
                diag["error"]=warm.get("error")
                if self.workers["uia3"][0].process.poll() is not None:
                    results.pop("uia3",None)
                    self.cooldowns[(*identity,"uia3")]=time.monotonic()+60
            diagnostics.append(diag)
        return info, results, diagnostics

    def _reference(self, backend, native_ref, row, args, context):
        generation = self.workers[backend][1] if backend != "cdp" else "cdp"
        key = (backend, generation, native_ref)
        if key not in self.reverse_refs:
            self.sequence += 1
            self.reverse_refs[key] = f"e{self.nonce}.{self.sequence}"
        ref = self.reverse_refs[key]
        self.refs[ref] = {"backend": backend, "native_ref": native_ref, "generation": generation, "seen": time.monotonic(), "window": args.get("window"), "row": row, "context": {k: context[k] for k in ("endpoint", "target") if k in context}}
        return ref

    def _inspect(self, args):
        if args.get("root_ref") and not args.get("window") and args["root_ref"] in self.refs:
            args={**args,"window":self.refs[args["root_ref"]]["window"]}
        info, results, diagnostics = self._collect(args)
        if not results:
            first=diagnostics[0].get("error",{}) if diagnostics else {}
            precise=args.get("root_ref") or args.get("backend","auto")!="auto"
            return {**error(first.get("code","no_semantic_channel") if precise else "no_semantic_channel", first.get("message","No channel returned a usable observation.") if precise else "No channel returned a usable observation."), "data": {"channels": diagnostics}}
        combined = []
        # A rich tree is placed first; weaker duplicate representations can be
        # suppressed without changing the origin of any executable reference.
        ordered = sorted(results, key=lambda b: (results[b]["quality"]["interactive"], results[b]["quality"]["named"]), reverse=True)
        for backend in ordered:
            result = results[backend]
            rows = result["data"].get("elements", [])
            aliases = {r["ref"]: self._reference(backend, r["ref"], r, args, result.get("context", {})) for r in rows}
            for row in rows:
                combined.append({**row, "ref": aliases[row["ref"]], "parent": aliases.get(row.get("parent")), "backend": backend})
        # GUI catalogs aggregate later, retaining each provider's exact binding.
        # Legacy desktop callers can keep their older compact observation.
        if args.get('preserve_bindings'):
            duplicates=0
        else: combined, duplicates = deduplicate(combined)
        expired = {ref for ref, record in self.refs.items() if time.monotonic()-record["seen"]>600}
        if len(self.refs)-len(expired)>20000:
            expired.update(sorted((r for r in self.refs if r not in expired), key=lambda r:self.refs[r]["seen"])[:len(self.refs)-len(expired)-20000])
        for ref in expired:
            del self.refs[ref]
        if expired:
            self.reverse_refs = {k:v for k,v in self.reverse_refs.items() if v not in expired}
        if args.get("actionable", True):
            combined = [r for r in combined if set(r.get("actions", [])) - {"get", "focus"} or not r.get("parent")]
        scope = json.dumps({k: args.get(k) for k in ("window", "backend", "within", "root_ref", "time_budget_ms", "view", "depth", "limit", "values", "actionable", "endpoint", "target")}, sort_keys=True)
        current = {r["ref"]: r for r in combined}
        snapshot = uuid.uuid4().hex
        data = {"window": args.get("window"), "snapshot": snapshot, "elements": combined, "channels": diagnostics, "quality": metrics(combined), "duplicates_suppressed": duplicates, "truncated": any(r["data"].get("truncated", False) for r in results.values()), "coverage": "partial; human-equivalent completeness has not been measured"}
        since = args.get("since")
        if since:
            old = self.snapshots.get(since)
            if not old or old["scope"] != scope:
                return error("unknown_snapshot", "Use a snapshot from this session with exactly the same scope.")
            if old["channels"] != set(results):
                return error("incomparable_snapshot", "Channel availability changed. Take a new full observation; missing channels must not be mistaken for removed controls.")
            if old["truncated"] or data["truncated"]:
                return error("incomparable_snapshot","Partial trees cannot reliably identify removed controls. Narrow the scope before comparing snapshots.")
            data.pop("elements")
            data.update(added=[r for ref, r in current.items() if ref not in old["rows"]], removed=[ref for ref in old["rows"] if ref not in current], changed=[r for ref, r in current.items() if ref in old["rows"] and old["rows"][ref] != r])
        self.snapshots[snapshot] = {"scope": scope, "rows": current, "channels": set(results), "truncated":data["truncated"]}
        while len(self.snapshots) > 16:
            del self.snapshots[next(iter(self.snapshots))]
        return {"ok": True, "data": data, "backend": "router"}

    def _action(self, op, args):
        ref = args.get("ref")
        if not ref:
            # Keep efficient, scoped UIA selectors available for one-shot CLI use.
            if args.get("within") or args.get("backend") in ("uia", "uia3"):
                return self._call("uia" if args.get("backend")=="uia" else "uia3", op, args)
            if args.get("css") and args.get("endpoint"):
                return self._call("cdp", op, args)
            if not any(args.get(f) is not None for f in ("automation_id", "name")):
                return error("invalid_target", "Use an inspected ref or an exact name/automation_id selector.")
            observed = self._inspect({**args, "actionable": False})
            if not observed.get("ok"):
                return observed
            candidates = [r for r in observed["data"]["elements"] if matches(r, args) and op in r.get("actions", [])]
            if len(candidates) != 1:
                return {**error("ambiguous_target" if candidates else "element_not_found", "The selector must match exactly one capable control. Use a session ref or specify backend/role/within."), "data": {"matches": candidates}}
            ref = candidates[0]["ref"]
        record = self.refs.get(ref)
        if not record or time.monotonic() - record["seen"] > 600:
            return error("stale_ref", "References belong to this session; inspect again.")
        if op not in record["row"].get("actions", []):
            return error("protected_control" if record["row"].get("password") else "unsupported", "The observed control does not advertise this action. No alternate action was attempted.")
        backend = record["backend"]
        if backend != "cdp":
            entry = self.workers.get(backend)
            if not entry or entry[1] != record["generation"] or entry[0].process.poll() is not None:
                return error("stale_ref", "The source worker was replaced. Inspect again before acting.")
        return self._call(backend, op, {**args, **record["context"], "ref": record["native_ref"]})

    def _input(self, args):
        from object_bridge import Fault
        ref = args.get('ref')
        record = self.refs.get(ref)
        if not record or time.monotonic()-record['seen'] > 600:
            return error('stale_ref', 'Observe a native control before sending input')
        backend = record['backend']
        if backend not in ('uia3','uia','msaa','win32'):
            return error('unsupported', 'Native input requires a control discovered by a native provider')
        entry = self.workers.get(backend)
        if not entry or entry[1] != record['generation'] or entry[0].process.poll() is not None:
            return error('stale_ref', 'The original provider was replaced; observe again')
        def require(result):
            if not result.get('ok'):
                raise Fault(result['error']['code'], result['error']['message'])
            return result['data']
        target = {'ref':record['native_ref']}
        try:
            guards = []
            for guard_ref in args.get('guard_refs',[]):
                guard = self.refs.get(guard_ref)
                if not guard or guard.get('window') != record.get('window') or time.monotonic()-guard['seen'] > 600:
                    raise Fault('stale_ref','Input guard is no longer bound to this window')
                provider = self.workers.get(guard['backend'])
                if not provider or provider[1] != guard['generation'] or provider[0].process.poll() is not None:
                    raise Fault('stale_ref','Input guard provider was replaced')
                guards.append(guard)
            def inspect():
                for guard in guards:
                    current=require(self._call(guard['backend'],'input_target',{'ref':guard['native_ref']},5))
                    if current.get('name') != guard['row'].get('name','') or current.get('role') != guard['row'].get('role'):
                        raise Fault('context_changed','An input guard changed name/role; no further input was sent')
                return require(self._call(backend,'input_target',target,5))
            context = require(self._call('win32','ping',{})).get('desktop',{})
            if not context.get('interactive_confirmed'):
                raise Fault('desktop_not_interactive', 'The unlocked interactive desktop was not confirmed')
            if self.input_driver is None:
                from gui_input import NativeInput
                self.input_driver = (self.input_factory or NativeInput)()
            data = self.input_driver.execute(args.get('method'), args.get('arguments',{}),
                inspect,
                lambda: require(self._call(backend,'focus',target,5)))
            return {'ok':True,'data':data}
        except Fault as exc:
            return error(exc.code,str(exc))

    def _request(self, op, args):
        if op=='escape':
            from gui_input import NativeInput
            from object_bridge import Fault
            try:
                state=self._call('win32','window',{**args,'state':'get'})
                if not state.get('ok'): return state
                if state['data'].get('pid')!=args.get('pid'): return error('stale_process','Window identity changed')
                context=self._call('win32','ping',{})
                if not context.get('ok') or not context['data'].get('desktop',{}).get('interactive_confirmed'):
                    return error('desktop_not_interactive','Unlocked interactive desktop was not confirmed')
                if self.input_driver is None: self.input_driver=(self.input_factory or NativeInput)()
                return {'ok':True,'data':self.input_driver.window_escape(args['window'],
                    lambda:self._call('win32','window',{**args,'state':'get'}))}
            except Fault as exc: return error(exc.code,str(exc))
        if op in ('request_accessibility','read_console','dismiss_popup'):
            return self._call('win32',op,args)
        if op == 'input': return self._input(args)
        if op in ("apps", "window", "ping"):
            result=self._call("win32", op, args)
            if op=="window" and args.get("state","get")!="get":
                self.metadata.pop(args.get("window"),None)
            return result
        if op == "describe":
            return self._describe(args)
        if op == "targets":
            return self._call("cdp", op, args)
        if op == "probe":
            info, results, diagnostics = self._collect({**args, "actionable": False, "diagnostic_all": bool(args.get("compare_legacy"))})
            ranked = sorted(results, key=lambda b: (results[b]["quality"]["interactive"], results[b]["quality"]["named"]), reverse=True)
            useful = any(r["quality"]["content_interactive"] for r in results.values())
            response = {"ok": bool(results), "backend": "router", "data": {"window": info, "channels": diagnostics, "preferred_backend": ranked[0] if ranked else None, "status": "semantic_controls_found" if useful else "structure_only" if results else "no_channel", "coverage": "unmeasured; node count is not human-equivalent coverage", "next_steps": [] if useful else ["Check for an application/framework accessibility bridge or an existing supported scripting interface.", "Pixel-only regions remain unresolved; no screenshots or guessed clicks were used."]}}
            if not results:
                response["error"] = {"code":"no_semantic_channel", "message":"No channel returned an observation."}
            return response
        if op in ("inspect", "find"):
            result = self._inspect(args)
            if op == "find" and result.get("ok"):
                if not any(args.get(k) is not None for k in ("name", "role", "automation_id")):
                    return error("invalid_arguments", "find requires name, role, or automation_id")
                result["data"]["elements"] = [r for r in result["data"]["elements"] if matches(r, args)]
                result["data"]["matches"] = len(result["data"]["elements"])
            return result
        if op in ACTIONS:
            return self._action(op, args)
        if op == "watch":
            backend = args.get("backend", "auto")
            if backend == "auto":
                backend = "cdp" if args.get("endpoint") else "win32"
            result = self._call(backend, op, args, 8)
            if result.get("ok"):
                self.watch_context = (backend, args) if args.get("enabled", True) else None
            return result
        if op == "events":
            if not self.watch_context:
                return error("watch_required", "Subscribe with watch in the same session first.")
            backend, context = self.watch_context
            return self._call(backend, op, {**context, **{k: v for k, v in args.items() if k in ("timeout_ms", "limit")}}, 8)
        return error("unknown_command", "Unknown command: " + op)

    def request(self, op, args=None, request_id=None):
        started = time.perf_counter()
        try:
            result = self._request(op, args or {})
        except (ValueError, TypeError, KeyError) as exc:
            result = error("invalid_arguments", str(exc))
        result["id"] = request_id
        result["roundtrip_ms"] = round((time.perf_counter() - started) * 1000, 2)
        return result

    def close(self):
        for worker, _ in self.workers.values():
            worker.close()
        self.cdp.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
