"""Search checkpoints and cross-window programs through real MCP restarts."""
import asyncio
import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path

from assay.demo import prepare_demo


def test_search_program_and_checkpoint_over_stdio(tmp_path):
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    root = Path(__file__).resolve().parents[2]
    data_dir = tmp_path / 'data'
    _, fixture = prepare_demo(data_dir)
    env = dict(os.environ, PYTHONPATH=str(root / 'src'), ASSAY_DATA_DIR=str(data_dir),
               PYTHONDONTWRITEBYTECODE='1')
    params = StdioServerParameters(command=sys.executable, args=['-m', 'assay.mcp.server'], env=env, cwd=str(tmp_path))

    def weekdays(start, end):
        day, last = date.fromisoformat(start), date.fromisoformat(end)
        result = []
        while day <= last:
            if day.weekday() < 5:
                result.append(day.isoformat())
            day += timedelta(days=1)
        return result

    async def call(session, tool_name, **args):
        result = await session.call_tool(tool_name, args)
        assert not result.isError, result
        return result.structuredContent or json.loads(result.content[0].text)

    async def scenario():
        with (tmp_path / 'mcp.log').open('w') as log:
            async with stdio_client(params, errlog=log) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = {t.name for t in (await session.list_tools()).tools}
                    assert {'assay_checkpoint_save', 'assay_program_reassess'} <= tools
                    common = dict(universe='NASDAQ100', period=fixture['period'], horizons=[1],
                                  portfolio={'enabled': False})
                    old_ds = await call(session, 'assay_dataset_register', name='old',
                        score_period=['2024-02-01', '2024-03-29'],
                        score_dates=weekdays('2024-02-01', '2024-03-29'), **common)
                    new_ds = await call(session, 'assay_dataset_register', name='new',
                        score_period=['2024-04-01', '2024-05-31'],
                        score_dates=weekdays('2024-04-01', '2024-05-31'), **common)
                    old = (await call(session, 'assay_research_session_create', dataset_id=old_ds['id'],
                                      goal='MCTS source', max_experiments=4))['id']
                    new = (await call(session, 'assay_research_session_create', dataset_id=new_ds['id'],
                                      goal='Later evaluation', max_experiments=4))['id']
                    program = await call(session, 'assay_program_create', name='test', goal='Track factor', request_id='p1')
                    pid = program['id']
                    await call(session, 'assay_program_attach_session', program_id=pid, session_id=old, label='old')
                    await call(session, 'assay_program_attach_session', program_id=pid, session_id=new, label='new')
                    attempt = await call(session, 'assay_experiment_run', session_id=old,
                        expr='ts_returns(close,3)', hypothesis='momentum', request_id='first')
                    assert attempt['status'] == 'completed', attempt
                    ctx = await call(session, 'assay_research_context', session_id=old)
                    cp = await call(session, 'assay_checkpoint_save', session_id=old, name='tree', algorithm='mcts',
                        state={'nodes': [{'attempt_id': attempt['id'], 'visits': 3}], 'rng': [2, 3]},
                        request_id='cp1', expected_revision=0, expected_state_revision=ctx['state_revision'])
                    exported = await call(session, 'assay_research_export', session_id=old)
                    assert exported['state']['checkpoints'][0] == cp
            async with stdio_client(params, errlog=log) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    loaded = await call(session, 'assay_checkpoint_get', session_id=old, name='tree')
                    assert loaded['checkpoint'] == cp and not loaded['stale']
                    assert (await call(session, 'assay_program_list'))['programs'][0]['id'] == pid
                    new_attempt = await call(session, 'assay_program_reassess', program_id=pid,
                        source_experiment_id=attempt['id'], target_session_id=new, request_id='later')
                    assert new_attempt['attempt']['status'] == 'completed', new_attempt
                    history = await call(session, 'assay_program_factor_history', program_id=pid,
                                         factor_id=attempt['factor_id'])
                    assert len(history['attempts']) == 2 and history['settings_match']
                    pool = await call(session, 'assay_program_candidates', program_id=pid)
                    assert pool['total'] == 1 and pool['factors'][0]['attempt_count'] == 2
                    assert history['attempts'][1]['reassessment_of'] == attempt['id']
                    assert {tuple(a['score_period']) for a in history['attempts']} == {
                        ('2024-02-01', '2024-03-29'), ('2024-04-01', '2024-05-31')}
                    compared = await call(session, 'assay_program_compare_windows', program_id=pid,
                        baseline_experiment_id=attempt['id'], target_experiment_id=new_attempt['attempt']['id'],
                        drop_threshold=.01)
                    assert compared['baseline_window'] == ['2024-02-01', '2024-03-29']
                    assert compared['target_window'] == ['2024-04-01', '2024-05-31']

    asyncio.run(scenario())
