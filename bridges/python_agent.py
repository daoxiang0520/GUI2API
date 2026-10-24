"""Load an explicitly selected object factory into an owned Python process."""
import argparse
from contextlib import redirect_stdout
import importlib.util
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from object_bridge import ObjectHost, PythonAdapter, QtAdapter


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--module", required=True)
    parser.add_argument("--factory", default="create_roots")
    parser.add_argument("--qt", action="store_true")
    args = parser.parse_args()
    spec = importlib.util.spec_from_file_location("appctl_registered_provider", args.module)
    module = importlib.util.module_from_spec(spec)
    with redirect_stdout(sys.stderr):
        spec.loader.exec_module(module)
        roots = getattr(module, args.factory)()
    pump = None
    if args.qt:
        from PyQt5.QtCore import QCoreApplication
        application = QCoreApplication.instance() or QCoreApplication([])
        pump = application.processEvents
    ObjectHost(roots, adapter=QtAdapter() if args.qt else PythonAdapter(), pump=pump).serve()


if __name__ == "__main__":
    main()
