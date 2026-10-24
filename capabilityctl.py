"""CLI and NDJSON API for the local named capability registry."""
import argparse
import json
import sys

from capability_registry import CapabilityRegistry, onboard


def main(argv=None):
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", help="Task-local registry path")
    parser.add_argument("--pretty", action="store_true")
    sub = parser.add_subparsers(dest="op", required=True)
    sub.add_parser("session")
    register = sub.add_parser("register")
    register.add_argument("path")
    listing = sub.add_parser("list")
    listing.add_argument("--app", dest="app_id")
    listing.add_argument("--query", default="")
    draft = sub.add_parser("onboard")
    draft.add_argument("--config", required=True)
    draft.add_argument("--target", required=True)
    draft.add_argument("--app-id", required=True)
    draft.add_argument("--root")
    draft.add_argument("--output", required=True)
    for op in ("describe", "validate", "invoke"):
        command = sub.add_parser(op)
        command.add_argument("--app", dest="app_id", required=True)
        command.add_argument("--capability", dest="capability_id", required=True)
        if op == "validate":
            command.add_argument("--allow-write", action="store_true")
        if op == "invoke":
            command.add_argument("--arguments", type=json.loads, default={})
    args = vars(parser.parse_args(argv))
    registry, pretty, op = args.pop("registry"), args.pop("pretty"), args.pop("op")
    def emit(value):
        print(json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2 if pretty else None), flush=True)
    with CapabilityRegistry(registry) as session:
        if op == "session":
            for line in sys.stdin:
                try:
                    request = json.loads(line)
                    if not isinstance(request, dict) or not isinstance(request.get("op"), str) or not isinstance(request.get("args", {}), dict):
                        raise ValueError("Expected op and object args")
                    result = session.request(request["op"], request.get("args"))
                    emit({"id": request.get("id"), **result})
                except ValueError as exc:
                    emit({"ok": False, "error": {"code": "invalid_request", "message": str(exc)}})
            return 0
        if op == "onboard":
            try:
                result = {"ok": True, "data": onboard(**args)}
            except Exception as exc:
                result = {"ok": False, "error": {"code": getattr(exc, "code", "onboarding_failed"), "message": str(exc)}}
        else:
            result = session.request(op, args)
        emit(result)
        return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
