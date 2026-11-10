"""Research closure: identity, decisions, migration and full backtest evidence."""
import copy
import hashlib
import importlib
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from assay.research import ResearchWorkspace
from assay.research_lifecycle import identity


class Backend:
    def evaluate(self, expr, dataset):
        return dict(rank_ic=.1, failure_mode='INVALID' if expr == 'ts_mean(close,500)' else None)


@pytest.fixture
def research(tmp_path):
    ws = ResearchWorkspace(tmp_path / 'workspace.sqlite3', {'test': Backend()})
    ds = ws.register_dataset('daily', 'TEST', ['2024-01-01', '2024-06-30'], backend='test')
    sid = ws.create_session(ds['id'], 'discover', max_experiments=5)['id']
    return ws, sid


def test_canonical_registry_and_every_trial_survive_restart(research):
    ws, sid = research
    a = ws.evaluate(sid, 'add(close,open)', 'one', request_id='one')
    b = ws.evaluate(sid, 'add(open,close)', 'two', request_id='two', parent_id=a['id'])
    assert a['factor_id'] == b['factor_id'] == identity('add(open,close)')[0]
    assert ws.factor_registry.count() == 1
    assert ws.factor_registry.job(a['id'])['status'] == 'done'
    assert len(ws.factor_registry.query()) == 1
    resumed = ResearchWorkspace(ws.path)
    history = resumed.factor_history(a['factor_id'])
    assert history['experiments'] == [a, b]
    assert history['experiments'][1]['parent_id'] == a['id']
    assert resumed.get_session(sid)['remaining_experiments'] == 3


def test_selection_is_append_only_per_session_and_idempotent(research):
    ws, sid = research
    a = ws.evaluate(sid, 'close', 'one', request_id='one')
    other = ws.create_session(a['dataset_id'], 'other job')['id']
    ws.evaluate(other, 'close', 'same factor different job', request_id='one')
    event = ws.decide_candidate(a['id'], 'accepted', 'consistent results', request_id='accept')
    assert ws.decide_candidate(a['id'], 'accepted', 'consistent results', request_id='accept') == event
    with pytest.raises(ValueError, match='different inputs'):
        ws.decide_candidate(a['id'], 'rejected', 'changed', request_id='accept')
    archived = ws.decide_candidate(a['id'], 'archived', 'redundant', request_id='archive')
    assert ws.list_candidates(sid)[0]['decision'] == 'archived'
    assert ws.list_candidates(other)[0]['decision'] == 'candidate'
    assert ws.factor_history(a['factor_id'], session_id=sid)['decisions'] == [event, archived]
    assert ws.get_session(sid)['remaining_experiments'] == 4
    assert ws.research_context(sid)['decisions'] == [event, archived]


def test_concurrent_decisions_commit_only_once(research):
    ws, sid = research
    a = ws.evaluate(sid, 'close', 'one', request_id='one')
    def select(_):
        return ResearchWorkspace(ws.path).decide_candidate(a['id'], 'accepted', 'pass', request_id='same')
    with ThreadPoolExecutor(max_workers=4) as pool:
        events = list(pool.map(select, range(4)))
    assert all(e == events[0] for e in events)
    assert ws.get_session(sid)['decisions'] == [events[0]]


def test_failed_factor_cannot_be_accepted_but_can_be_rejected(research):
    ws, sid = research
    a = ws.evaluate(sid, 'ts_mean(close,500)', 'bad', request_id='one')
    with pytest.raises(ValueError, match='only completed'):
        ws.decide_candidate(a['id'], 'accepted', 'bad', request_id='accept')
    ws.decide_candidate(a['id'], 'rejected', 'invalid operator', request_id='reject')
    assert ws.list_candidates(sid)[0]['decision'] == 'rejected'


def test_decisions_and_registry_export_import_atomically(research, tmp_path):
    ws, sid = research
    a = ws.evaluate(sid, 'close', 'one', request_id='one')
    ws.decide_candidate(a['id'], 'accepted', 'pass', request_id='accept')
    manifest = ws.export_session(sid)
    target = ResearchWorkspace(tmp_path / 'import.sqlite3')
    assert target.import_session(manifest) == ws.get_session(sid)
    assert target.import_session(manifest) == ws.get_session(sid)
    assert target.factor_history(a['factor_id']) == ws.factor_history(a['factor_id'])
    bad = copy.deepcopy(manifest)
    bad['state']['decisions'][0]['factor_id'] = 'wrong'
    payload = {k:v for k,v in bad.items() if k != 'manifest_hash'}
    bad['manifest_hash'] = hashlib.sha256(json.dumps(payload, sort_keys=True, allow_nan=False).encode()).hexdigest()
    empty = ResearchWorkspace(tmp_path / 'empty.sqlite3')
    with pytest.raises(ValueError, match='decision factor identity'):
        empty.import_session(bad)
    assert empty.list_sessions() == [] and empty.factor_registry.count() == 0


