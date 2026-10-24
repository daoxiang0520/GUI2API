"""Bounded exploration of business objects through an existing object session.

This layer contains no application names or runtime-specific reflection. Entry
points still come from a provider or an explicitly attached object. Metadata is
the default; reading properties and calling declared read methods are opt-in.
"""
from collections import deque
import hashlib
import json
import math
import time


def explore(session, *, target=None, ref=None, query="", read_members=None,
            call_read_methods=False, max_objects=32, max_depth=3,
            max_requests=128, max_seconds=15, max_members=256):
    if (target is None) == (ref is None):
        raise ValueError("explore requires exactly one target or current object ref")
    if not isinstance(target if target is not None else ref, str):
        raise ValueError("The target or ref must be a string")
    for name, value, low, high in (
        ("max_objects", max_objects, 1, 256), ("max_depth", max_depth, 0, 8),
        ("max_requests", max_requests, 1, 1024), ("max_members", max_members, 1, 1024),
    ):
        if type(value) is not int or not low <= value <= high:
            raise ValueError(f"{name} must be {low}..{high}")
    if type(max_seconds) not in (int, float) or not math.isfinite(max_seconds) or not .05 <= max_seconds <= 60:
        raise ValueError("max_seconds must be .05..60 (a deadline between requests)")
    if not isinstance(query, str) or len(query) > 256:
        raise ValueError("query must be a string of at most 256 characters")
    if type(call_read_methods) is not bool:
        raise ValueError("call_read_methods must be boolean")
    read_members = [] if read_members is None else read_members
    if not isinstance(read_members, list) or len(read_members) > 64 or any(not isinstance(v, str) or not v or len(v) > 256 for v in read_members):
        raise ValueError("read_members must be at most 64 exact advertised member names")
    selected_reads = set(read_members)
    started = time.monotonic()
    report = {
        "format": "appctl.exploration/1", "target": target,
        "scope": "objects reachable from the selected provider or attached reference",
        "policy": {"read_members": sorted(selected_reads), "call_read_methods": call_read_methods,
                   "method_name_implies_read_only": False, "literal_values_saved": False,
                   "property_getters_may_execute_code": True, "retries": False},
        "limits": {"objects": max_objects, "depth": max_depth, "requests": max_requests,
                   "seconds_between_requests": max_seconds, "members_per_object": max_members},
        "roots": [], "nodes": [], "edges": [], "matches": [], "errors": [],
        "stopped_by": [], "deferred": {},
        "statistics": {"requests": 0, "descriptions": 0, "property_reads": 0, "declared_read_calls": 0},
        "arbitrary_process_attach": False, "business_semantics_verified": False,
        "references": "valid only in this live object session",
    }
    nodes, pending = {}, deque()
    halted = False

    def stopped(reason):
        if reason not in report["stopped_by"]:
            report["stopped_by"].append(reason)

    def defer(reason):
        report["deferred"][reason] = report["deferred"].get(reason, 0) + 1

    def request(op, args):
        nonlocal halted
        if halted:
            return None
        if report["statistics"]["requests"] >= max_requests:
            stopped("max_requests")
            halted = True
            return None
        if time.monotonic() - started >= max_seconds:
            stopped("max_seconds")
            halted = True
            return None
        report["statistics"]["requests"] += 1
        result = session.request(op, args)
        if not result.get("ok"):
            report["errors"].append({"operation": op, "ref": args.get("ref"),
                                     "member": args.get("member"), "error": result.get("error", {})})
            # A failed getter or method can already have changed application
            # state. Do not compound an unknown result with further exploration.
            if op in ("get", "call") or result.get("error", {}).get("code") in (
                "outcome_unknown", "provider_timeout", "protocol_error", "provider_stopped",
                "provider_unavailable", "stale_ref",
            ):
                halted = True
                stopped("operation_failed")
        return result

    def retain(value, depth, path):
        if value.get("kind") != "object" or not isinstance(value.get("ref"), str):
            return None
        identity = value["ref"]
        if identity in nodes:
            if path not in nodes[identity]["paths"] and len(nodes[identity]["paths"]) < 8:
                nodes[identity]["paths"].append(path)
            return identity
        if len(nodes) >= max_objects:
            stopped("max_objects")
            defer("object_not_retained")
            return None
        node = {"ref": identity, "type": value.get("type", "unknown"), "depth": depth,
                "paths": [path], "described": False, "properties": [], "methods": [], "events": []}
        nodes[identity] = node
        report["nodes"].append(node)
        pending.append(identity)
        return identity

    if target is not None:
        initial = request("list", {"target": target})
        if initial is None or not initial.get("ok"):
            return initial or {"ok": False, "error": {"code": "exploration_budget", "message": "No request budget for entry discovery"}}
        for root in initial["data"]["objects"]:
            report["roots"].append({"name": root["name"], "ref": root["ref"]})
            retain(root, 0, [root["name"]])
    else:
        report["roots"].append({"name": "attached", "ref": ref})
        retain({"kind": "object", "ref": ref}, 0, ["attached"])

    tokens = list(dict.fromkeys(query.casefold().split()))

    def match(identity, category, member, text):
        hits = [token for token in tokens if token in text.casefold()]
        if hits:
            report["matches"].append({"ref": identity, "category": category, "member": member,
                                      "matched_terms": hits, "evidence": "metadata_text_match",
                                      "business_meaning_verified": False})

    while pending and not halted:
        identity = pending.popleft()
        node = nodes[identity]
        response = request("describe", {"ref": identity})
        if response is None:
            break
        if not response.get("ok"):
            continue
        metadata = response["data"]
        report["statistics"]["descriptions"] += 1
        node["described"] = True
        node["type"] = metadata.get("type", node["type"])
        for key in ("observation_only", "source_pid", "captured_at", "source_backend", "source_scope", "source_stopped_by", "capabilities", "virtual_slots"):
            if key in metadata:
                node[key] = metadata[key]
        if metadata.get('observation_only'):
            report['observation_only'] = True
            for reason in metadata.get('source_stopped_by', []):
                stopped('source_' + str(reason))
        match(identity, "object", None, node["type"])
        left = max_members
        for category in ("properties", "methods", "events"):
            members = metadata.get(category, [])
            node[category] = members[:left]
            left -= len(node[category])
            if len(members) > len(node[category]):
                stopped("max_members")
            for member in node[category]:
                parts = [member.get(key, "") for key in ("name", "member", "type", "returns", "signature")]
                for parameter in member.get("parameters", []):
                    parts.extend([parameter.get("name", ""), parameter.get("type", "")])
                match(identity, category, member["name"], " ".join(str(part) for part in parts))
        fingerprint = {key: node[key] for key in ("type", "properties", "methods", "events")}
        node["metadata_fingerprint"] = hashlib.sha256(json.dumps(fingerprint, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:24]
        # Providers may expose structural relationships directly from metadata,
        # without executing any getter or method. Treat only object envelopes as
        # references, and apply the same depth/object budgets as explicit reads.
        for relationship in metadata.get('relationships', [])[:max_members]:
            other = relationship.get('object', {})
            if other.get('kind') != 'object':
                continue
            if node['depth'] >= max_depth and other.get('ref') not in nodes:
                stopped('max_depth')
                defer('metadata_relationship_at_depth_limit')
                continue
            path = node['paths'][0] + [relationship.get('name', relationship.get('relation', 'related'))]
            related = retain(other, node['depth'] + 1, path)
            report['edges'].append({'from': identity, 'to': other.get('ref'), 'operation': 'describe',
                                    'relation': relationship.get('relation', 'related'), 'evidence': 'metadata_relationship',
                                    'result_kind': 'object', 'retained': related is not None})
        actions = []
        for prop in node["properties"]:
            if prop["name"] in selected_reads and prop.get("readable"):
                actions.append(("get", prop))
            elif prop.get("readable"):
                defer("property_read_not_selected")
        for method in node["methods"]:
            if not call_read_methods:
                defer("method_calls_disabled")
            elif method.get("effect") != "read":
                defer("method_effect_not_declared_read")
            elif method.get("signature_available") is False or not isinstance(method.get("parameters"), list) or any(p.get("required", True) for p in method["parameters"]):
                defer("method_requires_arguments_or_signature_unknown")
            else:
                actions.append(("call", method))
        if actions and node["depth"] >= max_depth:
            stopped("max_depth")
            defer("actions_at_depth_limit")
            continue
        for op, member in actions:
            args = {"ref": identity, "member": member["name"]}
            if op == "call":
                args["arguments"] = []
            result = request(op, args)
            if result is None:
                break
            statistic = "property_reads" if op == "get" else "declared_read_calls"
            report["statistics"][statistic] += 1
            if not result.get("ok"):
                break
            value = result["data"].get("result", {})
            edge = {"from": identity, "member": member["name"], "operation": op,
                    "evidence": "property_return" if op == "get" else "declared_read_method_return",
                    "result_kind": value.get("kind", "unknown")}
            if value.get("kind") == "object":
                path = node["paths"][0] + [member["name"] + ("()" if op == "call" else "")]
                child = retain(value, node["depth"] + 1, path)
                edge.update(to=value.get("ref"), retained=child is not None)
            elif value.get("kind") == "value":
                # Never interpret application JSON as an object envelope.
                edge["value_type"] = type(value.get("value")).__name__
            report["edges"].append(edge)
            if halted:
                break
    report["statistics"]["objects"] = len(nodes)
    report["statistics"]["elapsed_seconds"] = round(time.monotonic() - started, 3)
    report["status"] = "partial" if report["errors"] or report["stopped_by"] else "metadata_explored"
    report["entry_found"] = bool(report["roots"])
    return {"ok": True, "data": report}
