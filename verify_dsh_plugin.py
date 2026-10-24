"""Check DSH plugin with official services and an owned Qt window; no model calls."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

from probe_universality import launch_owned, close_owned

ROOT = Path(__file__).resolve().parent
PLUGIN = ROOT / 'plugins/dsh-gui2api-bot'


def main():
    run = ROOT / 'tests/runs' / ('dsh-plugin-' + uuid.uuid4().hex[:10])
    run.mkdir(parents=True)
    version = json.loads((PLUGIN/'package.json').read_text(encoding='utf-8'))['version']
    report = {'ok': False, 'plugin_version':version, 'screenshots': 0, 'model_api_calls': 0, 'messages_sent': 0,
              'scope': 'official DSH SDK services and an owned fixture; no VM or live DeepSeek connection'}
    owned = None
    try:
        unit = subprocess.run(['node', '--test', 'test/bot.test.mjs', 'test/host.test.mjs', 'test/lifecycle.test.mjs', 'test/discovery.test.mjs'], cwd=PLUGIN,
            capture_output=True, text=True, encoding='utf-8', timeout=60,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        report['unit'] = {'ok': unit.returncode == 0, 'output': unit.stdout, 'stderr': unit.stderr}
        if unit.returncode:
            raise RuntimeError('Plugin unit checks failed')
        state = run / 'qt-state.json'
        owned = launch_owned([sys.executable, '-X', 'utf8', str(ROOT/'tests/fixtures/qt_gui_controls.py'), str(state)])
        deadline = time.monotonic() + 12
        while not state.exists() and time.monotonic() < deadline and owned.poll() is None:
            time.sleep(.05)
        initial = json.loads(state.read_text(encoding='utf-8'))
        environment = {**os.environ, 'GUI2API_PYTHON': sys.executable}
        result = subprocess.run(['node', str(PLUGIN/'test/owned-window.mjs'), str(ROOT), str(run/'bot-home'), str(initial['window']), str(state)],
            cwd=PLUGIN, env=environment, capture_output=True, text=True, encoding='utf-8', timeout=90,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        report['integration'] = json.loads(result.stdout.strip().splitlines()[-1])
        report['integration_stderr'] = result.stderr
        report['ok'] = result.returncode == 0 and report['integration']['ok']
    except Exception as exc:
        report['error'] = str(exc)
    finally:
        if owned is not None:
            try: report['fixture_cleanup'] = close_owned(owned)
            except Exception as exc:
                report['fixture_cleanup'] = {'closed':False,'error':str(exc)}
                report['ok'] = False
    output = ROOT/('reports/dsh-plugin-'+version)
    output.mkdir(parents=True, exist_ok=True)
    (output/'validation.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
