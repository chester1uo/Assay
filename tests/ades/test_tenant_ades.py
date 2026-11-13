"""Two users share ADES workers but never a factor/job/score registry."""
import pytest
from fastapi.testclient import TestClient

from assay.api import auth
from assay.api.app import create_app
from assay.config import AssayConfig
from assay.service import AssayService
from assay.factordb import service
from assay.ades.worker import Worker
from assay.data.store import QFVPanelStore
from tests.ades.test_ades import controller, _ClientTransport
from tests.api.test_research_tenants import _mcp


@pytest.fixture
def tenant_controller(controller, tmp_path, monkeypatch):
    _, panel = controller
    monkeypatch.setenv('ASSAY_ACCOUNTS', '1')
    monkeypatch.setenv('ASSAY_STATE_DIR', str(tmp_path/'state'))
    monkeypatch.setenv('ASSAY_API_KEYS', 'svc-key')
    monkeypatch.delenv('ASSAY_ADMIN_KEYS', raising=False)
    monkeypatch.delenv('ASSAY_AUTH_DB', raising=False)
    monkeypatch.delenv('ASSAY_SECRET_KEY', raising=False)
    auth._reset_for_tests()
    monkeypatch.setattr(AssayService, '_instance', None)
    AssayService.init(AssayConfig.for_tests(data_dir=str(tmp_path/'data')))
    alice=auth.account_store().create_user('alice','correct horse battery')
    bob=auth.account_store().create_user('bob','correct horse battery')
    _, akey=auth.account_store().create_api_key(alice.id,'agent')
    _, bkey=auth.account_store().create_api_key(bob.id,'agent')
    with TestClient(create_app()) as client:
        yield client, panel, akey, bkey
    auth._reset_for_tests()


def test_private_rest_mcp_jobs_and_real_worker_do_not_cross_accounts(tenant_controller):
    client,panel,akey,bkey=tenant_controller
    assert client.get('/.well-known/assay.json').json()['research']['private_compute_configured']
    a={'Authorization':'Bearer '+akey}; b={'Authorization':'Bearer '+bkey}
    request={'exprs':['ts_delta(close,2)'], 'universe':'TOP500', 'start':'2024-12-02', 'horizons':[1]}
    first=client.post('/v1/fdb/submit',headers=a,json=request)
    second=client.post('/v1/fdb/submit',headers=b,json=request)
    assert first.status_code==200, first.text
    assert second.status_code==200, second.text
    ajob,bjob=first.json()['job_id'],second.json()['job_id']
    assert ajob and bjob and ajob!=bjob
    assert client.get('/v1/fdb/jobs/'+ajob,headers=b).json()['error']['code']=='NOT_FOUND'
    assert _mcp(client,bkey,'fdb_job',{'job_id':ajob})['error']['code']=='NOT_FOUND'
    worker=Worker(_ClientTransport(client,'wk-1'),QFVPanelStore(panel),'private-worker')
    assert worker.run_once()
    # The second account has not inherited the first account's score or budget.
    assert client.get('/v1/fdb/jobs/'+ajob,headers=a).json()['status']=='done'
    assert client.get('/v1/fdb/jobs/'+bjob,headers=b).json()['status']=='queued'
    assert client.get('/v1/fdb/factors',headers=b).json()['count']==0
    assert client.get('/v1/fdb/budget',headers=b).json()['factors']==0
    assert worker.run_once()
    assert client.get('/v1/fdb/jobs/'+bjob,headers=b).json()['status']=='done'
    assert client.get('/v1/fdb/factors',headers=a).json()['count']==1
    assert client.get('/v1/fdb/factors',headers=b).json()['count']==1
    assert _mcp(client,akey,'fdb_query',{'source':'compute'})['count']==1
    assert not worker.run_once()


