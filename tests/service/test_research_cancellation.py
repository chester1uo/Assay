"""Cancellation races and durable recovery across research, queue and registry."""
from threading import Event
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import pytest
from assay.research import ResearchWorkspace
from tests.service.test_research_async import research, submit, controller


def test_queued_cancel_is_idempotent_preserves_budget_and_does_not_poison_cache(research):
    ws, backend, svc, sid, worker = research
    a = submit(ws, sid)
    cancelled = ws.cancel_experiment(a['id'], 'Research direction changed')
    assert cancelled['status'] == 'cancelled'
    assert ws.cancel_experiment(a['id'], 'different reason') == cancelled
    assert ws.reconcile_experiment(a['id']) == cancelled
    assert submit(ws, sid) == cancelled
    assert svc.reg.job(a['execution']['job_id'])['status'] == 'cancelled'
    assert not worker.run_once()
    assert svc.reg.usage_today('research')['factors'] == 0
    assert svc.reg.score(a['execution']['factor_id'], a['execution']['spec'], a['execution']['data_version']) is None
    assert ws.get_session(sid)['remaining_experiments'] == 4
    manifest = ws.export_session(sid)
    imported = ResearchWorkspace(ws.path.parent / 'imported.sqlite', {'factordb_ades': backend})
    imported.import_session(manifest)
    assert imported.get_experiment(a['id']) == cancelled
    revised = submit(ws, sid, request='new-attempt')
    assert not revised['execution']['cached']
    assert worker.run_once()
    assert ws.reconcile_experiment(revised['id'])['status'] == 'completed'


def test_leased_cancel_fences_all_old_worker_operations(research):
    ws, _, svc, sid, _ = research
    a = submit(ws, sid)
    q = svc.ades
    t = q.lease('w', a['execution']['data_version'])
    assert ws.cancel_experiment(a['id'], 'Stop this attempt')['status'] == 'cancelled'
    assert not q.heartbeat(t['task_id'], 'w', t['lease_token'])
    assert not q.release(t['task_id'], 'w', t['lease_token'])
    assert q.fail(t['task_id'], 'w', 'late', t['lease_token']) == []
    results = [{'factor_id': a['execution']['factor_id'], 'metrics': {'rank_ic': .2}}]
    assert q.complete(t['task_id'], 'w', 1, 0, 9., results=results, lease_token=t['lease_token']) is None
    assert not q.lease('other', a['execution']['data_version'])
    assert not q.result_receipts()
    assert svc.reg.usage_today('research')['seconds'] == 0.


def test_completion_accepted_before_cancel_wins_even_before_registry_apply(research):
    ws, _, svc, sid, _ = research
    a = submit(ws, sid)
    t = svc.ades.lease('w', a['execution']['data_version'])
    results = [{'factor_id': a['execution']['factor_id'], 'metrics': {'rank_ic': .2}}]
    svc.ades.complete(t['task_id'], 'w', 1, 0, 2., results=results, lease_token=t['lease_token'])
    result = ws.cancel_experiment(a['id'], 'too late')
    assert result['status'] == 'completed'
    assert not svc.ades.cancellation_receipts()
    assert svc.reg.usage_today('research')['seconds'] == 2.
    assert ws.cancel_experiment(a['id'], 'repeat') == result


@pytest.mark.parametrize('lost', ['submission_response', 'dispatch'])
def test_cancel_missing_submission_receipt_fences_delayed_dispatch(research, monkeypatch, lost):
    ws, backend, svc, sid, _ = research
    obj, method = (backend, 'submit') if lost == 'submission_response' else (svc.ades, 'enqueue')
    original = getattr(obj, method)
    def crash(*args, **kwargs):
        if lost == 'submission_response': original(*args, **kwargs)
        raise OSError('response lost')
    monkeypatch.setattr(obj, method, crash)
    a = submit(ws, sid)
    assert a['status'] == 'running' and not a['execution'].get('job_id')
    monkeypatch.setattr(obj, method, original)
    assert ws.cancel_experiment(a['id'], 'stop after timeout')['status'] == 'cancelled'
    receipt = backend.submit(a['expr'], ws.get_dataset(a['dataset_id']), a['id'])
    # A dispatch already past its registry check is still fenced by the queue tombstone.
    svc.ades.enqueue(receipt['job_id'], receipt['owner'], receipt['spec'], receipt['data_version'],
                     [[receipt['factor_id'], a['expr']]], 1, idempotent=True)
    assert not svc.ades.lease('w', receipt['data_version'])
    assert svc.ades.status()['tasks'] == {'cancelled': 1}


