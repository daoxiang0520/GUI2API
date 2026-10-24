"""Bounded, metadata-first discovery shared by GUI, CLI, MCP and DSH.

Live bindings stay in this manager. The disk cache contains fingerprints,
channel preferences and schema hashes, never object references or UI text.
Discovery does not certify a business meaning or execute candidate methods.
"""
from __future__ import annotations

from collections import OrderedDict
import copy
import hashlib
import inspect
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

from object_bridge import Fault

ROOT = Path(__file__).resolve().parent
FORMAT = "appctl.discovery/1"
NO_ARGS = {"type": "object", "properties": {}, "additionalProperties": False}
STALE_CODES = {"stale_ref", "stale_process", "provider_stopped", "provider_unavailable",
               "provider_timeout", "channel_unavailable", "runtime_unavailable", "outcome_unknown"}


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    allow_nan=False).encode()).hexdigest()[:24]


def file_stamp(path):
    if not path:
        return [None]
    try:
        path = Path(path).resolve()
        stat = path.stat()
        return [str(path).casefold(), stat.st_size, stat.st_mtime_ns]
    except (OSError, ValueError, TypeError):
        return [str(path), "unavailable"]


def contract(metadata, maximum=256):
    left, result = maximum, {"type": metadata.get("type", "unknown")}
    for field in ("properties", "methods", "events"):
        result[field] = metadata.get(field, [])[:left]
        left -= len(result[field])
    return digest(result)


def checked(response):
    if not response.get("ok"):
        error = response.get("error", {})
        raise Fault(error.get("code", "discovery_failed"), error.get("message", "Discovery failed"))
    return response["data"]


class MetadataCache:
    """Best-effort bounded hints. A cache entry can never authorize a call."""
    def __init__(self, path=None):
        self.path = Path(path or os.environ.get("GUI2API_DISCOVERY_CACHE", ROOT / ".cache/discovery.json"))
        self.records = {}
        try:
            if self.path.stat().st_size <= 1024 * 1024:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if data.get("format") == "appctl.discovery-cache/1" and isinstance(data.get("records"), dict):
                    self.records = dict(list(data["records"].items())[-128:])
        except (OSError, ValueError, TypeError):
            pass

    def hint(self, fingerprint):
        row = self.records.get(fingerprint, {})
        return row.get("preferred_backend") if isinstance(row, dict) else None

    def remember(self, record, preferred=None):
        # This explicit allowlist also excludes labels, URLs, function arguments,
        # raw addresses, HWNDs, PIDs, refs and application values from persistence.
        self.records.pop(record["fingerprint"], None)
        self.records[record["fingerprint"]] = {
            "preferred_backend": preferred, "updated_at": time.time(),
            "capability_count": len(record["entries"]),
            "schema_hash": digest(sorted({digest(c["input_schema"]) for c in record["entries"]})),
        }
        self.records = dict(list(self.records.items())[-128:])
        temporary = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # Merge under a cross-process lock; busy caches never block discovery.
            with open(str(self.path) + ".lock", "a+b") as lock:
                lock.seek(0, 2)
                if not lock.tell():
                    lock.write(b"0"); lock.flush()
                lock.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                merged = {}
                try:
                    old = json.loads(self.path.read_text(encoding="utf-8"))
                    if old.get("format") == "appctl.discovery-cache/1":
                        merged = old.get("records", {})
                except (OSError, ValueError):
                    pass
                merged.update(self.records)
                merged = dict(sorted(merged.items(), key=lambda item: item[1].get("updated_at", 0))[-128:])
                temporary = self.path.with_name(self.path.name + "." + uuid.uuid4().hex + ".tmp")
                temporary.write_text(json.dumps({"format": "appctl.discovery-cache/1", "records": merged}), encoding="utf-8")
                temporary.replace(self.path)
                # Closing the file releases either platform's lock.
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        finally:
            if temporary and temporary.exists():
                temporary.unlink(missing_ok=True)


class ObjectProvider:
    """One runtime adapter for every registered object provider, including COM."""
    def __init__(self, session):
        self.session = session

    def probe(self, target, *, max_objects=32, max_depth=3, max_seconds=10):
        return checked(self.session.request("explore", {
            "target": target, "max_objects": max_objects, "max_depth": max_depth,
            "max_seconds": max_seconds, "max_requests": 128, "max_members": 256,
        }))

    def describe(self, ref):
        return checked(self.session.request("describe", {"ref": ref}))

    def invoke(self, binding, arguments):
        return self.session.request(binding["operation"], {
            "ref": binding["ref"], "member": binding["member"], **arguments})

    def subscribe(self, ref, names, enabled=True):
        return self.session.request("watch", {"ref": ref, "names": names, "enabled": enabled})

    def release(self):
        self.session.close()


