"""Final check: actual fixtures, public protocols, and read-only app coverage."""
import json
import argparse
from pathlib import Path
import subprocess
import sys
import os
import uuid
from datetime import datetime
import time

ROOT = Path(__file__).resolve().parent
sys.stdout.reconfigure(encoding="utf-8",errors="replace")
parser=argparse.ArgumentParser()
parser.add_argument("--stage", choices=("channel_actions","json_lines","mcp_tools","app_coverage","visible_coverage","qq_controls"))
args=parser.parse_args()
report_path=ROOT / "release-validation.json"
report = json.loads(report_path.read_text(encoding="utf-8")) if args.stage and report_path.exists() else {"ok": True, "stages": []}
report.update(run_id=uuid.uuid4().hex,started_at=datetime.now().astimezone().isoformat(timespec="seconds"),completed=False)
report_path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
for name, script in [("channel_actions", "verify_channels.py"), ("json_lines", "tests/smoke_session.py"), ("mcp_tools", "tests/smoke_mcp.py"), ("app_coverage", "probe_apps.py"), ("visible_coverage","probe_apps.py"), ("qq_controls", "verify_qq_controls.py")]:
    if args.stage and name!=args.stage:
        continue
    print(json.dumps({"stage": name, "status": "started"}), flush=True)
    started = time.monotonic()
    run = subprocess.run([sys.executable, str(ROOT / script),*(["--reveal"] if name=="visible_coverage" else [])], cwd=ROOT, env={**os.environ,"PYTHONUTF8":"1","PYTHONIOENCODING":"utf-8"},capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    item = {"stage": name, "ok": run.returncode == 0, "seconds": round(time.monotonic()-started, 2), "results": run.stdout.strip().splitlines()}
    if run.returncode:
        item["error"] = run.stderr[-8000:]
    report["stages"]=[s for s in report["stages"] if s["stage"]!=name]+[item]
    report["ok"] = all(s["ok"] for s in report["stages"])
    (ROOT / "release-validation.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(item, ensure_ascii=False), flush=True)
    if not item["ok"]:
        raise SystemExit(1)
report["completed"]=True
report_path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