def test_cancellation_intent_survives_backend_outage_and_restart(research, monkeypatch):
    ws, backend, svc, sid, _ = research
    a = submit(ws, sid)
    original = backend.cancel
    monkeypatch.setattr(backend, 'cancel', lambda *a: (_ for _ in ()).throw(OSError('offline')))
    pending = ws.cancel_experiment(a['id'], 'Persist this reason')
    assert pending['status'] == 'running' and pending['error']['retryable']
    assert pending['cancellation']['reason'] == 'Persist this reason'
    monkeypatch.setattr(backend, 'cancel', original)
    resumed = ResearchWorkspace(ws.path, {'factordb_ades': backend})
    assert resumed.reconcile_experiment(a['id'])['status'] == 'cancelled'
    assert resumed.get_session(sid)['remaining_experiments'] == 4


def test_queue_cancellation_receipt_recovers_after_lost_registry_update(research, monkeypatch):
    ws, backend, svc, sid, _ = research
    a = submit(ws, sid)
    original = svc.reg.apply_cancellation
    monkeypatch.setattr(svc.reg, 'apply_cancellation', lambda *a: (_ for _ in ()).throw(OSError('crash')))
    pending = ws.cancel_experiment(a['id'], 'queue committed')
    assert pending['status'] == 'running' and svc.ades.status()['tasks'] == {'cancelled': 1}
    monkeypatch.setattr(svc.reg, 'apply_cancellation', original)
    assert ws.reconcile_experiment(a['id'])['status'] == 'cancelled'
    assert svc.reg.job(a['execution']['job_id'])['status'] == 'cancelled'


def test_slow_poll_cannot_erase_cancellation_intent(research, monkeypatch):
    ws, backend, svc, sid, _ = research
    a = submit(ws, sid)
    started, release = Event(), Event()
    original = backend.poll
    def slow_poll(receipt):
        started.set()
        assert release.wait(10)
        return {'phase': 'queued', 'report': None}
    monkeypatch.setattr(backend, 'poll', slow_poll)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(ws.reconcile_experiment, a['id'])
        assert started.wait(10)
        result = ws.cancel_experiment(a['id'], 'concurrent stop')
        release.set()
        assert future.result() == result
    assert ws.get_experiment(a['id'])['status'] == 'cancelled'


def test_cancel_without_any_backend_submission_does_not_create_job(research, monkeypatch):
    ws, backend, svc, sid, _ = research
    original = backend.submit
    monkeypatch.setattr(backend, 'submit', lambda *a: (_ for _ in ()).throw(OSError('offline before submission')))
    a = submit(ws, sid)
    monkeypatch.setattr(backend, 'submit', original)
    result = ws.cancel_experiment(a['id'], 'stop before submission')
    assert result['status'] == 'cancelled' and not svc.reg.jobs()
    assert not svc.ades.lease('w', svc.db.qfv.snapshot_id)
    assert ws.verify_manifest(ws.export_session(sid))


def test_cancel_after_completed_submission_with_lost_response(research, monkeypatch):
    ws, backend, svc, sid, worker = research
    original = backend.submit
    def lose(*a, **kw):
        original(*a, **kw)
        assert worker.run_once()
        raise OSError('lost response')
    monkeypatch.setattr(backend, 'submit', lose)
    a = submit(ws, sid)
    monkeypatch.setattr(backend, 'submit', original)
    result = ws.cancel_experiment(a['id'], 'completion won')
    assert result['status'] == 'completed' and result['execution']['factor_id']
    ws.import_session(ws.export_session(sid))


def test_rest_and_mcp_cancel_same_attempt(research, controller, monkeypatch):
    from mcp.server.fastmcp import FastMCP
    from assay.mcp.research import register_research_tools
    import assay.api.routes.research as routes
    ws, _, _, sid, _ = research
    monkeypatch.setattr(routes, 'get_service', lambda: SimpleNamespace(research=ws))
    client, _ = controller
    a = submit(ws, sid)
    path = '/v1/research/experiments/' + a['id'] + '/cancel'
    assert client.post(path, json={}).status_code == 422
    assert client.post(path, json={'reason': '  '}).status_code == 400
    result = client.post(path, json={'reason': 'stop from browser'}).json()
    assert result['status'] == 'cancelled'
    mcp = FastMCP('cancel-test')
    register_research_tools(mcp, lambda: SimpleNamespace(research=ws))
    assert mcp._tool_manager.get_tool('assay_experiment_cancel').fn(a['id'], 'repeat') == result


