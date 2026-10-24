"""Actual no-screenshot field round-trips; never sends a message.

Only initially empty fields are exercised. Every marker is read back and removed
with compare-and-set, so unrelated concurrent edits are never overwritten.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import time
import uuid

from messenger_api import MessengerSession

ROOT = Path(__file__).resolve().parent
REPORT = ROOT / 'reports/messenger-runtime-20261024'


def verify_app(app):
    report = {'app': app, 'screenshots': 0, 'messages_sent': 0, 'fields': {}}
    with MessengerSession(app) as session:
        report['status'] = session.status()
        for name in ('search', 'draft'):
            if not report['status']['fields'][name]['available']:
                report['fields'][name] = {'status': 'unavailable_on_current_page'}
                continue
            field = getattr(session, name)
            initial = field.read()
            if initial['value'] != '':
                report['fields'][name] = {'status': 'skipped_nonempty_field', 'existing_value_saved': False}
                continue
            marker = 'appctl-api-check-' + uuid.uuid4().hex[:10]
            item = {'initially_empty': True, 'calls': 0, 'restored': False}
            report['fields'][name] = item
            started = time.perf_counter()
            try:
                item['calls'] += 1
                written = field.replace(marker, '')
                read = field.read()
                assert written['value'] == marker and read['value'] == marker
                item.update(status='roundtrip_verified', source=written['source'],
                            verification=written['status'], roundtrip_ms=round((time.perf_counter()-started)*1000, 2))
            finally:
                current = field.read()['value']
                if current == marker:
                    item['calls'] += 1
                    restored = field.replace('', marker)
                    item['restored'] = restored['value'] == '' and field.read()['value'] == ''
                elif current == '':
                    item['restored'] = True
                else:
                    item['concurrent_change_detected'] = True
                    item['restored'] = False
            assert item['restored'], 'Marker cleanup was not verified'
    required = ['search'] + (['draft'] if app == 'qq' else [])
    report['ok'] = all(report['fields'][n].get('status') == 'roundtrip_verified' and report['fields'][n].get('restored') for n in required)
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--app', choices=['wechat', 'qq', 'all'], default='all')
    args = parser.parse_args()
    REPORT.mkdir(parents=True, exist_ok=True)
    reports = []
    for app in (['wechat', 'qq'] if args.app == 'all' else [args.app]):
        try:
            result = verify_app(app)
        except Exception as exc:
            result = {'app': app, 'ok': False, 'error_type': type(exc).__name__, 'error': str(exc)}
        reports.append(result)
        (REPORT / (app + '-field-validation.json')).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0 if all(r['ok'] for r in reports) else 1


if __name__ == '__main__':
    raise SystemExit(main())