def test_private_research_async_recovery_and_cancel(tenant_controller):
    client,panel,akey,bkey=tenant_controller
    a={'Authorization':'Bearer '+akey}; b={'Authorization':'Bearer '+bkey}
    version=service.get().db.qfv.snapshot_id
    end=str(service.get().db.grid().end)
    ds=client.post('/v1/research/datasets',headers=a,json={'name':'ades','universe':'TOP500',
        'period':['2024-12-02',end], 'adj':'total','horizons':[1],
        'backend':'factordb_ades','snapshot_id':version})
    assert ds.status_code==200,ds.text
    sess=client.post('/v1/research/sessions',headers=a,json={'dataset_id':ds.json()['id'],'goal':'private ADES'})
    assert sess.status_code==200,sess.text
    sid=sess.json()['id']
    attempt=client.post('/v1/research/sessions/'+sid+'/experiments',headers=a,
                        json={'expr':'ts_delta(close,2)','hypothesis':'private','request_id':'a'})
    assert attempt.status_code==200,attempt.text
    attempt=attempt.json()
    assert attempt['status']=='running'
    assert client.get('/v1/research/experiments/'+attempt['id'],headers=b).status_code==400
    # A fresh registry facade sees the queued task and later receives its worker result.
    service._tenant_services.clear()
    worker=Worker(_ClientTransport(client,'wk-1'),QFVPanelStore(panel),'research-worker')
    assert worker.run_once()
    path='/v1/research/experiments/'+attempt['id']+'/reconcile'
    done=client.post(path,headers=a,json={})
    assert done.status_code==200,done.text
    assert done.json()['status']=='completed'
    assert client.get('/v1/fdb/jobs/'+attempt['execution']['job_id'],headers=b).json()['error']['code']=='NOT_FOUND'
    manifest=client.get('/v1/research/sessions/'+sid+'/manifest',headers=a)
    assert manifest.status_code == 200, manifest.text
    restored=client.post('/v1/research/sessions/import',headers=b,json=manifest.json())
    assert restored.status_code == 200, restored.text
    second=client.post('/v1/research/sessions/'+sid+'/experiments',headers=a,
                       json={'expr':'ts_delta(close,3)','hypothesis':'cancel private','request_id':'b'}).json()
    assert second['status']=='running'
    leased=client.post('/v1/ades/lease',headers={'Authorization':'Bearer wk-1'},json={
        'worker':'cancel-worker','panel':version,'info':{'lease_token_supported':True}})
    assert leased.status_code==200,leased.text
    task=leased.json()
    cancel_path='/v1/research/experiments/'+second['id']+'/cancel'
    assert client.post(cancel_path,headers=b,json={'reason':'not mine'}).status_code==400
    cancelled=client.post(cancel_path,headers=a,json={'reason':'no longer needed'})
    assert cancelled.status_code==200,cancelled.text
    assert cancelled.json()['status']=='cancelled'
    late=client.post('/v1/ades/complete',headers={'Authorization':'Bearer wk-1'},json={
        'task_id':task['task_id'],'lease_token':task['lease_token'], 'worker':'cancel-worker',
        'seconds':1,'results':[{'factor_id':task['items'][0][0],'metrics':{'rank_ic':.5}}]})
    assert late.status_code==409
    assert client.get('/v1/fdb/factors',headers=b).json()['count']==0


def test_private_job_resumes_after_controller_restart_before_any_user_request(tenant_controller, tmp_path, monkeypatch):
    client,panel,akey,bkey=tenant_controller
    headers={'Authorization':'Bearer '+akey}
    req={'exprs':['ts_delta(close,4)'],'universe':'TOP500','start':'2024-12-02','horizons':[1]}
    response=client.post('/v1/fdb/submit',headers=headers,json=req)
    assert response.status_code==200,response.text
    job_id=response.json()['job_id']
    # Simulate a new controller process with neither singleton in memory.
    monkeypatch.setenv('ASSAY_DATA_DIR',str(tmp_path/'data'))
    monkeypatch.setenv('ASSAY_CONFIG_FILE',str(tmp_path/'empty-config.json'))
    service.get().close()
    monkeypatch.setattr(service,'_service',None)
    monkeypatch.setattr(AssayService,'_instance',None)
    service._tenant_services.clear()
    worker=Worker(_ClientTransport(client,'wk-1'),QFVPanelStore(panel),'restarted-worker')
    assert worker.run_once()
    job=client.get('/v1/fdb/jobs/'+job_id,headers=headers)
    assert job.status_code==200,job.text
    assert job.json()['status']=='done'
    assert client.get('/v1/fdb/jobs/'+job_id,headers={'Authorization':'Bearer '+bkey}).json()['error']['code']=='NOT_FOUND'


