"""Research REST uses the durable MCP/SDK workspace, including budget semantics."""
import pytest
from fastapi.testclient import TestClient
from assay.api.app import create_app
from assay.demo import prepare_demo
from assay.service import AssayService


@pytest.fixture
def workbench(tmp_path, monkeypatch):
    monkeypatch.delenv('ASSAY_API_KEYS', raising=False)
    monkeypatch.setattr(AssayService, '_instance', None)
    service, dataset = prepare_demo(tmp_path / 'data')
    with TestClient(create_app()) as client:
        yield client, service.research, dataset


def test_workbench_real_failure_revision_and_mcp_state(workbench):
    client, ws, dataset = workbench
    response = client.post('/v1/research/sessions', json={'dataset_id':dataset['id'], 'goal':'reversal', 'max_experiments':3})
    assert response.status_code == 200, response.text
    sid = response.json()['id']
    base = '/v1/research/sessions/' + sid
    payload = {'expr':'unknown_operator(close)', 'hypothesis':'diagnostic fixture', 'request_id':'failure'}
    failed = client.post(base+'/experiments',json=payload).json()
    assert failed['status'] == 'failed' and failed['agent'] == 'human:webui'
    assert client.post(base+'/experiments',json=payload).json()['id'] == failed['id']
    assert len(ws.get_session(sid)['experiments']) == 1
    fixed = client.post(base+'/experiments',json={'expr':'-cs_rank(ts_returns(close,5))',
        'hypothesis':'valid revision', 'parent_id':failed['id'], 'request_id':'revision'}).json()
    assert fixed['status'] == 'completed' and fixed['parent_id'] == failed['id']
    # An independent MCP/SDK write becomes visible in the workbench.
    ws.add_note(sid,'External agent handoff',agent='test:mcp')
    data = client.get(base).json()
    assert data['notes'][0]['agent'] == 'test:mcp' and data['remaining_experiments'] == 1
    assert client.post(base+'/notes',json={'text':'Human review'}).status_code == 200
    manifest = client.get(base+'/manifest').json()
    assert ws.verify_manifest(manifest)
    assert manifest['state']['notes'][-1]['agent'] == 'human:webui'
    assert client.get('/v1/research/sessions').json()[0]['id'] == sid
    assert client.get('/v1/research/datasets').json()[0]['id'] == dataset['id']


def test_workbench_auth_validation_and_budget_cannot_be_bypassed(workbench, monkeypatch):
    client, ws, ds = workbench
    monkeypatch.setenv('ASSAY_API_KEYS','test-key')
    assert client.get('/v1/research/datasets').status_code == 401
    assert client.post('/v1/research/sessions',json={}).status_code == 401
    headers={'X-API-Key':'test-key'}
    assert client.get('/v1/research/datasets',headers=headers).status_code == 200
    for budget in [True, 0, 1.5]:
        r=client.post('/v1/research/sessions',headers=headers,json={'dataset_id':ds['id'],'goal':'x','max_experiments':budget})
        assert r.status_code == 422
    sid=ws.create_session(ds['id'],'bounded',max_experiments=1)['id']
    base='/v1/research/sessions/'+sid+'/experiments'
    payload={'expr':'close','hypothesis':'x','request_id':'one'}
    first=client.post(base,headers=headers,json=payload)
    assert first.status_code == 200
    assert client.post(base,headers=headers,json=dict(payload,request_id='two')).status_code == 400
    assert len(ws.get_session(sid)['experiments']) == 1
    assert client.post(base,headers=headers,json=dict(payload,agent='fake:llm')).status_code == 422
    assert client.post(base,headers=headers,json=dict(payload,expr='open')).status_code == 400


def test_running_request_can_be_inspected_and_retried_without_duplicate_execution(workbench):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    client, ws, _ = workbench
    started, release = Event(), Event()
    class SlowBackend:
        calls = 0
        def evaluate(self, expr, dataset):
            self.calls += 1
            started.set()
            assert release.wait(10)
            return {'rank_ic': .1}
    backend=SlowBackend()
    ws.register_backend('slow',backend)
    ds=ws.register_dataset('slow','TEST',['2024-01-01','2024-02-01'],backend='slow')
    sid=ws.create_session(ds['id'],'concurrent reads',max_experiments=2)['id']
    path='/v1/research/sessions/'+sid
    body={'expr':'close','hypothesis':'test','request_id':'same'}
    with ThreadPoolExecutor() as pool:
        future=pool.submit(client.post,path+'/experiments',json=body)
        try:
            assert started.wait(5)
            retry=client.post(path+'/experiments',json=body)
            assert retry.status_code == 200 and retry.json()['status']=='running'
            assert client.get(path).json()['remaining_experiments']==1
        finally:
            release.set()
        result=future.result(timeout=10)
    assert result.json()['status']=='completed'
    assert backend.calls==1 and retry.json()['id']==result.json()['id']
