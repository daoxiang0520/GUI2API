"""Read-only multi-application channel comparison; never saves control text."""
import json
from pathlib import Path
import time
import argparse
from datetime import datetime

from desktop import DesktopSession


ALLOWED = {"explorer", "applicationframehost", "weixin", "qq", "wps", "cloudmusic", "chrome", "steamwebhelper", "5eclient"}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--reveal",action="store_true",help="Temporarily restore/activate each selected window and restore its minimized state")
    args=parser.parse_args()
    output=Path("app-visible-coverage.json" if args.reveal else "app-channel-coverage.json")
    report = {"observed_at":datetime.now().astimezone().isoformat(timespec="seconds"),"screenshots": 0, "input_commands": 0, "window_reveal":args.reveal,"saved_control_names_or_values": False, "apps": []}
    with DesktopSession() as session:
        windows = session.request("apps")["data"]["windows"]
        chosen = []
        seen = set()
        for app in windows:
            if app["app"].lower() not in ALLOWED or app["app"].lower() in seen:
                continue
            seen.add(app["app"].lower())
            chosen.append(app)
        for app in chosen:
            started = time.perf_counter()
            before=session.request("window",{"window":app["window"],"pid":app["pid"]})
            reveal=None
            after_reveal=None
            minimized_restored=None
            try:
                if args.reveal and before.get("ok"):
                    reveal=session.request("window",{"window":app["window"],"pid":app["pid"],"state":"activate"})
                    time.sleep(.25)
                    after_reveal=session.request("window",{"window":app["window"],"pid":app["pid"]}).get("data")
                result = session.request("probe", {"window": app["window"], "depth": 9, "limit": 400, "probe_timeout": 6, "compare_legacy": not args.reveal, "refresh":args.reveal})
            finally:
                if args.reveal and before.get("ok") and before["data"].get("minimized"):
                    minimized_restored=session.request("window",{"window":app["window"],"pid":app["pid"],"state":"minimize"}).get("data",{}).get("status")=="verified"
            data = result.get("data", {})
            info = data.get("window") or {}
            row = {"app": app["app"], "window": app["window"], "pid": app["pid"], "minimized":info.get("minimized"), "foreground":info.get("foreground"), "access_context":info.get("access_context"), "framework_hints": info.get("framework_hints", []), "class_name": info.get("class_name"), "framework_modules": info.get("framework_modules", []), "child_classes": info.get("child_classes", []), "status": data.get("status"), "preferred_backend": data.get("preferred_backend"), "channels": data.get("channels", []), "roundtrip_ms": result.get("roundtrip_ms"), "error": result.get("error")}
            report["apps"].append(row)
            row["before"]=before.get("data")
            row["reveal"]=reveal.get("data") if reveal else None
            row["after_reveal"]=after_reveal
            row["minimized_restored"]=minimized_restored
            print(json.dumps({"app": row["app"], "frameworks": row["framework_hints"], "status": row["status"], "channels": [{"backend": c.get("backend"), "quality": c.get("quality"), "error": c.get("error", {}).get("code")} for c in row["channels"]], "seconds": round(time.perf_counter() - started, 2)}, ensure_ascii=False), flush=True)
            output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