def test_request_identity_and_cancellation_are_account_scoped(tenant_controller):
    from assay.integrations.factordb import FactorDBResearchBackend
    from assay.research_errors import ResearchError
    client,_,akey,bkey=tenant_controller
    a={'Authorization':'Bearer '+akey}; b={'Authorization':'Bearer '+bkey}
    client.get('/v1/research/backends',headers=b)  # create Bob's private root
    users={u.username:u for u in auth.account_store().list_users()}
    owner_a=service.owner_for_tenant('account:'+users['alice'].workspace_id)
    owner_b=service.owner_for_tenant('account:'+users['bob'].workspace_id)
    version=service.get().db.qfv.snapshot_id
    end=str(service.get().db.grid().end)
    ds=client.post('/v1/research/datasets',headers=a,json={'name':'same','universe':'TOP500',
        'period':['2024-12-02',end], 'adj':'total','horizons':[1],
        'backend':'factordb_ades','snapshot_id':version}).json()
    a_backend=FactorDBResearchBackend(lambda: service.for_owner(owner_a),owner=owner_a)
    b_backend=FactorDBResearchBackend(lambda: service.for_owner(owner_b),owner=owner_b)
    x=a_backend.submit('ts_delta(close,2)',ds,'same-request')
    y=b_backend.submit('ts_delta(close,2)',ds,'same-request')
    assert x['job_id'] != y['job_id']
    with pytest.raises(ResearchError,match='does not belong'):
        b_backend.cancel({**y,'job_id':x['job_id']},'forged')
    with pytest.raises(ValueError,match='another account'):
        service.get().ades.cancel_job(x['job_id'],'forged',owner=owner_b)
    assert b_backend.cancel(y,'stop mine')['phase']=='cancelled'
    assert service.for_owner(owner_a).reg.job(x['job_id'])['status']=='queued'


def test_private_novelty_recovers_and_merges_durable_worker_receipts(tenant_controller):
    client,panel,akey,bkey=tenant_controller
    a={'Authorization':'Bearer '+akey}; b={'Authorization':'Bearer '+bkey}
    request={'exprs':['ts_delta(close,2)'],'universe':'TOP500','start':'2024-12-02','horizons':[1]}
    assert client.post('/v1/fdb/submit',headers=a,json=request).status_code==200
    worker=Worker(_ClientTransport(client,'wk-1'),QFVPanelStore(panel),'novelty-worker')
    assert worker.run_once()
    body={'expr':'ts_delta(close,4)','universe':'TOP500','start':'2024-12-02','horizons':[1]}
    assert client.post('/v1/fdb/novelty',headers=a,json=body).json()['library']==1
    user=auth.account_store().list_users()[0]
    old=service.for_owner(service.owner_for_tenant('account:'+user.workspace_id))
    service._tenant_services.clear()
    assert client.post('/v1/fdb/novelty',headers=a,json=body).json()['library']==1
    assert client.post('/v1/fdb/submit',headers=a,json={**request,'exprs':['ts_delta(close,3)']}).status_code==200
    assert worker.run_once()
    assert old.novelty('ts_delta(close,4)',universe='TOP500',start='2024-12-02',horizons=[1])['library']==2
    assert client.post('/v1/fdb/novelty',headers=b,json=body).json()['library']==0


