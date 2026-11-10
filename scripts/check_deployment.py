"""Read-only acceptance of a public Assay REST + authenticated MCP endpoint."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys

import httpx

from assay.api.deployment import INTERFACE_VERSION, endpoint_origin
from assay.agent import normalize_tool_result
from assay.mcp.client import connect_mcp, validate_mcp_url


async def check(base, key, timeout):
    rows = []

    def record(name, ok, **facts):
        rows.append(dict(check=name, passed=bool(ok), **facts))

    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        async def get(path, authenticated=False):
            headers = {"Authorization": f"Bearer {key}"} if authenticated and key else {}
            return await client.get(base+path, headers=headers)

        response = await get('/health')
        record('liveness', response.status_code == 200, http_status=response.status_code)
        response = await get('/.well-known/assay.json')
        discovery = response.json() if response.status_code == 200 else {}
        record('discovery', discovery.get('interface_version') == INTERFACE_VERSION
               and discovery.get('base_url') == base and discovery.get('mcp', {}).get('url') == base+'/mcp/'
               and discovery.get('mcp', {}).get('available') is True
               and discovery.get('authentication', {}).get('required') is True,
               http_status=response.status_code)
        response = await get('/openapi.json')
        schema = response.json() if response.status_code == 200 else {}
        required = {'/v1/research/datasets', '/v1/research/backends', '/v1/research/sessions/import',
                    '/v1/research/experiments/{experiment_id}', '/ready'}
        record('rest_contract', required <= set(schema.get('paths', {})), http_status=response.status_code)
        response = await get('/v1/research/datasets')
        record('rest_requires_auth', response.status_code == 401, http_status=response.status_code)
        response = await client.post(base+'/mcp/', headers={'Accept':'application/json, text/event-stream'},
                                     json={'jsonrpc':'2.0','id':1,'method':'tools/list','params':{}})
        record('mcp_requires_auth', response.status_code == 401, http_status=response.status_code)
        if key:
            for name, path in [('readiness', '/ready'), ('research_datasets', '/v1/research/datasets')]:
                response = await get(path, authenticated=True)
                ok = response.status_code == 200
                if name == 'readiness' and ok:
                    ok = response.json().get('status') == 'ready'
                record(name, ok, http_status=response.status_code)

    if key:
        async with connect_mcp(url=base+'/mcp/', api_key=key, timeout=timeout) as session:
            tools = await session.list_tools()
            names = {t.name for t in tools.tools}
            required = {'assay_research_audit','assay_dataset_register','assay_research_session_create','assay_experiment_run',
                        'assay_experiment_get','assay_research_context','assay_research_export',
                        'assay_research_import','assay_candidate_decide','assay_factor_history'}
            record('mcp_tools', required <= names and len(names) == discovery.get('mcp',{}).get('tool_count'),
                   count=len(names))
            lint = await session.call_tool('assay_lint', {'expr':'ts_mean(close,5)'})
            value = normalize_tool_result(lint.model_dump(mode='json'))
            record('mcp_lint', not lint.isError and isinstance(value,dict) and value.get('ast') is not None)
            datasets = await session.call_tool('assay_dataset_list', {})
            value = normalize_tool_result(datasets.model_dump(mode='json'))
            record('mcp_research_read', not datasets.isError and isinstance(value,dict)
                   and isinstance(value.get('datasets'), list))
    passed = all(row['passed'] for row in rows)
    return dict(base_url=base, status=('passed' if key else 'incomplete') if passed else 'failed',
                authenticated_checks_run=bool(key), mutation_tools_called=False,
                market_execution_checked=False, checks=rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', default='https://assay.qfinverse.com')
    parser.add_argument('--api-key-env', default='ASSAY_MCP_API_KEY')
    parser.add_argument('--timeout', type=float, default=30)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    base = endpoint_origin(args.base_url)
    validate_mcp_url(base+'/mcp/')
    if args.output.exists():
        parser.error('output already exists; choose a new report file')
    key = os.environ.get(args.api_key_env)
    try:
        result = asyncio.run(check(base, key, args.timeout))
    except Exception as exc:
        # Exceptions can include request headers/provider text. Keep only their
        # type in this public report; never serialize credentials or responses.
        result = dict(base_url=base, status='failed', error_type=type(exc).__name__,
                      authenticated_checks_run=bool(key), mutation_tools_called=False)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2)+'\n')
    print(json.dumps(result, indent=2))
    sys.exit({'passed':0, 'failed':1, 'incomplete':2}[result['status']])


if __name__ == '__main__':
    main()