def test_cancel_cached_result_preserves_completed_receipt(research):
    ws, _, svc, sid, worker = research
    first = submit(ws, sid)
    assert worker.run_once()
    assert ws.reconcile_experiment(first['id'])['status'] == 'completed'
    second = submit(ws, sid, request='cached')
    assert second['execution']['cached']
    assert ws.cancel_experiment(second['id'], 'already cached') == second
    assert svc.reg.usage_today('research')['factors'] == 1


def test_cancellation_transaction_failure_keeps_queue_and_receipt_consistent(research):
    ws, _, svc, sid, _ = research
    a = submit(ws, sid)
    with svc.ades._conn() as c:
        c.execute("CREATE TRIGGER crash_cancel BEFORE UPDATE ON tasks WHEN NEW.status='cancelled' "
                  "BEGIN SELECT RAISE(ABORT, 'cancel transaction interrupted'); END")
    pending = ws.cancel_experiment(a['id'], 'retry transaction')
    assert pending['status'] == 'running' and pending['error']['retryable']
    assert not svc.ades.cancellation_receipts()
    assert svc.ades.status()['tasks'] == {'queued': 1}
    with svc.ades._conn() as c: c.execute('DROP TRIGGER crash_cancel')
    assert ws.reconcile_experiment(a['id'])['status'] == 'cancelled'


def test_concurrent_completion_and_cancel_have_one_authoritative_outcome(research):
    from threading import Barrier
    ws, _, svc, sid, _ = research
    a = submit(ws, sid)
    t = svc.ades.lease('w', a['execution']['data_version'])
    gate = Barrier(2)
    results = [{'factor_id': a['execution']['factor_id'], 'metrics': {'rank_ic': .2}}]
    def complete():
        gate.wait(timeout=10)
        return svc.ades.complete(t['task_id'], 'w', 1, 0, 2., results=results, lease_token=t['lease_token'])
    def cancel():
        gate.wait(timeout=10)
        return ws.cancel_experiment(a['id'], 'race')
    with ThreadPoolExecutor(max_workers=2) as pool:
        completion = pool.submit(complete)
        cancellation = pool.submit(cancel)
        accepted = completion.result()
        final = cancellation.result()
    assert final['status'] == ('completed' if accepted else 'cancelled')
    assert svc.reg.usage_today('research')['seconds'] == (2. if accepted else 0.)
    assert ws.reconcile_experiment(a['id']) == final
    assert ws.get_session(sid)['remaining_experiments'] == 4


def test_synchronous_attempt_rejects_cancel_without_saving_intent(tmp_path):
    class Backend:
        def evaluate(self, expr, ds): raise AssertionError('not invoked')
    ws = ResearchWorkspace(tmp_path/'sync.sqlite', {'sync': Backend()})
    ds = ws.register_dataset('sync', 'TEST', ['2024-01-01','2024-01-31'], backend='sync')
    sid = ws.create_session(ds['id'], 'sync goal')['id']
    from assay.research_errors import ResearchError
    # Simulate a process interruption before synchronous evaluation returned.
    class Crash(BaseException): pass
    ws.backends['sync'].evaluate = lambda *a: (_ for _ in ()).throw(Crash())
    with pytest.raises(Crash): a = submit(ws, sid)
    a = ws.get_session(sid)['experiments'][0]
    with pytest.raises(ResearchError, match='does not support'):
        ws.cancel_experiment(a['id'], 'cannot cancel a native synchronous call')
    assert 'cancellation' not in ws.get_experiment(a['id'])
    assert ws.abandon_experiment(a['id'], 'checked stopped')['status'] == 'abandoned'


def test_cancel_before_registry_submission_fences_future_dispatch(research, monkeypatch):
    ws, backend, svc, sid, _ = research
    started, release = Event(), Event()
    original = backend.submit
    def delayed_submit(*args, **kwargs):
        started.set()
        assert release.wait(10)
        return original(*args, **kwargs)
    monkeypatch.setattr(backend, 'submit', delayed_submit)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(submit, ws, sid)
        assert started.wait(10)
        a = ws.get_session(sid)['experiments'][0]
        assert not svc.reg.jobs()
        cancelled = ws.cancel_experiment(a['id'], 'cancel before registry transaction')
        release.set()
        assert future.result() == cancelled
    assert cancelled['status'] == 'cancelled'
    svc.recover_results()
    assert svc.reg.jobs()[0]['status'] == 'cancelled'
    assert svc.ades.status()['tasks'] == {'cancelled': 1}
    assert not svc.ades.lease('w', svc.db.qfv.snapshot_id)