def test_reaper_recovers_other_accounts_when_one_registry_is_unavailable(tenant_controller):
    client,_,akey,bkey=tenant_controller
    a={'Authorization':'Bearer '+akey}; b={'Authorization':'Bearer '+bkey}
    request={'exprs':['ts_delta(close,2)'],'universe':'TOP500','start':'2024-12-02','horizons':[1]}
    jobs=[client.post('/v1/fdb/submit',headers=h,json=request).json()['job_id'] for h in (a,b)]
    controller=service.get()
    for index in range(2):
        task=controller.ades.lease('w'+str(index),controller.db.qfv.snapshot_id)
        result=[{'factor_id':task['items'][0][0],'metrics':{'rank_ic':.2,'rank_icir':.3}}]
        assert controller.ades.complete(task['task_id'],'w'+str(index),1,0,1,
                                        results=result,lease_token=task['lease_token'])
    users={u.username:u for u in auth.account_store().list_users()}
    broken=service.owner_for_tenant('account:'+users['alice'].workspace_id)
    router=controller.owner_router
    def sometimes_unavailable(owner):
        if owner==broken:
            raise RuntimeError('test: account directory unavailable')
        return router(owner)
    controller.owner_router=sometimes_unavailable
    try:
        controller.recover_results(best_effort=True)
        assert client.get('/v1/fdb/jobs/'+jobs[1],headers=b).json()['status']=='done'
        assert client.get('/v1/fdb/jobs/'+jobs[0],headers=a).json()['status']=='queued'
    finally:
        controller.owner_router=router
    controller.recover_results(best_effort=True)
    assert client.get('/v1/fdb/jobs/'+jobs[0],headers=a).json()['status']=='done'


def _parallel_initialize_registries(paths):
    from assay.factordb.registry import Registry
    from assay.ades.tasks import TaskQueue
    r=Registry(paths[0]); q=TaskQueue(paths[1])
    with r._conn() as c:
        assert 'version' in {x[1] for x in c.execute('PRAGMA table_info(jobs)')}
    with q._conn() as c:
        assert 'lease_token' in {x[1] for x in c.execute('PRAGMA table_info(tasks)')}
    return True


def test_concurrent_first_time_registry_initialization(tmp_path):
    from concurrent.futures import ProcessPoolExecutor
    from multiprocessing import get_context
    paths=(str(tmp_path/'registry.sqlite3'),str(tmp_path/'queue.sqlite3'))
    with ProcessPoolExecutor(max_workers=8, mp_context=get_context('spawn')) as pool:
        assert all(pool.map(_parallel_initialize_registries,[paths]*24))


@pytest.mark.parametrize('crash_stage', ['factor_ids', 'queue'])
def test_normal_private_job_dispatch_recovers_after_queue_write_failure(tenant_controller, monkeypatch, crash_stage):
    client,panel,akey,bkey=tenant_controller
    a={'Authorization':'Bearer '+akey}; b={'Authorization':'Bearer '+bkey}
    controller=service.get()
    client.get('/v1/research/backends',headers=a)
    user=next(u for u in auth.account_store().list_users() if u.username=='alice')
    owner=service.owner_for_tenant('account:'+user.workspace_id)
    scoped=service.for_owner(owner)
    enqueue=controller.ades.enqueue
    update_job=scoped.reg.update_job
    def offline(*args, **kwargs):
        raise OSError('interrupted before queue commit')
    if crash_stage == 'factor_ids':
        monkeypatch.setattr(scoped.reg,'update_job',offline)
    else:
        monkeypatch.setattr(controller.ades,'enqueue',offline)
    with pytest.raises(OSError,match='interrupted'):
        scoped.submit(owner,['ts_delta(close,2)'],universe='TOP500',start='2024-12-02',horizons=[1])
    monkeypatch.setattr(controller.ades,'enqueue',enqueue)
    monkeypatch.setattr(scoped.reg,'update_job',update_job)
    jobs=client.get('/v1/fdb/jobs',headers=a).json()['jobs']
    assert len(jobs)==1 and jobs[0]['status']=='queued'
    # Recovery is independent of Alice's next request and safe to repeat.
    service._tenant_services.clear()
    service.recover_private_dispatches()
    service.recover_private_dispatches()
    worker=Worker(_ClientTransport(client,'wk-1'),QFVPanelStore(panel),'dispatch-recovery-worker')
    assert worker.run_once() and not worker.run_once()
    assert client.get('/v1/fdb/jobs/'+jobs[0]['id'],headers=a).json()['status']=='done'
    assert client.get('/v1/fdb/jobs/'+jobs[0]['id'],headers=b).json()['error']['code']=='NOT_FOUND'