class DiscoveryManager:
    def __init__(self, gui, *, object_config=None, cache_path=None, object_factory=None, registry=None):
        from object_client import ObjectSession
        self.gui = gui
        self.config_path = Path(object_config or os.environ.get("APPCTL_OBJECT_CONFIG", ROOT / "objects.local.json")).resolve()
        self.object_factory = object_factory or ObjectSession
        self._objects = self._provider = None
        self._config_stamp = None
        self.registry = registry
        self.cache = MetadataCache(cache_path)
        self.records, self.by_owner, self.capabilities = OrderedDict(), {}, {}
        self.window_providers = {}
        self.closed = False

    @property
    def objects(self):
        stamp = file_stamp(self.config_path)
        if self._objects is None or stamp != self._config_stamp:
            if self._objects:
                self._objects.close()
                for row in self.records.values():
                    if any(c["kind"] == "object" for c in row["entries"]):
                        self.invalidate(row["owner"], "provider_configuration_changed")
            self._objects = self.object_factory(self.config_path)
            self._provider = ObjectProvider(self._objects)
            self._config_stamp = stamp
        return self._objects

    def gui_fingerprint(self, source):
        target, info = source["target"], source.get("discovery_info", {})
        return digest({"runtime": source["runtime"], "image": file_stamp(target.get("exe", "")),
                       "framework_modules": sorted(info.get("framework_modules", [])),
                       "class": info.get("class_name"), "endpoint": target.get("endpoint"),
                       "page": target.get("target") if not target.get("exe") else None})

    def preferred_backend(self, source):
        if source.get("adapter") or source.get("backend", "auto") != "auto":
            return None
        hint = self.cache.hint(self.gui_fingerprint(source))
        return hint if hint in ("uia3", "msaa", "win32") else None

    def invalidate(self, owner, reason):
        key = self.by_owner.get(owner)
        if key in self.records:
            self.records[key]["invalidated"] = reason

    def mark_dirty(self, owner, reason):
        """An observation can age without revoking every live control binding."""
        key=self.by_owner.get(owner)
        if key in self.records: self.records[key]['needs_revalidation']=reason

    def release_source(self, source):
        """Detach releases the helpers belonging to that window, not just refs."""
        owner = 'gui:' + source
        record = self.records.get(self.by_owner.get(owner))
        targets = self.window_providers.pop(source, set())
        if record:
            self.invalidate(owner, 'source_detached')
            targets.update(self.capabilities[c['id']][2].get('target') for c in record['entries'] if c['id'] in self.capabilities)
        for target in targets:
            if target and target.startswith('window-model:') and self._objects:
                provider = self._objects.providers.pop(target, None)
                if provider: provider.close()
                self._objects.config['targets'].pop(target, None)
                self._objects.refs = {k:v for k,v in self._objects.refs.items() if v['target'] != target}
                self._objects.reverse = {k:v for k,v in self._objects.reverse.items() if k[0] != target}

    def _new(self, owner, fingerprint, request, reason):
        old = self.by_owner.get(owner)
        if old:
            previous = self.records.pop(old, None)
            if previous:
                for cap in previous["entries"]:
                    self.capabilities.pop(cap["id"], None)
        key = "d" + uuid.uuid4().hex[:16]
        record = {"id": key, "owner": owner, "fingerprint": fingerprint, "request": request,
                  "reason": reason, "created": time.monotonic(), "entries": [], "channels": [],
                  "truncated": False, "invalidated": None, "probe_done": False,
                  "business_calls_during_discovery": 0, "property_reads_during_discovery": 0}
        record.update(controls=[],sequence=0,needs_revalidation=None)
        self.records[key] = record
        self.by_owner[owner] = key
        while len(self.records) > 32:
            _, dropped = self.records.popitem(last=False)
            self.by_owner.pop(dropped["owner"], None)
            if dropped['owner'].startswith('gui:'):
                self.release_source(dropped['request']['source'])
            for cap in dropped["entries"]:
                self.capabilities.pop(cap["id"], None)
        return record

    def _add(self, record, *, binding, **entry):
        if entry.get('kind') == 'object':
            identity = tuple(binding.get(k) for k in ('target','ref','member','operation'))
            keys = record.setdefault('object_bindings', set())
            if identity in keys:
                return
            keys.add(identity)
        if entry.get('kind')!='control' and sum(c.get('kind')!='control' for c in record['entries']) >= 4096:
            record["truncated"] = True
            return
        record['sequence']=record.get('sequence',0)+1
        entry = {"id": record["id"] + "/" + str(record['sequence']),
                 "business_meaning_verified": False, "effect_verified": False,
                 "verification": "unverified", **entry}
        record["entries"].append(entry)
        self.capabilities[entry["id"]] = (record["id"], entry, binding)
        return entry

    def observe_gui(self, source, collection, *, rearm=False):
        """Called automatically after the first complete gui_find observation."""
        owner, fingerprint = "gui:" + source["id"], self.gui_fingerprint(source)
        old = self.records.get(self.by_owner.get(owner))
        if old and old.get('observation_id') == collection.get('observation_id',collection['created']) and not old["invalidated"] and old["fingerprint"] == fingerprint:
            return old
        reason = "first_use" if not old else old["invalidated"] or old.get('needs_revalidation') or ("version_changed" if old["fingerprint"] != fingerprint else "observation_changed")
        reusable=bool(old and not old['invalidated'] and old['fingerprint']==fingerprint)
        record = old if reusable else self._new(owner, fingerprint, {"source": source["id"]}, reason)
        previous={}
        for entry in list(record['entries']):
            if entry.get('kind')=='control':
                bound=self.capabilities.pop(entry['id'],(None,None,{}))[2]
                previous[(bound.get('ref'),bound.get('method'))]=entry
        prior_groups={cap['id']:control['id'] for control in record.get('controls',[]) for cap in control['operations']}
        for control in record.get('controls',[]): self.capabilities.pop(control['id'],None)
        record['entries']=[entry for entry in record['entries'] if entry.get('kind')!='control']
        record['controls']=[]; record['needs_revalidation']=None
        record['reason']=reason
        record["snapshot"] = collection["snapshot"]
        record["observation_created"] = collection["created"]
        record['observation_id']=collection.get('observation_id',collection['created'])
        record['events_cursor'] = collection.get('event_revision')
        record["truncated"] = collection["metadata"].get("truncated", False)
        record["channels"] = copy.deepcopy(collection["metadata"].get("channels", []))
        from gui_api import METHODS
        from gui_identity import annotate, group_controls, public_locator
        rows=list(collection['rows'].values())
        if any('fingerprint' not in row for row in rows): annotate(rows,source['target'].get('window'))
        groups=collection.get('controls') or group_controls(rows)
        if len(groups)>4096: record['truncated']=True
        for group in groups[:4096]:
            operations=[]
            for row in group['rows']:
                for method in row.get('methods',[]):
                    if method not in METHODS: continue
                    prior=previous.get((row['ref'],method))
                    stable=bool(prior and prior.get('role')==row.get('role','') and prior.get('evidence',{}).get('automation_id')==row.get('automation_id') and
                                (prior.get('name')==row.get('name','') or row.get('role') in ('Edit','ComboBox','Document','Group')))
                    if prior and prior.get('invalidated') and rearm: stable=False
                    cap=self._add(record, **({'id':prior['id']} if stable else {}),kind="control", name=row.get("name", ""), role=row.get("role", ""),
                          operation=method, provider=row.get("backend", source["runtime"]),
                          callable=True, binding_state="live_control", effect=METHODS[method]["effect"],
                          input_schema=METHODS[method]["inputSchema"],
                          reliability=row.get('invoke_reliability','unverified') if method=='invoke' else 'unverified',
                          recommended_method=row.get('recommended_method') if method=='invoke' else None,
                          evidence={"basis": "provider_label_role_and_advertised_method", "automation_id": row.get("automation_id"),
                                    "enabled": row.get("enabled"), "offscreen": row.get("offscreen")},
                          locator=public_locator(row),
                          binding={"source": source["id"], "ref": row["ref"], "method": method})
                    if stable and prior.get('invalidated'):
                        cap.update(invalidated=prior['invalidated'],callable=False,binding_state='quarantined')
                    operations.append(cap)
            primary=group['rows'][0]
            survived={prior_groups[cap['id']] for cap in operations if cap['id'] in prior_groups}
            group_id=next(iter(survived)) if len(survived)==1 else record['id']+'/control/'+digest([group['id'],sorted(cap['id'] for cap in operations)])
            control={**public_locator(primary),'id':group_id,'kind':'control_group',
                'callable':bool(operations),'binding_state':'live_control','verification':'unverified',
                'business_meaning_verified':False,'operations':operations,
                'methods':sorted({entry['operation'] for entry in operations}),
                'bindings':[public_locator(row) for row in group['rows']],
                'binding_count':len(group['rows']),'merge_evidence':'exact_role_bounds_and_label_or_shared_edit_handle',
                'identity_authorizes_input':False}
            control['aliases']=sorted({r.get('name','') for r in group['rows'] if r.get('name')})
            if not control.get('name') and control['aliases']: control['name']=control['aliases'][0]
            record['controls'].append(control)
            self.capabilities[control['id']]=(record['id'],control,{'source':source['id'],'operations':operations})
        successful = [c for c in record["channels"] if c.get("ok") and c.get("backend") in ("uia3", "msaa", "win32")]
        quality = lambda c: (c.get("quality", {}).get("named_interactive", 0), c.get("quality", {}).get("interactive", 0), c.get("quality", {}).get("named", 0))
        record["preferred_backend"] = max(successful, key=quality)["backend"] if successful else None
        self.cache.remember(record, record["preferred_backend"])
        return record

    def _graph_entries(self, record, graph, target):
        for node in graph["nodes"]:
            observation = node.get("observation_only", False)
            node_contract = contract(node)
            for category in ("properties", "methods", "events"):
                for member in node.get(category, []):
                    operations = ["call"] if category == "methods" else ["watch"] if category == "events" else ["get", "set"]
                    for op in operations:
                        available = member.get("callable", True) if op == "call" else member.get("observable", True) if op == "watch" else member.get("readable" if op == "get" else "writable", False)
                        if category == "properties" and not available and not observation:
                            continue
                        schema = NO_ARGS
                        if op == "set":
                            schema = {"type": "object", "properties": {"value": {}}, "required": ["value"], "additionalProperties": False}
                        elif op == "call":
                            schema = {"type": "object", "properties": {"arguments": {"type": "array", "maxItems": 64},
                                      "object_arguments":{"type":"object","additionalProperties":{"type":"string"},"maxProperties":64}}, "additionalProperties": False}
                        elif op == "watch":
                            schema = {"type": "object", "properties": {"enabled": {"type": "boolean"}}, "additionalProperties": False}
                        self._add(record, kind="object", name=member["name"], object_type=node["type"],
                                  operation=op, provider=target, callable=bool(available and not observation),
                                  binding_state="metadata_only" if observation else "live_object" if available else "unsupported_marshalling",
                                  effect="write" if op == "set" else member.get("effect", "unknown"),
                                  input_schema=schema,
                                  evidence={"basis": "runtime_metadata", "member": member, "paths": node.get("paths", []),
                                            "getter_executed": False, "observation_only": observation},
                                  binding={"target": target, "ref": node["ref"], "member": member["name"],
                                           "operation": op, "contract": node_contract})
        record["truncated"] |= graph.get("status") == "partial"
        record.setdefault("graphs", []).append({k: graph[k] for k in ("statistics", "stopped_by", "errors") if k in graph})

    def _target_fingerprint(self, target):
        objects = self.objects
        definition = objects.config["targets"].get(target)
        if definition is None:
            raise Fault("unknown_target", "Choose an explicitly configured object_target")
        files = []
        for arg in definition.get("command", [])[:32]:
            arg = arg.replace("{root}", str(ROOT)).replace("{config_dir}", str(self.config_path.parent)).replace("{python}", sys.executable)
            if Path(arg).is_file():
                files.append(file_stamp(arg))
        return digest({"definition": definition, "files": files}), definition

    def _object_record(self, target, refresh=False, **limits):
        fingerprint, definition = self._target_fingerprint(target)
        owner = "objects:" + target
        old = self.records.get(self.by_owner.get(owner))
        reason = "first_use" if not old else old["invalidated"] or ("refresh" if refresh else "expired_or_provider_changed")
        if old and old['probe_done'] and not refresh and not old["invalidated"] and old["fingerprint"] == fingerprint and old.get('limits') == limits and time.monotonic() - old["created"] < 600:
            try:
                for cap in old["entries"][:1]:
                    self.objects._reference(self.capabilities[cap["id"]][2]["ref"])
                return old, True
            except LookupError:
                reason = "provider_stopped"
        record = self._new(owner, fingerprint, {"object_target": target}, reason)
        record['limits'] = limits
        if target in self.objects.providers and self.objects.providers[target].process.poll() is not None:
            checked(self.objects.request("connect", {"target": target}))
            record["provider_reconnected"] = True
        graph = self._provider.probe(target, **limits)
        self._graph_entries(record, graph, target)
        record["channels"] = [{"provider": target, "runtime": definition.get("runtime"), "ok": True,
                               "scope": "configured_provider_roots", "provider_start_may_launch_application": True}]
        record["probe_done"] = True
        self.cache.remember(record)
        return record, False

    def _window_objects(self, record, source, max_seconds):
        """Probe the standard OBJID_NATIVEOM handshake, without an app ProgID."""
        if record["probe_done"]:
            return
        record["probe_done"] = True
        target = source["target"]
        if source.get("adapter") or not all(k in target for k in ("pid", "created_at", "window")):
            record["channels"].append({"provider": "com-native-model", "ok": False, "reason": "native_window_required"})
            return
        name = "window-model:" + digest({k: target[k] for k in ("pid", "created_at", "window")})
        objects = self.objects
        self.window_providers.setdefault(source['id'],set()).add(name)
        objects.config["targets"][name] = {"runtime": "com", "timeout": min(10, max_seconds),
            "command": ["{python}", "{root}/bridges/com_agent.py", "--window-tree", str(target["window"]), "--pid", str(target["pid"])]}
        try:
            graph = self._provider.probe(name, max_objects=16, max_seconds=min(10, max_seconds))
            self._graph_entries(record, graph, name)
            record["channels"].append({"provider": "com-native-model", "ok": True, "objects": len(graph["nodes"]),
                                        "scope": "selected_window_and_at_most_15_descendants"})
        except Fault as exc:
            record["channels"].append({"provider": "com-native-model", "ok": False,
                                        "error": {"code": exc.code, "message": str(exc)}})
            provider = objects.providers.pop(name, None)
            if provider:
                provider.close()
            self.window_providers.get(source['id'],set()).discard(name)

    def _native_metadata(self, record, source):
        target = source["target"]
        if not target.get("pid"):
            raise Fault("invalid_target", "Native metadata requires a bound Windows process")
        result = subprocess.run([sys.executable, str(ROOT / "bridges/native_probe.py"),
                                 str(target["pid"]), str(target["created_at"])],
                                capture_output=True, text=True, encoding="utf-8", timeout=12,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode or len(result.stdout) > 8 * 1024 * 1024:
            raise Fault("metadata_probe_failed", "The bounded native metadata helper failed")
        data = json.loads(result.stdout)
        record["channels"].append({"provider": "native-metadata", "ok": bool(data.get("ok")),
                                    "scope": data.get("scope"), "reason": data.get("reason"),
                                    "stopped_by": data.get("stopped_by", []), "callable": False})
        for cls in data.get("classes", [])[:256]:
            self._add(record, kind="native_metadata", name=cls["type"], operation="describe", provider="native-metadata",
                      callable=False, binding_state="metadata_only", effect="unknown", input_schema=NO_ARGS,
                      evidence={"basis": "native_runtime_metadata", "methods": cls.get("methods", [])[:128],
                                "properties": cls.get("properties", [])[:128], "signature_and_invocation_bound": False}, binding={})
        record["truncated"] |= bool(data.get("stopped_by"))
        record["native_metadata_done"] = True

    def discover(self, *, source=None, object_target=None, registered_app=None, refresh=False,
                 query="", limit=100, offset=0, native_metadata=False,
                 max_objects=32, max_depth=3, max_seconds=10, exact=False, role=None, enabled=None, onscreen=None, layout='operations'):
        if sum(value is not None for value in (source, object_target, registered_app)) != 1:
            raise Fault("invalid_arguments", "Choose exactly one source, object_target or registered_app")
        if type(refresh) is not bool or type(native_metadata) is not bool or not isinstance(query, str) or len(query) > 1024:
            raise Fault("invalid_arguments", "refresh/native_metadata must be booleans; query is at most 1024 characters")
        if type(exact) is not bool or role is not None and not isinstance(role,str) or any(v is not None and type(v) is not bool for v in (enabled,onscreen)):
            raise Fault('invalid_arguments','exact/enabled/onscreen must be boolean; role is a string')
        if layout not in ('controls','operations'): raise Fault('invalid_arguments','layout must be controls or operations')
        for value, minimum, maximum in ((limit, 1, 3000), (offset, 0, 20000), (max_objects, 1, 256), (max_depth, 0, 8)):
            if type(value) is not int or not minimum <= value <= maximum:
                raise Fault("invalid_arguments", "Invalid discovery page or graph budget")
        if type(max_seconds) not in (int, float) or not .1 <= max_seconds <= 60:
            raise Fault("invalid_arguments", "max_seconds must be .1..60 (checked between provider requests)")
        if native_metadata and source is None:
            raise Fault("invalid_arguments", "native_metadata requires a GUI source")
        if source is not None:
            bound = self.gui._source(source)
            old = self.records.get(self.by_owner.get("gui:" + source))
            if old and old["fingerprint"] != self.gui_fingerprint(bound):
                self.invalidate(old["owner"], "version_changed")
            cached = bool(old and not old["invalidated"] and not refresh)
            self.gui._find({"source": source, "refresh": refresh or bool(old and old["invalidated"]), "limit": 1},explicit=refresh)
            record = self.records[self.by_owner["gui:" + source]]
            cached &= record is old
            self._window_objects(record, bound, max_seconds)
            if native_metadata and not record.get("native_metadata_done"):
                self._native_metadata(record, bound)
            self.cache.remember(record, record.get("preferred_backend"))
        elif object_target is not None:
            if not isinstance(object_target, str) or not object_target or len(object_target) > 256:
                raise Fault("invalid_arguments", "object_target must be a configured target name")
            record, cached = self._object_record(object_target, refresh, max_objects=max_objects, max_depth=max_depth, max_seconds=max_seconds)
        else:
            from capability_registry import CapabilityRegistry
            if not isinstance(registered_app, str) or not registered_app or len(registered_app) > 128:
                raise Fault("invalid_arguments", "registered_app must be a registered application id")
            self.registry = self.registry or CapabilityRegistry()
            capabilities = self.registry.list(registered_app)["capabilities"]
            record = self._new("registry:" + registered_app, digest(capabilities), {"registered_app": registered_app}, "registry_read")
            for cap in capabilities:
                self._add(record, kind="registered", name=cap["title"], operation=cap["id"], provider=registered_app,
                          callable=cap["callable"], binding_state=cap["status"], effect=cap["effect"],
                          input_schema=cap["input_schema"], verification=cap["status"], evidence=cap["purpose"],
                          binding={"app_id": registered_app, "capability_id": cap["id"]})
            cached = False
        terms = query.casefold().split()
        def matches(entry):
            if role is not None and entry.get('role','').casefold()!=role.casefold(): return False
            locator=entry.get('locator',entry)
            if enabled is not None and locator.get('enabled')!=enabled: return False
            if onscreen is not None and locator.get('offscreen',False)==onscreen: return False
            aliases=[name.casefold() for name in entry.get('aliases',[])]
            if exact: return not query or query.casefold() in (entry.get('name','').casefold(),entry.get('automation_id','').casefold(),*aliases)
            text=json.dumps({key:entry.get(key) for key in ('name','object_type','role','operation','provider')},ensure_ascii=False).casefold()
            text+=' '+' '.join(aliases)
            return all(term in text for term in terms)
        rank=lambda entry:(entry.get('name','').casefold()!=query.casefold(),not entry.get('name','').casefold().startswith(query.casefold()),len(entry.get('name','')))
        entries=sorted([entry for entry in record['entries'] if matches(entry)],key=rank)
        controls=sorted([entry for entry in record.get('controls',[]) if matches(entry)],key=rank)
        compact_controls=[]
        for control in controls[offset:offset+limit]:
            compact={k:copy.deepcopy(v) for k,v in control.items() if k not in ('operations','bindings')}
            compact['operations']=[{k:entry[k] for k in ('id','operation','provider','effect','reliability','recommended_method','invalidated','callable','binding_state') if k in entry} for entry in control['operations']]
            compact['bindings']=[{k:binding[k] for k in ('ref','backend','hwnd','fingerprint') if k in binding} for binding in control['bindings']]
            compact_controls.append(compact)
        grouped=source is not None and layout=='controls'
        fields = ("id", "name", "object_type", "role", "kind", "operation", "provider", "callable", "binding_state", "effect", "verification",'locator')
        return {**self.summary(record), "cached": cached, "matched":len(controls) if grouped else len(entries), "offset": offset,
                'catalog_layout':'controls_with_operations' if source is not None else 'object_members',
                'controls':compact_controls,'matched_controls':len(controls),
                'controls_has_more':offset+limit<len(controls),
                'match_status':'found' if entries or controls else 'not_observed','absence_proven':False,
                'next_action':'choose_control_operation' if entries or controls else 'restore_request_accessibility_or_navigate_then_discover',
                "has_more": offset + limit < (len(controls) if grouped else len(entries)),
                "capabilities": [] if grouped else [{k: entry[k] for k in fields if k in entry} for entry in entries[offset:offset + limit]],
                'legacy_operations_included':not grouped,
                "query_evidence": "metadata_text_match; business meaning unverified", "references_valid_after_exit": False}

    def summary(self, record):
        controls=record.get('controls',[])
        return {"format": FORMAT, "catalog": record["id"], "fingerprint": record["fingerprint"],
                "trigger": record["reason"], "capability_count":len(controls) if controls else len(record['entries']),
                'control_count':len(controls),'readable_control_count':sum('get' in c['methods'] for c in controls),
                'writable_control_count':sum(bool(set(c['methods'])-{'get'}) for c in controls),
                'operation_count':len(record['entries']),'legacy_capabilities_layout':'operation_entries; prefer controls',
                "callable_count": sum(bool(c["callable"]) for c in record["entries"]),
                "channels": copy.deepcopy(record["channels"]), "truncated": record["truncated"],
                "preferred_backend": record.get('preferred_backend'),
                "exploration": copy.deepcopy(record.get('graphs', [])),
                "catalog_limits": {"live_catalogs":32,"controls_per_catalog":4096,'object_entries_per_catalog':4096,"reference_ttl_seconds":600},
                'invalidation_scope':'individual_control_after_window_observation','needs_revalidation':record.get('needs_revalidation'),
                "invalidated": record["invalidated"], "business_meaning_verified": False,
                "business_calls_during_discovery": 0, "property_reads_during_discovery": 0,
                "coverage": "provider-exposed objects only; human-equivalent coverage unmeasured"}

    def _selected(self, capability):
        if not isinstance(capability, str) or capability not in self.capabilities:
            raise Fault("stale_capability", "Rediscover; this capability is not bound in the current catalog/session")
        key, entry, binding = self.capabilities[capability]
        if entry.get('invalidated'):
            raise Fault('stale_capability','This operation is quarantined after '+entry['invalidated']+'; explicitly reobserve actual state before selecting a new operation')
        record = self.records[key]
        if record['owner'].startswith('gui:'):
            source = self.gui._source(record['request']['source'])
            self.gui._check_external(source)
            if entry['kind'] in ('control','control_group'):
                # Refresh metadata once after a window change. Preserve exact live
                # bindings; removed/replaced controls alone lose their IDs.
                self.gui._find({'source':source['id'],'limit':1})
                current=self.capabilities.get(capability)
                if current is None: raise Fault('stale_capability','This control/method was removed or replaced; rediscover')
                key,entry,binding=current; record=self.records[key]
        if record["invalidated"] or time.monotonic() - record["created"] >= 600:
            raise Fault("stale_capability", "Rediscover after " + str(record["invalidated"] or "catalog_expired"))
        if entry["kind"] in ("control",'control_group'):
            source = self.gui._source(binding["source"])
            if self.gui_fingerprint(source) != record["fingerprint"]:
                self.invalidate(record["owner"], "version_changed")
                raise Fault("stale_capability", "Application image changed; rediscover before invoking")
        elif entry["kind"] == "object":
            # The property getter itself can have side effects. Metadata is
            # checked first; read calls are never retried either.
            if record["owner"].startswith("objects:") and self._target_fingerprint(binding["target"])[0] != record["fingerprint"]:
                self.invalidate(record["owner"], "provider_changed")
                raise Fault("stale_capability", "Provider definition or code changed; rediscover")
            if record["owner"].startswith("gui:"):
                self.gui._source(record["request"]["source"])
            try:
                self.objects._reference(binding["ref"])
            except LookupError as exc:
                self.invalidate(record["owner"], "provider_stopped_or_ref_expired")
                raise Fault("stale_capability", "Object binding expired; rediscover before invoking") from exc
        return record, entry, binding

    def describe(self, capability):
        record, entry, _ = self._selected(capability)
        return {"catalog": record["id"], **copy.deepcopy(entry)}

    def invoke(self, capability, arguments=None, postcondition=None, method=None):
        record, entry, binding = self._selected(capability)
        if entry.get('invalidated'): raise Fault('stale_capability','This control operation needs a new observation after '+entry['invalidated'])
        if entry['kind']=='control_group':
            from gui_api import SHORTCUTS
            operation=SHORTCUTS.get(method,method)
            choices=[c for c in entry['operations'] if c['operation']==operation]
            if not choices: raise Fault('invalid_arguments','Choose a method advertised by this control group')
            choices.sort(key=lambda c:c.get('reliability')=='unreliable')
            # Every choice below names one exact provider. This is initial
            # selection, never fallback/replay after a dispatched operation.
            capability=choices[0]['id']; record,entry,binding=self._selected(capability)
        elif method is not None: raise Fault('invalid_arguments','method is only used with a grouped control id')
        if not entry["callable"]:
            raise Fault("not_callable", "Metadata is visible but this invocation channel is not connected or validated")
        arguments = {} if arguments is None else arguments
        if entry.get('kind')=='control' and entry.get('operation')=='set' and isinstance(arguments,dict) and 'text' in arguments:
            if 'value' in arguments: raise Fault('invalid_arguments','Use text or value, not both')
            arguments={**arguments,'value':arguments['text']}; del arguments['text']
        from jsonschema import Draft202012Validator
        try:
            json.dumps(arguments, allow_nan=False)
        except (ValueError, TypeError) as exc:
            raise Fault("invalid_arguments", "Arguments must be finite JSON values") from exc
        failures = list(Draft202012Validator(entry["input_schema"]).iter_errors(arguments))
        if failures:
            raise Fault("invalid_arguments", failures[0].message)
        # A postcondition is explicit caller evidence, never a guess generated
        # from a name. Validate it before dispatching the candidate operation.
        if postcondition is not None and entry['kind']!='control':
            if entry["kind"] != "object" or not isinstance(postcondition, dict) or set(postcondition) != {"member", "equals"} or not isinstance(postcondition["member"], str):
                raise Fault("invalid_arguments", "postcondition requires an object property member and equals")
            try:
                json.dumps(postcondition, allow_nan=False)
            except (TypeError, ValueError) as exc:
                raise Fault("invalid_arguments", "Postcondition must contain finite JSON values") from exc
        if entry["kind"] == "object":
            metadata = self._provider.describe(binding["ref"])
            if contract(metadata) != binding["contract"]:
                self.invalidate(record["owner"], "metadata_changed")
                raise Fault("stale_capability", "Runtime members changed; rediscover before invoking")
            if postcondition is not None:
                props = [p for p in metadata.get("properties", []) if p["name"] == postcondition["member"] and p.get("readable")]
                if len(props) != 1:
                    raise Fault("invalid_arguments", "Postcondition must name one readable property on the same object")
            if binding["operation"] == "watch":
                response = self._provider.subscribe(binding["ref"], [binding["member"]], arguments.get("enabled", True))
            else:
                response = self._provider.invoke(binding, arguments)
        elif entry["kind"] == "control":
            response = self.gui.request("call", {"ref": binding["ref"], "method": binding["method"], "arguments": arguments,'postcondition':postcondition})
        else:
            response = self.registry.request("invoke", {**binding, "arguments": arguments})
        if not response.get("ok"):
            # This also blocks a second call to a known-failed channel until a
            # new observation. Never switch providers and replay the operation.
            if entry['kind']=='control':
                current=self.capabilities.get(capability)
                if current: current[1].update(invalidated=response.get('error',{}).get('code','invocation_failed'),callable=False,binding_state='quarantined')
            else: self.invalidate(record["owner"], response.get("error", {}).get("code", "invocation_failed"))
            return {**response, "automatic_retries": 0}
        verification = {"status": "returned_unverified", "business_meaning_verified": False}
        if postcondition is not None and entry['kind']=='control':
            verification={'status':'postcondition_passed','business_meaning_verified':False,
                          'observation':response['data'].get('postcondition')}
        elif postcondition is not None:
            observation = self.objects.request("get", {"ref": binding["ref"], "member": postcondition["member"]})
            from capability_registry import same_json
            result = observation.get("data", {}).get("result", {})
            passed = bool(observation.get("ok") and result.get("kind") == "value" and same_json(result.get("value"), postcondition["equals"]))
            verification = {"status": "postcondition_passed" if passed else "postcondition_failed", "business_meaning_verified": False,
                            "property": postcondition["member"], "observation_ok": observation.get("ok", False)}
            if not passed:
                self.invalidate(record["owner"], "postcondition_failed")
                return {"ok": False, "data": response["data"], "verification": verification,
                        "error": {"code": "postcondition_failed", "message": "Operation returned; the explicit postcondition did not match. No retry."}, "automatic_retries": 0}
        followup = None
        result = response.get('data', {}).get('result', {})
        if entry['kind'] == 'object' and isinstance(result, dict) and result.get('kind') == 'object':
            # This is a protocol envelope, not kind:value application JSON.
            # Explore the returned instance's metadata without repeating the
            # operation that produced it or executing any of its getters.
            graph = self.objects.request('explore', {'ref':result['ref'],'max_objects':16,'max_depth':2,'max_requests':64,'max_seconds':2})
            if graph.get('ok'):
                before = len(record['entries'])
                self._graph_entries(record, graph['data'], binding['target'])
                followup = {'ok':True,'catalog':record['id'],'added_capabilities':len(record['entries'])-before,
                            'property_reads':0,'method_calls':0}
            else:
                followup = {'ok':False,'error':graph.get('error',{}),'operation_not_repeated':True}
        return {**response, "verification": verification, "automatic_retries": 0,
                **({'returned_object_discovery':followup} if followup is not None else {})}

    def events(self, catalog, timeout_ms=0, limit=100):
        if type(timeout_ms) is not int or not 0 <= timeout_ms <= 5000 or type(limit) is not int or not 1 <= limit <= 1000:
            raise Fault('invalid_arguments', 'timeout_ms must be 0..5000 and limit 1..1000')
        record = self.records.get(catalog)
        if record is None:
            raise Fault('stale_catalog', 'Rediscover; this catalog was replaced or is not in this session')
        if record and record['owner'].startswith('gui:'):
            source = self.gui._source(record['request']['source'])
            baseline = source.get('collection', {}).get('event_revision')
            state = self.gui._check_external(source, after=record.get('events_cursor', baseline), timeout_ms=timeout_ms)
            record['events_cursor'] = state['revision']
            rows = [{'kind':'gui_changed', 'source':source['id'], 'revision':state['revision']}] if state.get('changed') else []
            return {'ok':True, 'data':{'events':rows, 'dropped':0, 'invalidated':bool(record['invalidated']),
                                     'needs_revalidation':bool(record.get('needs_revalidation')),'scope':'selected_window_observation',
                                     'observation':state, 'contains_values':False}}
        if not record["owner"].startswith("objects:"):
            raise Fault("unsupported_events", "Use a configured object catalog and subscribe to its advertised event first")
        target = record["request"]["object_target"]
        response = self.objects.request("events", {"target": target, "timeout_ms": timeout_ms, "limit": limit})
        if response.get("ok") and (response["data"].get("events") or response["data"].get("dropped")):
            self.invalidate(record["owner"], "object_event")
        return response

    def request(self, op, args):
        if self.closed:
            raise Fault("session_closed", "Discovery manager is closed")
        functions = {'discover':self.discover,'discovery_describe':self.describe,
                     'discovery_invoke':self.invoke,'discovery_events':self.events}
        function = functions.get(op)
        if function is None:
            raise Fault('unknown_operation','Unknown discovery operation')
        try:
            inspect.signature(function).bind(**args)
        except TypeError as exc:
            raise Fault('invalid_arguments',str(exc)) from exc
        try:
            result = function(**args)
            return {'ok':True,'data':result} if op in ('discover','discovery_describe') else result
        except Fault as exc:
            if exc.code in STALE_CODES and args.get('capability') in self.capabilities:
                key = self.capabilities[args['capability']][0]
                self.invalidate(self.records[key]['owner'], exc.code)
            raise

    def close(self):
        if self._provider:
            self._provider.release()
        if self.registry:
            self.registry.close()
        self.records.clear(); self.capabilities.clear(); self.by_owner.clear()
        self.window_providers.clear()
        self.closed = True
