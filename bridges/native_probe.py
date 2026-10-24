"""Read-only bounded metadata probe of one already-bound process; no injection."""
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def probe(pid, created):
    import psutil
    process = psutil.Process(pid)
    if process.create_time() != created:
        raise ValueError("Process identity changed")
    from native_qt import inspect_qt
    result = inspect_qt(pid, max_objects=96, max_seconds=3)
    if result.get("ok"):
        return result
    # Only actual mapped modules inside the selected application's directory.
    # There are no application names, service names or hand-written offsets here.
    root = Path(process.exe()).parent.resolve()
    modules = []
    for item in process.memory_maps(grouped=True):
        path = Path(item.path).resolve()
        if path.is_relative_to(root) and path.suffix.lower() in (".exe", ".dll", ".node"):
            modules.append(path.name)
        if len(modules) >= 6:
            break
    from native_cpp import inspect_cpp
    return inspect_cpp(pid, modules, max_seconds=5)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        print(json.dumps(probe(int(sys.argv[1]), float(sys.argv[2])), ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({"ok": False, "reason": str(exc), "injected_code": False}))