def test_private_generic_job_requires_worker_lease_token(tenant_controller):
    client,_,akey,_=tenant_controller
    a={'Authorization':'Bearer '+akey}
    result=client.post('/v1/fdb/submit',headers=a,json={'exprs':['ts_delta(close,2)'],
        'universe':'TOP500','start':'2024-12-02','horizons':[1]}).json()
    queue=service.get().ades
    version=service.get().db.qfv.snapshot_id
    assert queue.lease('legacy',version,supports_research_leases=False) is None
    task=queue.lease('modern',version,supports_research_leases=True)
    assert task['task_id'].startswith('rtsk_')
    payload=[{'factor_id':task['items'][0][0],'metrics':{'rank_ic':.2,'rank_icir':.3}}]
    assert queue.complete(task['task_id'],'modern',1,0,1,results=payload) is None
    assert queue.complete(task['task_id'],'modern',1,0,1,results=payload,lease_token=task['lease_token'])
    # A controller crash after queue commit but before registry apply is recoverable.
    service._tenant_services.clear()
    service.get().recover_results(best_effort=True)
    job=client.get('/v1/fdb/jobs/'+result['job_id'],headers=a).json()
    assert job['status']=='done' and job['done']==1
    service.get().recover_results(best_effort=True)
    assert client.get('/v1/fdb/budget',headers=a).json()['factors']==1


def test_old_controller_cannot_duplicate_recovered_chunk_during_rolling_upgrade(tenant_controller, monkeypatch):
    client,panel,akey,_=tenant_controller
    a={'Authorization':'Bearer '+akey}
    controller=service.get()
    client.get('/v1/research/backends',headers=a)
    user=next(u for u in auth.account_store().list_users() if u.username=='alice')
    owner=service.owner_for_tenant('account:'+user.workspace_id)
    scoped=service.for_owner(owner)
    enqueue=controller.ades.enqueue
    def interrupted(*args, **kwargs):
        raise OSError('old controller stopped before enqueue')
    monkeypatch.setattr(controller.ades,'enqueue',interrupted)
    with pytest.raises(OSError):
        scoped.submit(owner,['ts_delta(close,2)'],universe='TOP500',start='2024-12-02',horizons=[1])
    monkeypatch.setattr(controller.ades,'enqueue',enqueue)
    job_id=client.get('/v1/fdb/jobs',headers=a).json()['jobs'][0]['id']
    scoped.recover_dispatches()  # new controller's deterministic task
    job=scoped.reg.job(job_id)
    # Older controller resumes its pre-upgrade random-ID enqueue.
    enqueue(job_id,owner,job['spec'],job['version'],list(map(list,zip(job['factor_ids'],job['exprs']))),64)
    with controller.ades._conn() as connection:
        assert connection.execute('SELECT count(*) FROM tasks WHERE job_id=?',(job_id,)).fetchone()[0]==1
    worker=Worker(_ClientTransport(client,'wk-1'),QFVPanelStore(panel),'upgrade-worker')
    assert worker.run_once() and not worker.run_once()
    done=client.get('/v1/fdb/jobs/'+job_id,headers=a).json()
    assert (done['done'],done['total'])==(1,1)
    assert client.get('/v1/fdb/budget',headers=a).json()['factors']==1


