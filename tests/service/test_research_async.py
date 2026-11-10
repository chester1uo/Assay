"""Real controller/worker research jobs, including cross-database crash recovery."""
from concurrent.futures import ThreadPoolExecutor
import pytest

from assay.research import ResearchWorkspace
from assay.integrations.factordb import FactorDBResearchBackend
from assay.factordb import service
from assay.ades.worker import Worker
from assay.data.store import QFVPanelStore
from tests.ades.test_ades import controller, _ClientTransport


@pytest.fixture
def research(controller, tmp_path):
    client, panel = controller
    svc = service.get()
    backend = FactorDBResearchBackend(lambda: svc)
    ws = ResearchWorkspace(tmp_path / 'research.sqlite', {'factordb_ades': backend})
    ds = ws.register_dataset('panel', 'TOP500', ['2024-12-02', str(svc.db.grid().end)],
                             backend='factordb_ades', adj='total', horizons=[1],
                             snapshot_id=svc.db.qfv.snapshot_id)
    sid = ws.create_session(ds['id'], 'reversal', max_experiments=5)['id']
    return ws, backend, svc, sid, Worker(_ClientTransport(client, 'wk-1'), QFVPanelStore(panel), 'research-worker')


def submit(ws, sid, expr='ts_delta(close, 2)', request='one'):
    return ws.evaluate(sid, expr, 'test hypothesis', request_id=request)


def test_real_worker_restart_and_cached_attempt(research):
    ws, backend, svc, sid, worker = research
    a = submit(ws, sid)
    assert a['status'] == 'running' and a['execution']['job_id']
    assert worker.run_once()
    resumed = ResearchWorkspace(ws.path, {'factordb_ades': backend})
    done = resumed.reconcile_experiment(a['id'])
    assert done['status'] == 'completed'
    assert done['report']['data_version'] == a['execution']['data_version']
    assert resumed.reconcile_experiment(a['id']) == done
    assert submit(resumed, sid) == done
    usage = svc.reg.usage_today('research')
    cached = submit(resumed, sid, request='two')
    assert cached['status'] == 'completed' and cached['execution']['cached']
    assert cached['id'] != done['id']
    assert svc.reg.usage_today('research') == usage
    assert resumed.get_session(sid)['remaining_experiments'] == 3
    manifest = resumed.export_session(sid)
    assert resumed.verify_manifest(manifest)
    resumed.import_session(manifest)


@pytest.mark.parametrize('field', ['factor_id','request_id','spec','job_id'])
@pytest.mark.parametrize('status', ['completed','failed'])
def test_inconsistent_terminal_receipts_cannot_be_restored(research, tmp_path, field, status):
    import hashlib
    import json
    ws, _, _, sid, worker = research
    attempt = submit(ws,sid)
    assert worker.run_once()
    assert ws.reconcile_experiment(attempt['id'])['status'] == 'completed'
    manifest = ws.export_session(sid)
    saved = manifest['state']['experiments'][0]
    saved['status'] = status
    saved['execution']['phase'] = status
    if status == 'failed':
        saved['report']['failure_mode'] = 'TEST_FAILURE'
    saved['execution'][field] = 'inconsistent'
    payload = {k:v for k,v in manifest.items() if k != 'manifest_hash'}
    manifest['manifest_hash'] = hashlib.sha256(json.dumps(payload,sort_keys=True,allow_nan=False).encode()).hexdigest()
    target = ResearchWorkspace(tmp_path/'invalid-import.sqlite3')
    with pytest.raises(ValueError,match='execution .* identity'):
        target.import_session(manifest)
    assert target.list_sessions() == [] and target.factor_registry.count() == 0


def test_legacy_next_close_history_imports_but_cannot_execute(research, tmp_path):
    import hashlib
    import json
    ws, backend, _, sid, worker = research
    attempt=submit(ws,sid)
    assert worker.run_once()
    assert ws.reconcile_experiment(attempt['id'])['status']=='completed'
    manifest=ws.export_session(sid)
    state=manifest['state']
    dataset=state['dataset']
    dataset.pop('evaluation_protocol')
    dataset['execution']='next_close'
    metadata={'id','name','description','spec_hash','created_at'}
    spec={k:v for k,v in dataset.items() if k not in metadata}
    dataset['spec_hash']=hashlib.sha256(json.dumps(spec,sort_keys=True,allow_nan=False).encode()).hexdigest()
    saved=state['experiments'][0]
    saved['spec_hash']=dataset['spec_hash']
    legacy=json.dumps([dataset['universe'],*dataset['period'],dataset['horizons'],'next_close'])
    saved['execution']['spec']=hashlib.blake2b(legacy.encode(),digest_size=8).hexdigest()
    payload={k:v for k,v in manifest.items() if k!='manifest_hash'}
    manifest['manifest_hash']=hashlib.sha256(json.dumps(payload,sort_keys=True,allow_nan=False).encode()).hexdigest()
    target=ResearchWorkspace(tmp_path/'legacy-import.sqlite3')
    assert target.import_session(manifest)['experiments']==state['experiments']
    with pytest.raises(ValueError,match='corrected next-day-close'):
        backend.validate_dataset(dataset)


