"""Rolling upgrades must not lease token-fenced research work to old workers."""
from assay.ades.tasks import TaskQueue
import pytest
from assay.factordb import service
from tests.ades.test_ades import controller


def test_legacy_workers_keep_ordinary_jobs_but_cannot_take_research(controller):
    client,_ = controller
    svc=service.get()
    version=svc.db.qfv.snapshot_id
    svc.reg.add_spec('compat','TOP500','2024-12-02','2024-12-06',[1],'next_open')
    svc.ades.enqueue('research','owner','compat',version,[['f1','close']],1,idempotent=True)
    svc.ades.enqueue('ordinary','owner','compat',version,[['f2','open']],1)
    headers={'Authorization':'Bearer wk-1'}
    old={'worker':'legacy','panel':version,'panels':['unavailable',version],'info':{'host':'old'}}
    leased=client.post('/v1/ades/lease',headers=headers,json=old)
    assert leased.status_code==200 and leased.json()['job_id']=='ordinary'
    assert client.post('/v1/ades/lease',headers=headers,json=old).status_code==204
    upgraded={**old,'worker':'upgraded','info':{'lease_token_supported':True}}
    task=client.post('/v1/ades/lease',headers=headers,json=upgraded).json()
    assert task['job_id']=='research' and task['lease_token']
    body={'worker':'upgraded','task_id':task['task_id']}
    assert client.post('/v1/ades/heartbeat',headers=headers,json=body).status_code==409
    assert client.post('/v1/ades/heartbeat',headers=headers,json={**body,'lease_token':task['lease_token']}).status_code==200
    assert client.post('/v1/ades/lease',headers=headers,json={**old,'panels':[['bad']]}).status_code==422


def test_retired_snapshots_leave_recoverable_failure_receipts(tmp_path):
    q=TaskQueue(tmp_path/'queue.sqlite3')
    q.enqueue('research','owner','spec','retired',[['f1','close'],['f2','open']],2,idempotent=True)
    retired=q.retire_versions(['current'])
    assert len(retired)==1 and q.status()['tasks']=={'failed':1}
    task,results,seconds=q.result_receipts()[0]
    assert task['status']=='failed' and seconds==0
    assert {r['factor_id'] for r in results}=={'f1','f2'}
    assert all(r['metrics'] is None and 'retired' in r['error'] for r in results)
    assert q.retire_versions(['current'])==[]


@pytest.mark.parametrize('execution',['next_close','close'])
def test_new_execution_modes_require_upgraded_workers(controller,execution):
    from assay.ades.worker import Worker
    from assay.data.store import QFVPanelStore
    from tests.ades.test_ades import _ClientTransport
    client,panel=controller
    svc=service.get()
    job=svc.submit('owner',['ts_delta(close,2)'],universe='TOP500',start='2024-12-02',horizons=[1],execution=execution)
    response=client.post('/v1/ades/lease',headers={'Authorization':'Bearer wk-1'},
        json={'worker':'legacy','panel':svc.db.qfv.snapshot_id,'info':{}})
    assert response.status_code==204
    worker=Worker(_ClientTransport(client,'wk-1'),QFVPanelStore(panel),'upgraded')
    assert worker.run_once()
    assert svc.job(job['job_id'])['status']=='done'