def test_idempotent_generic_submit_across_rest_mcp_and_concurrency(tenant_controller):
    from concurrent.futures import ThreadPoolExecutor
    client,panel,akey,bkey=tenant_controller
    a={'Authorization':'Bearer '+akey}; b={'Authorization':'Bearer '+bkey}
    body={'exprs':['ts_delta(close,2)','ts_delta(close,2.0)','bad('],
          'universe':'TOP500','start':'2024-12-02','horizons':[1], 'request_id':'batch-001'}
    def send(_):
        response=client.post('/v1/fdb/submit',headers=a,json=body)
        assert response.status_code == 200,response.text
        return response.json()
    with ThreadPoolExecutor(max_workers=8) as pool:
        replies=list(pool.map(send,range(12)))
    assert all(row == replies[0] for row in replies)
    job=replies[0]['job_id']
    assert job and replies[0]['queued']==1 and len(replies[0]['duplicates'])==1 and len(replies[0]['rejected'])==1
    via_mcp=_mcp(client,akey,'fdb_submit',body)
    assert via_mcp==replies[0]
    conflict=client.post('/v1/fdb/submit',headers=a,json={**body,'exprs':['close']})
    assert conflict.status_code==409
    assert _mcp(client,akey,'fdb_submit',{**body,'exprs':['close']})['error']['code']=='REQUEST_CONFLICT'
    other=client.post('/v1/fdb/submit',headers=b,json=body).json()
    assert other['job_id'] and other['job_id']!=job
    worker=Worker(_ClientTransport(client,'wk-1'),QFVPanelStore(panel),'idempotent-worker')
    assert worker.run_once() and worker.run_once() and not worker.run_once()
    assert client.get('/v1/fdb/jobs/'+job,headers=a).json()['done']==1
    assert client.get('/v1/fdb/budget',headers=a).json()['factors']==1
    # Retries remain retrievable even after the daily budget is exhausted.
    user=next(u for u in auth.account_store().list_users() if u.username=='alice')
    private=service.for_owner(service.owner_for_tenant('account:'+user.workspace_id))
    private.daily_budget_s=1e-12
    assert send(0)==replies[0]
    assert client.post('/v1/fdb/submit',headers=a,json={**body,'request_id':'new-budget'}).status_code==429


def test_idempotent_generic_submission_survives_lost_dispatch(tenant_controller, monkeypatch):
    client,panel,akey,_=tenant_controller
    a={'Authorization':'Bearer '+akey}
    request={'exprs':['ts_delta(close,2)'],'universe':'TOP500','start':'2024-12-02',
             'horizons':[1],'request_id':'retry-after-crash'}
    controller=service.get()
    original=controller.ades.enqueue
    monkeypatch.setattr(controller.ades,'enqueue',lambda *a,**kw: (_ for _ in ()).throw(OSError('offline')))
    first=client.post('/v1/fdb/submit',headers=a,json=request)
    assert first.status_code==200 and first.json()['job_id']
    monkeypatch.setattr(controller.ades,'enqueue',original)
    service._tenant_services.clear()
    retry=client.post('/v1/fdb/submit',headers=a,json=request)
    assert retry.status_code==200 and retry.json()==first.json()
    worker=Worker(_ClientTransport(client,'wk-1'),QFVPanelStore(panel),'retry-worker')
    assert worker.run_once() and not worker.run_once()
    assert client.get('/v1/fdb/jobs/'+first.json()['job_id'],headers=a).json()['done']==1


def test_service_key_idempotency_requires_fenced_worker(tenant_controller):
    client,_,_,_=tenant_controller
    h={'Authorization':'Bearer svc-key'}
    request={'exprs':['ts_delta(close,2)'],'universe':'TOP500','start':'2024-12-02',
             'horizons':[1],'request_id':'service-dedup'}
    response=client.post('/v1/fdb/submit',headers=h,json=request)
    assert response.status_code==200,response.text
    assert client.post('/v1/fdb/submit',headers=h,json=request).json()==response.json()
    queue=service.get().ades
    version=service.get().db.qfv.snapshot_id
    assert queue.lease('legacy',version,supports_research_leases=False) is None
    task=queue.lease('fenced',version,supports_research_leases=True)
    assert task['task_id'].startswith('rtsk_')
    results=[{'factor_id':task['items'][0][0],'metrics':{'rank_ic':.2,'rank_icir':.3}}]
    assert queue.complete(task['task_id'],'fenced',1,0,1,results=results) is None
    assert queue.complete(task['task_id'],'fenced',1,0,1,results=results,lease_token=task['lease_token'])
    service.get().recover_results(best_effort=True)
    assert client.get('/v1/fdb/jobs/'+response.json()['job_id'],headers=h).json()['done']==1


def test_unauthenticated_http_rest_and_mcp_share_request_identity(controller):
    request={'exprs':['ts_delta(close,2)'],'universe':'TOP500','start':'2024-12-02',
             'horizons':[1],'request_id':'local-http-retry'}
    with TestClient(create_app()) as client:
        result=client.post('/v1/fdb/submit',json=request)
        assert result.status_code==200,result.text
        retry=_mcp(client,'','fdb_submit',request)
        assert retry==result.json()
        assert len(service.get().reg.jobs('anonymous'))==1
