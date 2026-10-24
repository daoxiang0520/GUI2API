"""Register proven messenger fields and verify the real CLI and MCP paths."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

from capability_registry import CapabilityRegistry, digest
from messenger_api import checked

ROOT = Path(__file__).resolve().parent
REPORT = ROOT / 'reports/messenger-runtime-20261024'


def manifests():
    result = []
    for app in ('wechat', 'qq'):
        evidence = json.loads((REPORT / (app + '-field-validation.json')).read_text(encoding='utf-8'))
        if not evidence.get('ok'):
            raise RuntimeError('Live field validation must pass before registration')
        caps = []
        for slot, proof in evidence['fields'].items():
            if proof.get('status') != 'roundtrip_verified' or not proof.get('restored'):
                continue
            purpose = {'description': 'Read or replace the explicitly bound current ' + slot + ' field; no message is sent',
                       'basis': 'observed', 'sources': ['../reports/messenger-runtime-20261024/' + app + '-field-validation.json']}
            path = [{'op': 'get', 'member': slot}]
            caps.append({'id': slot + '.read', 'title': 'Read ' + slot,
                'purpose': purpose, 'effect': 'read',
                'input_schema': {'type': 'object', 'properties': {}, 'additionalProperties': False},
                'output_schema': {'type': 'string'},
                'binding': {'root': 'application', 'path': path, 'op': 'get', 'member': 'text'},
                'tests': [{'arguments': {}, 'expected': ''}]})
            def expected(text):
                return {'value': text, 'source': proof['source'], 'status': proof['verification'],
                        'message_sent': False, 'business_completion_proven': False}
            marker = 'appctl-capability-contract-check'
            caps.append({'id': slot + '.replace', 'title': 'Replace ' + slot + ' with compare-and-set',
                'purpose': purpose, 'effect': 'write',
                'input_schema': {'type': 'object', 'properties': {
                    'text': {'type': 'string', 'maxLength': 4096, 'pattern': '^[^\r\n\u0000]*$'},
                    'expected': {'type': 'string', 'maxLength': 65536}},
                    'required': ['text', 'expected'], 'additionalProperties': False},
                'output_schema': {'type': 'object', 'properties': {
                    'value': {'type': 'string'}, 'source': {'const': proof['source']},
                    'status': {'const': proof['verification']}, 'message_sent': {'const': False},
                    'business_completion_proven': {'const': False}},
                    'required': ['value', 'source', 'status', 'message_sent', 'business_completion_proven'],
                    'additionalProperties': False},
                'binding': {'root': 'application', 'path': path, 'op': 'call', 'member': 'replace',
                    'arguments': [{'$arg': 'text'}, {'$arg': 'expected'}]},
                'postconditions': [{'member': 'text', 'equals': {'$arg': 'text'}}],
                'tests': [{'arguments': {'text': marker, 'expected': ''}, 'expected': expected(marker)},
                          {'arguments': {'text': '', 'expected': marker}, 'expected': expected('')}]})
        manifest = {'format': 'appctl.capabilities/1', 'app': {'id': app, 'name': '微信' if app == 'wechat' else 'QQ'},
                    'provider': {'config': '../objects.messengers.json', 'target': app + '-controls'},
                    'capabilities': caps}
        path = ROOT / 'capabilities' / (app + '-fields.json')
        path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
        result.append((app, path, manifest))
    return result


def register_and_validate(entries):
    registry_path = ROOT / 'capabilities.local.json'
    with CapabilityRegistry(registry_path) as registry:
        existing = registry._read()['apps']
        for app, _, manifest in entries:
            if app in existing and existing[app]['manifest'] != manifest:
                raise RuntimeError('Existing application registration differs; it was preserved: ' + app)
        results = []
        for app, path, manifest in entries:
            registry.register(str(path))
            for cap in manifest['capabilities']:
                try:
                    validation = registry.validate(app, cap['id'], allow_write=cap['effect'] == 'write')
                    results.append({'app': app, 'capability': cap['id'], 'cases_passed': validation['cases_passed']})
                except Exception:
                    # The test marker is owned by this verifier. Re-read before
                    # cleanup; never replace an unrelated edit after a failure.
                    if cap['effect'] == 'write':
                        record, _ = registry._select(app, cap['id'])
                        read_cap = next(c for c in manifest['capabilities'] if c['id'] == cap['id'].replace('.replace', '.read'))
                        current, _ = registry._execute(record, read_cap, {})
                        if current == 'appctl-capability-contract-check':
                            registry._execute(record, cap, {'text': '', 'expected': current})
                    raise
        return registry_path, results


def cli_reads(registry_path):
    rows = []
    for app, capability in [('wechat', 'search.read'), ('qq', 'draft.read')]:
        process = subprocess.run([sys.executable, str(ROOT / 'appctl.py'), 'capabilities',
            '--registry', str(registry_path), 'invoke', '--app', app, '--capability', capability],
            cwd=ROOT, capture_output=True, encoding='utf-8', timeout=45,
            env={**os.environ, 'PYTHONUTF8': '1'}, creationflags=subprocess.CREATE_NO_WINDOW)
        if process.returncode:
            raise RuntimeError('CLI read failed: ' + process.stdout[-1500:] + process.stderr[-500:])
        result = checked(json.loads(process.stdout))
        assert result['result'] == ''
        rows.append({'app': app, 'capability': capability, 'ok': True})
    return rows


async def mcp_check(registry_path):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    params = StdioServerParameters(command=sys.executable, args=[str(ROOT / 'appctl_mcp.py')], cwd=str(ROOT),
        env={**os.environ, 'APPCTL_CAPABILITY_REGISTRY': str(registry_path), 'PYTHONUTF8': '1'})
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as client:
            await client.initialize()
            names = {t.name for t in (await client.list_tools()).tools}
            async def invoke(app, capability, arguments=None):
                response = await client.call_tool('application_capability_invoke',
                    {'app_id': app, 'capability_id': capability, 'arguments': arguments or {}})
                value = response.structuredContent or json.loads(response.content[0].text)
                return checked(value)
            assert (await invoke('wechat', 'search.read'))['result'] == ''
            assert (await invoke('qq', 'draft.read'))['result'] == ''
            marker = 'appctl-mcp-proof-' + uuid.uuid4().hex[:8]
            try:
                result = await invoke('qq', 'draft.replace', {'text': marker, 'expected': ''})
                assert result['result']['status'] == 'editor_model_verified'
                assert (await invoke('qq', 'draft.read'))['result'] == marker
            finally:
                current = (await invoke('qq', 'draft.read'))['result']
                if current == marker:
                    await invoke('qq', 'draft.replace', {'text': '', 'expected': marker})
            assert (await invoke('qq', 'draft.read'))['result'] == ''
            return {'ok': True, 'tools': len(names), 'wechat_search_read': True,
                    'qq_draft_write_model_readback_restore': True}


def main():
    report = {'screenshots': 0, 'messages_sent': 0, 'registered': [], 'ok': False}
    try:
        entries = manifests()
        registry_path, results = register_and_validate(entries)
        report['registered'] = results
        print(json.dumps({'stage': 'registry', 'validated': len(results)}), flush=True)
        report['cli'] = cli_reads(registry_path)
        print(json.dumps({'stage': 'cli', 'ok': True}), flush=True)
        report['mcp'] = asyncio.run(asyncio.wait_for(mcp_check(registry_path), timeout=120))
        report['ok'] = True
    finally:
        (REPORT / 'integration.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(report, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
