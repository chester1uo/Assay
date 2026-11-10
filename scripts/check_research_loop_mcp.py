"""Run the research loop through two fresh MCP stdio server processes."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def check(data_dir, output):
    output.mkdir(parents=True, exist_ok=False)
    env = dict(os.environ, ASSAY_DATA_DIR=str(data_dir.resolve()), ASSAY_QFV_PANEL_DIR='',
               ASSAY_API_KEYS='', ASSAY_ADMIN_KEYS='', ASSAY_ACCOUNTS='')
    params = StdioServerParameters(command=sys.executable, args=['-m','assay.mcp.server'], env=env)
    receipts = []
    async def call(client, tool_name, **arguments):
        result = await client.call_tool(tool_name, arguments)
        assert not result.isError, result
        value = result.structuredContent
        if value is None:
            value = json.loads(next(c.text for c in result.content if c.type == 'text'))
        assert not value.get('error'), value
        receipts.append(dict(tool=tool_name,arguments=arguments,result=value))
        return value
    async with stdio_client(params) as (reader, writer):
        async with ClientSession(reader,writer) as client:
            await client.initialize()
            tools = await client.list_tools()
            assert len(tools.tools) == 39
            ds = await call(client,'assay_dataset_register',name='stdio-loop',universe='NASDAQ100',
                            period=['2024-01-02','2024-06-17'],horizons=[1])
            session = await call(client,'assay_research_session_create',dataset_id=ds['id'],goal='Verify MCP research closure',max_experiments=3)
            sid = session['id']
            first = await call(client,'assay_experiment_run',session_id=sid,expr='ts_returns(close,5)',hypothesis='momentum',request_id='first')
            assert first['status'] == 'completed' and first['report']['portfolio']['status'] == 'completed'
            failed = await call(client,'assay_experiment_run',session_id=sid,expr='unknown_operator(close)',hypothesis='diagnostic retention',request_id='invalid')
            assert failed['status'] == 'failed'
            revised = await call(client,'assay_experiment_run',session_id=sid,expr='ts_returns(close,3)',hypothesis='shorter lookback',request_id='revision',parent_id=first['id'])
            assert revised['status'] == 'completed'
            await call(client,'assay_candidate_decide',experiment_id=revised['id'],decision='accepted',reason='Continue shorter lookback',request_id='accept')
            await call(client,'assay_candidate_decide',experiment_id=first['id'],decision='archived',reason='Retain baseline',request_id='archive')
            catalog = await call(client,'fdb_query',source='research')
            assert revised['factor_id'] in {f['id'] for f in catalog['factors']}
    async with stdio_client(params) as (reader,writer):
        async with ClientSession(reader,writer) as client:
            await client.initialize()
            state = await call(client,'assay_research_session_get',session_id=sid)
            assert state['remaining_experiments'] == 0 and len(state['decisions']) == 2
            retry = await call(client,'assay_experiment_run',session_id=sid,expr=revised['expr'],hypothesis=revised['hypothesis'],request_id='revision',parent_id=first['id'])
            assert retry == revised
            candidates = await call(client,'assay_candidate_list',session_id=sid)
            assert {c['decision'] for c in candidates['candidates']} == {'accepted','archived'}
            history = await call(client,'assay_factor_history',factor_id=revised['factor_id'])
            assert any(e['id']==revised['id'] for e in history['experiments'])
            context = await call(client,'assay_research_context',session_id=sid)
            assert len(context['top_candidates']) == 1 and context['top_candidates'][0]['id'] == revised['id']
            manifest = await call(client,'assay_research_export',session_id=sid)
    from assay.research import ResearchWorkspace
    target = ResearchWorkspace(output/'restored.sqlite3')
    assert target.import_session(manifest) == state
    result = dict(tools=39, process_restart=True, persisted_portfolio=True, decisions_restored=True,
                  retry_did_not_spend_budget=True, catalog_discovery=True, manifest_roundtrip=True,
                  provider_calls=0, tool_calls=len(receipts), session_id=sid)
    (output/'manifest.json').write_text(json.dumps(manifest,indent=2))
    (output/'receipts.json').write_text(json.dumps(receipts,indent=2))
    (output/'checks.json').write_text(json.dumps(result,indent=2))
    print(json.dumps(result,indent=2))


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    asyncio.run(check(args.data_dir,args.output))
