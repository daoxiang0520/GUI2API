"""Call application business objects through a framework-neutral protocol."""
import argparse
import json
import sys

from object_client import ObjectSession, error, schema


def parser():
    root = argparse.ArgumentParser(description=__doc__)
    root.add_argument("--config", help="Explicit local provider configuration")
    root.add_argument("--pretty", action="store_true")
    sub = root.add_subparsers(dest="op", required=True)
    for op in ("schema", "targets", "session"):
        sub.add_parser(op)
    discovery = sub.add_parser("discover")
    discovery.add_argument("--runtime", default="com")
    exploration = sub.add_parser("explore", help="Build a bounded object graph and search advertised member metadata")
    entry = exploration.add_mutually_exclusive_group(required=True)
    entry.add_argument("--target")
    entry.add_argument("--ref")
    exploration.add_argument("--query", default="", help="Metadata text terms; no business meaning is inferred")
    exploration.add_argument("--read-member", dest="read_members", action="append", default=[], help="Explicitly read this exact property name on explored objects; getters can run code")
    exploration.add_argument("--call-read-methods", action="store_true", help="Call methods declared effect=read that need no arguments; declarations are not behavior proofs")
    exploration.add_argument("--max-objects", type=int, default=32)
    exploration.add_argument("--max-depth", type=int, default=3)
    exploration.add_argument("--max-requests", type=int, default=128)
    exploration.add_argument("--max-seconds", type=float, default=15)
    exploration.add_argument("--max-members", type=int, default=256)
    attach = sub.add_parser("attach")
    attach.add_argument("--runtime", default="com")
    selection = attach.add_mutually_exclusive_group(required=True)
    selection.add_argument("--rot-id")
    selection.add_argument("--progid")
    attach.add_argument("--activate", action="store_true", help="Explicitly create a COM automation object; may start the application")
    for op in ("connect", "list", "events"):
        command = sub.add_parser(op)
        command.add_argument("--target", required=True)
        if op == "events":
            command.add_argument("--timeout-ms", type=int, default=0)
            command.add_argument("--limit", type=int, default=100)
    for op in ("describe", "get", "set", "call", "watch", "release"):
        command = sub.add_parser(op)
        command.add_argument("--ref")
        command.add_argument("--target", help="One-shot target, together with --object")
        command.add_argument("--object", help="Exact registered root name for a one-shot command")
        if op in ("get", "set", "call"):
            command.add_argument("--member", required=True)
        if op == "set":
            command.add_argument("--value", required=True, type=json.loads, help="JSON value, including JSON quotes for strings")
        if op == "call":
            command.add_argument("--arguments", type=json.loads, default=[])
            command.add_argument("--object-arguments", type=json.loads, default={})
        if op == "watch":
            command.add_argument("--names", type=json.loads)
            command.add_argument("--stop", dest="enabled", action="store_false", default=True)
    return root


def main(argv=None):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stdin.reconfigure(encoding="utf-8")
    args = vars(parser().parse_args(argv))
    config, pretty, op = args.pop("config"), args.pop("pretty"), args.pop("op")
    def emit(result):
        print(json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2 if pretty else None), flush=True)
    if op == "schema":
        emit({"ok": True, "data": schema()})
        return 0
    try:
        with ObjectSession(config) as session:
            if op == "session":
                for line in sys.stdin:
                    try:
                        request = json.loads(line)
                        if not isinstance(request, dict) or not isinstance(request.get("op"), str) or not isinstance(request.get("args", {}), dict):
                            raise ValueError("Expected op and object args")
                        emit(session.request(request["op"], request.get("args", {}), request.get("id")))
                    except ValueError as exc:
                        emit(error("invalid_request", str(exc)))
                return 0
            name = args.pop("object", None)
            if name is not None:
                if args.get("ref") or not args.get("target"):
                    emit(error("invalid_arguments", "Use --object with --target and without --ref"))
                    return 1
                listed = session.request("list", {"target": args["target"]})
                if not listed.get("ok"):
                    emit(listed)
                    return 1
                found = [r for r in listed["data"]["objects"] if r.get("name") == name]
                if len(found) != 1:
                    emit(error("ambiguous_object" if found else "unknown_object", "Choose one exact registered root"))
                    return 1
                args["ref"] = found[0]["ref"]
            result = session.request(op, {k: v for k, v in args.items() if v is not None})
            emit(result)
            return 0 if result.get("ok") else 1
    except (OSError, ValueError) as exc:
        emit(error("configuration_error", str(exc)))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