def test_old_manifest_without_lifecycle_fields_imports(research, tmp_path):
    ws, sid = research
    ws.evaluate(sid, 'close', 'legacy', request_id='one')
    manifest = ws.export_session(sid)
    del manifest['state']['decisions']
    del manifest['state']['experiments'][0]['factor_id']
    payload = {k:v for k,v in manifest.items() if k != 'manifest_hash'}
    manifest['manifest_hash'] = hashlib.sha256(json.dumps(payload, sort_keys=True, allow_nan=False).encode()).hexdigest()
    target = ResearchWorkspace(tmp_path / 'old.sqlite3')
    state = target.import_session(manifest)
    assert state['decisions'] == []
    assert target.factor_history(identity('close')[0])['experiments'] == state['experiments']


def test_mcp_and_rest_selection_share_the_workspace(research, monkeypatch):
    from types import SimpleNamespace
    from fastapi.testclient import TestClient
    from assay.api.app import create_app
    from assay.service import AssayService
    ws, sid = research
    a = ws.evaluate(sid, 'close', 'one', request_id='one')
    service = SimpleNamespace(research=ws)
    monkeypatch.setattr(AssayService, '_instance', service)
    tools = {t.name:t.fn for t in importlib.import_module('assay.mcp.server').mcp._tool_manager.list_tools()}
    event = tools['assay_candidate_decide'](a['id'], 'accepted', 'pass', 'accept')
    assert tools['assay_factor_history'](a['factor_id'])['decisions'] == [event]
    assert tools['fdb_query'](source='research')['factors'][0]['id'] == a['factor_id']
    assert tools['fdb_factor'](a['factor_id'], source='research')['research_history']['decisions'] == [event]
    monkeypatch.setenv('ASSAY_MCP_HTTP', '0')
    with TestClient(create_app()) as client:
        response = client.get(f'/v1/research/sessions/{sid}/candidates')
        assert response.status_code == 200, response.text
        assert response.json() == tools['assay_candidate_list'](sid)['candidates']


def test_malformed_index_metric_is_terminal_and_preserves_evidence(research):
    ws, sid = research
    ws.backends['test'].evaluate = lambda expr, ds: {'rank_ic': {'value': .1}}
    a = ws.evaluate(sid, 'close', 'bad metric', request_id='bad')
    assert a['status'] == 'failed'
    assert a['report']['failure_mode'] == 'INVALID_BACKEND_REPORT'
    assert a['report']['rank_ic'] == {'value': .1}
    assert ws.get_experiment(a['id']) == a
    assert ws.factor_registry.query()[0]['rank_ic'] is None


@pytest.mark.parametrize('report,status', [({'rank_ic':.1},'completed'), (None,'failed')])
def test_generic_async_backend_can_restore_without_factordb_receipt(research, tmp_path, report, status):
    ws, _ = research
    class Async:
        def submit(self, expr, ds, request_id):
            return {'job_id':request_id, 'data_version':'custom-logical-version'}
        def poll(self, receipt):
            return {'phase':'completed','report':report}
    ws.register_backend('custom',Async())
    ds = ws.register_dataset('async','TEST',['2024-01-01','2024-02-01'],backend='custom')
    sid = ws.create_session(ds['id'],'generic async')['id']
    a = ws.evaluate(sid,'close','generic',request_id='one')
    assert a['status'] == status
    target = ResearchWorkspace(tmp_path / 'generic.sqlite3')
    assert target.import_session(ws.export_session(sid)) == ws.get_session(sid)


def test_archived_candidates_are_excluded_from_active_handoff(research):
    ws,sid = research
    a = ws.evaluate(sid,'close','one',request_id='one')
    ws.decide_candidate(a['id'],'archived','superseded',request_id='archive')
    assert ws.research_context(sid)['top_candidates'] == []


def test_monthly_output_does_not_turn_valid_daily_backtest_into_failure(tmp_path):
    from assay.demo import prepare_demo
    service, base = prepare_demo(tmp_path/'demo')
    ds = service.research.register_dataset('one-month','NASDAQ100',['2024-02-01','2024-02-29'],
        horizons=[1],portfolio={'output_frequency':'monthly','rebalance_type':'daily'})
    sid = service.research.create_session(ds['id'],'monthly output')['id']
    a = service.research.evaluate(sid,'ts_returns(close,2)','one month',request_id='one')
    assert a['status'] == 'completed', a
    assert len(a['report']['portfolio']['nav_series']) == 1
    assert a['report']['portfolio']['n_trading_days'] > 10