@pytest.mark.parametrize('boundary', ['lost_response', 'queue_dispatch'])
def test_submission_crash_recovers_one_job_and_one_task(research, monkeypatch, boundary):
    ws, backend, svc, sid, worker = research
    obj, name = (backend, 'submit') if boundary == 'lost_response' else (svc.ades, 'enqueue')
    original = getattr(obj, name)
    def crash(*args, **kwargs):
        if boundary == 'lost_response':
            original(*args, **kwargs)
        raise OSError('simulated controller crash')
    monkeypatch.setattr(obj, name, crash)
    a = submit(ws, sid)
    assert a['status'] == 'running' and a['error']['retryable']
    monkeypatch.setattr(obj, name, original)
    resumed = ResearchWorkspace(ws.path, {'factordb_ades': backend})
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: resumed.reconcile_experiment(a['id']), range(4)))
    assert len(svc.reg.jobs()) == 1
    assert svc.ades.status()['tasks'] == {'queued': 1}
    assert worker.run_once()
    assert resumed.reconcile_experiment(a['id'])['status'] == 'completed'
    assert resumed.get_session(sid)['remaining_experiments'] == 4


def test_completed_queue_receipt_recovers_registry_after_crash(research, monkeypatch):
    ws, backend, svc, sid, worker = research
    a = submit(ws, sid)
    task = svc.ades.lease('w', a['execution']['data_version'])
    results = [{'factor_id': a['execution']['factor_id'], 'metrics': {'rank_ic': .1}}]
    completed = svc.ades.complete(task['task_id'], 'w', 1, 0, 2.,
                                  results=results, lease_token=task['lease_token'])
    # Process dies after queue commit, before registry apply_results.
    assert svc.reg.job(task['job_id'])['done'] == 0
    completed_day = '2020-01-02'
    with svc.ades._conn() as c:
        c.execute('UPDATE tasks SET updated=? WHERE id=?', (1577923200., task['task_id']))
    assert ws.reconcile_experiment(a['id'])['status'] == 'completed'
    svc.recover_results()
    assert svc.reg.job(task['job_id'])['done'] == 1
    with svc.reg._conn() as c:
        usage = c.execute('SELECT * FROM usage WHERE owner=?', ('research',)).fetchall()
    assert len(usage) == 1 and usage[0]['day'] == completed_day and usage[0]['seconds'] == 2.


def test_max_retry_failure_receipt_recovers_without_progress_callback(research):
    ws, _, svc, sid, _ = research
    a = submit(ws, sid)
    svc.ades.max_attempts = 1
    t = svc.ades.lease('w', a['execution']['data_version'])
    svc.ades.fail(t['task_id'], 'w', 'worker crashed', t['lease_token'])
    assert svc.reg.job(t['job_id'])['failed'] == 0
    failed = ws.reconcile_experiment(a['id'])
    assert failed['status'] == 'failed' and failed['report']['failure_mode'] == 'BACKEND_EVALUATION_FAILED'
    svc.recover_results()
    assert svc.reg.job(t['job_id'])['failed'] == 1


def test_stale_lease_is_fenced_even_for_same_worker(research):
    ws, _, svc, sid, _ = research
    a = submit(ws, sid)
    q = svc.ades
    old = q.lease('w', a['execution']['data_version'])
    assert q.release(old['task_id'], 'w', old['lease_token'])
    new = q.lease('w', a['execution']['data_version'])
    assert old['lease_token'] != new['lease_token']
    assert not q.heartbeat(old['task_id'], 'w', old['lease_token'])
    assert not q.release(old['task_id'], 'w', old['lease_token'])
    assert q.fail(old['task_id'], 'w', 'stale', old['lease_token']) == []
    assert q.complete(old['task_id'], 'w', 1, 0, 0., lease_token=old['lease_token']) is None
    assert not q.heartbeat(new['task_id'], 'w')
    assert q.heartbeat(new['task_id'], 'w', new['lease_token'])


