"""Run a synthetic, model-free research handoff through two MCP server processes.

Prepare data with ``python -m assay.demo --prepare-only --output ./demo-data``.
This script submits a fixed sequence of tool calls; it does not call an LLM.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


def unpack(result):
    if result.isError:
        raise RuntimeError(f'MCP tool failed: {result.content}')
    if isinstance(result.structuredContent, dict):
        return result.structuredContent
    if len(result.content) == 1 and result.content[0].type == 'text':
        return json.loads(result.content[0].text)
    raise RuntimeError('MCP returned an unexpected result format')


async def call(session, name, arguments):
    return unpack(await session.call_tool(name, arguments))


async def walkthrough(data_dir):
    params = StdioServerParameters(
        command=sys.executable,
        args=['-m', 'assay.mcp.server'],
        env={**os.environ, 'ASSAY_DATA_DIR': str(data_dir)},
        cwd=str(data_dir),
    )
    async with stdio_client(params) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = {tool.name for tool in tools.tools}
            required = {'assay_dataset_list', 'assay_research_session_create',
                        'assay_experiment_run', 'assay_research_note',
                        'assay_checkpoint_save', 'assay_research_session_get',
                        'assay_checkpoint_get', 'assay_experiment_compare'}
            missing = required - names
            if missing:
                raise RuntimeError(f'MCP server is missing required tools: {sorted(missing)}')
            datasets = await call(session, 'assay_dataset_list', {})
            matches = [d for d in datasets['datasets'] if d['name'] == 'synthetic-demo']
            if len(matches) != 1:
                raise RuntimeError('Expected one synthetic-demo dataset; prepare a new workspace first')
            created = await call(session, 'assay_research_session_create', {
                'dataset_id': matches[0]['id'], 'goal': 'Explore synthetic short-horizon reversal',
                'agent': 'scripted-walkthrough', 'max_experiments': 5,
            })
            session_id = created['id']
            first = await call(session, 'assay_experiment_run', {
                'session_id': session_id, 'expr': '-cs_rank(ts_returns(close,5))',
                'hypothesis': 'Inspect a five-day reversal signal on synthetic data',
                'request_id': 'first', 'agent': 'scripted-walkthrough',
            })
            if first['status'] != 'completed':
                raise RuntimeError(f'Initial evaluation did not complete: {first}')
            failed = await call(session, 'assay_experiment_run', {
                'session_id': session_id, 'expr': 'unknown_operator(close)',
                'hypothesis': 'Retain an invalid expression for diagnosis',
                'request_id': 'failure', 'agent': 'scripted-walkthrough',
            })
            if failed['status'] != 'failed':
                raise RuntimeError(f'Expected the invalid expression to fail: {failed}')
            await call(session, 'assay_research_note', {
                'session_id': session_id, 'text': 'Inspect the failure and compare a shorter lookback.',
                'agent': 'scripted-walkthrough',
            })
            await call(session, 'assay_checkpoint_save', {
                'session_id': session_id, 'name': 'handoff', 'algorithm': 'scripted',
                'state': {'next_expression': '-cs_rank(ts_returns(close,3))',
                          'parent_id': first['id']},
                'request_id': 'checkpoint', 'expected_revision': 0,
            })

    # The first server has exited. A fresh MCP process reads the same workspace.
    async with stdio_client(params) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            restored = await call(session, 'assay_research_session_get', {'session_id': session_id})
            observed = {item['id']: item['status'] for item in restored['experiments']}
            if (restored['id'] != session_id or observed.get(first['id']) != 'completed'
                    or observed.get(failed['id']) != 'failed' or not restored['notes']
                    or restored['remaining_experiments'] != 3):
                raise RuntimeError('The restarted server did not restore the expected history and budget')
            checkpoint = await call(session, 'assay_checkpoint_get', {
                'session_id': session_id, 'name': 'handoff',
            })
            if checkpoint['checkpoint']['state']['parent_id'] != first['id']:
                raise RuntimeError('Restored checkpoint did not retain the experiment ID')
            revision = await call(session, 'assay_experiment_run', {
                'session_id': session_id, 'expr': checkpoint['checkpoint']['state']['next_expression'],
                'hypothesis': 'Compare a shorter lookback after handoff',
                'request_id': 'revision', 'parent_id': first['id'],
                'agent': 'scripted-walkthrough',
            })
            if revision['status'] != 'completed' or revision.get('parent_id') != first['id']:
                raise RuntimeError(f'Revision did not complete with the expected parent: {revision}')
            comparison = await call(session, 'assay_experiment_compare', {
                'experiment_ids': [first['id'], revision['id']],
            })
            if not comparison.get('comparable'):
                raise RuntimeError('The two evaluations were not reported as comparable')
            final_session = await call(session, 'assay_research_session_get', {'session_id': session_id})
            if final_session['remaining_experiments'] != 2:
                raise RuntimeError('The final session did not retain two attempts for further research')
            return {
                'synthetic': True, 'model_calls': 0, 'session_id': session_id,
                'remaining_experiments': final_session['remaining_experiments'],
                'first': first, 'failed': failed, 'restored_session': restored,
                'checkpoint': checkpoint, 'revision': revision, 'comparison': comparison,
            }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data', type=Path, required=True, help='Prepared synthetic workspace')
    parser.add_argument('--output', type=Path, required=True, help='New JSON result file')
    args = parser.parse_args()
    data = args.data.resolve()
    if not (data / 'demo-dataset.json').is_file():
        parser.error('--data must point to a workspace prepared by assay.demo --prepare-only')
    if args.output.exists():
        parser.error('--output already exists; choose a new path')
    result = asyncio.run(walkthrough(data))
    with args.output.open('x', encoding='utf-8') as stream:
        json.dump(result, stream, indent=2, allow_nan=False)
    print(f"Synthetic MCP handoff completed. Session: {result['session_id']}")
    print(f"Initial and revised experiments: {result['first']['id']}, {result['revision']['id']}")
    print(f"Saved results: {args.output.resolve()}")


if __name__ == '__main__':
    main()
