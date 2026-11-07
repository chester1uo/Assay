"""Validate the actual study intervention and cell failure accounting without models."""
import asyncio
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

from mcp.types import CallToolResult, TextContent
from assay.research import ResearchWorkspace


def study_module():
    path = Path(__file__).resolve().parents[2] / 'experiments/agent-readiness/study.py'
    spec = importlib.util.spec_from_file_location('assay_state_study', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_hidden_state_masks_both_representations_but_retains_new_evidence(tmp_path):
    module = study_module()

    class Backend:
        def evaluate(self, expr, dataset):
            return {'rank_ic': .1}

    ws = ResearchWorkspace(tmp_path / 'db.sqlite3', {'test': Backend()})
    ds = ws.register_dataset('demo', 'TEST', ['2024-01-01', '2024-06-30'], backend='test')
    sid = ws.create_session(ds['id'], 'goal', max_experiments=4)['id']
    old = ws.evaluate(sid, 'close', 'PRIVATE_OLD_HYPOTHESIS', request_id='old')
    ws.add_note(sid, 'PRIVATE_OLD_NOTE')
    new = ws.evaluate(sid, 'open', 'VISIBLE_NEW_HYPOTHESIS', request_id='new')
    ws.add_note(sid, 'VISIBLE_NEW_NOTE')

    class Session:
        async def list_tools(self, **kwargs):
            return {'same_schema': True}

        async def call_tool(self, name, args):
            value = ws.research_context(sid) if name == 'assay_research_context' else ws.get_experiment(args['experiment_id'])
            return CallToolResult(structuredContent=value, content=[TextContent(type='text', text=json.dumps(value))])

    async def scenario():
        hidden = module.StateView(Session(), ws, sid, {old['id']}, 1, 'hidden')
        full = module.StateView(Session(), ws, sid, {old['id']}, 1, 'full')
        assert await hidden.list_tools() == await full.list_tools()
        context = await hidden.call_tool('assay_research_context', {'session_id': sid})
        serialized = context.model_dump_json()
        assert 'PRIVATE_OLD' not in serialized and 'VISIBLE_NEW' in serialized
        assert context.structuredContent['remaining_experiments'] == 2
        assert context.structuredContent['hidden_attempt_count'] == 1
        prior = await hidden.call_tool('assay_experiment_get', {'experiment_id': old['id']})
        assert 'PRIVATE_OLD' not in prior.model_dump_json()
        recent = await hidden.call_tool('assay_experiment_get', {'experiment_id': new['id']})
        assert recent.structuredContent == new
        assert 'PRIVATE_OLD' in (await full.call_tool('assay_research_context', {'session_id': sid})).model_dump_json()

    asyncio.run(scenario())


def test_runner_failure_and_unrun_cells_remain_in_inventory(tmp_path, monkeypatch):
    module = study_module()
    config = tmp_path / 'model.json'
    config.write_text(json.dumps({'model': 'fake', 'api_key_env': 'ASSAY_TEST_KEY'}))
    monkeypatch.setenv('ASSAY_TEST_KEY', 'test-value')

    async def broken(*args):
        raise RuntimeError('fixture setup failed before provider')

    monkeypatch.setattr(module, 'trial', broken)
    output = tmp_path / 'results'
    asyncio.run(module.main(SimpleNamespace(output=output, configs=[config], seeds=[7, 13])))
    rows = json.loads((output / 'results.json').read_text())
    assert len(rows) == 4
    assert sum(r.get('status') == 'runner_failure' for r in rows) == 2
    assert sum(r.get('status') == 'not_run' for r in rows) == 2
    assert all(r['provider_requests'] == 0 and not r['usage_complete'] for r in rows if r.get('status') == 'runner_failure')