@pytest.mark.parametrize('expr', ['nosuch(close)', 'close + ('])
def test_invalid_expression_is_terminal_and_revisable(research, expr):
    ws, _, svc, sid, _ = research
    a = submit(ws, sid, expr)
    assert a['status'] == 'failed' and a['error']['code'] == 'INVALID_EXPRESSION'
    assert not a['error']['retryable'] and not svc.reg.jobs()


def test_version_mismatch_creates_no_backend_job(research, monkeypatch):
    ws, backend, svc, sid, _ = research
    monkeypatch.setattr(type(svc.db.qfv), 'snapshot_id', property(lambda _: 'changed'))
    a = submit(ws, sid)
    assert a['status'] == 'failed' and a['error']['code'] == 'DATA_VERSION_MISMATCH'
    assert not svc.reg.jobs()


def test_invalid_result_rolls_back_receipt_and_keeps_lease(research):
    ws, _, svc, sid, _ = research
    a = submit(ws, sid)
    q = svc.ades
    t = q.lease('w', a['execution']['data_version'])
    results = [{'factor_id': a['execution']['factor_id'], 'metrics': {'rank_ic': float('nan')}}]
    with pytest.raises(ValueError):
        q.complete(t['task_id'], 'w', 1, 0, 1., results=results, lease_token=t['lease_token'])
    assert not q.result_receipts()
    assert q.status()['tasks'] == {'leased': 1}
    assert q.heartbeat(t['task_id'], 'w', t['lease_token'])


def test_database_failure_after_receipt_insert_rolls_back_everything(research):
    import sqlite3
    ws, _, svc, sid, _ = research
    a = submit(ws, sid)
    q = svc.ades
    t = q.lease('w', a['execution']['data_version'])
    with q._conn() as c:
        c.execute("CREATE TRIGGER crash_complete BEFORE UPDATE ON tasks WHEN NEW.status='done' "
                  "BEGIN SELECT RAISE(ABORT, 'simulated crash after receipt'); END")
    results = [{'factor_id': a['execution']['factor_id'], 'metrics': {'rank_ic': .1}}]
    with pytest.raises(sqlite3.IntegrityError, match='simulated crash'):
        q.complete(t['task_id'], 'w', 1, 0, 1., results=results, lease_token=t['lease_token'])
    assert not q.result_receipts() and q.status()['tasks'] == {'leased': 1}
    assert q.status()['workers'][0]['done'] == 0
    with q._conn() as c:
        c.execute('DROP TRIGGER crash_complete')
    assert q.complete(t['task_id'], 'w', 1, 0, 1., results=results, lease_token=t['lease_token'])
    assert ws.reconcile_experiment(a['id'])['status'] == 'completed'


def test_lease_validation_reads_clock_after_acquiring_write_transaction(research, monkeypatch):
    ws, _, svc, sid, _ = research
    a = submit(ws, sid)
    q = svc.ades
    t = q.lease('w', a['execution']['data_version'])
    original = q._conn
    clock = [0.]
    def connection():
        c = original()
        def trace(sql):
            if sql == 'BEGIN IMMEDIATE':
                clock[0] = 10**12  # lease expires while waiting for transaction lock
        c.set_trace_callback(trace)
        return c
    monkeypatch.setattr(q, '_conn', connection)
    monkeypatch.setattr('assay.ades.tasks.time.time', lambda: clock[0])
    assert q.complete(t['task_id'], 'w', 1, 0, 0., lease_token=t['lease_token']) is None


def test_rest_and_mcp_reconcile_share_the_same_attempt(research, controller, monkeypatch):
    from types import SimpleNamespace
    from mcp.server.fastmcp import FastMCP
    from assay.mcp.research import register_research_tools
    import assay.api.routes.research as routes
    ws, _, _, sid, worker = research
    monkeypatch.setattr(routes, 'get_service', lambda: SimpleNamespace(research=ws))
    client, _ = controller
    a = submit(ws, sid)
    path = '/v1/research/experiments/' + a['id'] + '/reconcile'
    assert client.post(path).json()['status'] == 'running'
    assert worker.run_once()
    mcp = FastMCP('async-test')
    register_research_tools(mcp, lambda: SimpleNamespace(research=ws))
    result = mcp._tool_manager.get_tool('assay_experiment_reconcile').fn(a['id'])
    assert result['status'] == 'completed'
    assert client.post(path).json() == result
    assert ws.get_session(sid)['remaining_experiments'] == 4
