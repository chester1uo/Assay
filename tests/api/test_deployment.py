"""Public contract, production transport controls and remote SDK interoperability."""
import asyncio
from contextlib import contextmanager
import socket
import threading
import time

import pytest
from fastapi.testclient import TestClient

from assay.api.app import create_app
from assay.demo import prepare_demo
from assay.service import AssayService


@pytest.fixture
def production(monkeypatch, tmp_path):
    monkeypatch.setenv('ASSAY_DEPLOYMENT', 'production')
    monkeypatch.setenv('ASSAY_PUBLIC_URL', 'https://assay.qfinverse.com/')
    monkeypatch.setenv('ASSAY_API_KEYS', 'test-deployment-key')
    monkeypatch.setenv('ASSAY_MCP_HTTP', '1')
    monkeypatch.delenv('ASSAY_ACCOUNTS', raising=False)
    monkeypatch.delenv('ASSAY_CORS_ORIGINS', raising=False)
    monkeypatch.setenv('ASSAY_CONFIG_FILE', str(tmp_path/'config.json'))
    monkeypatch.setattr(AssayService, '_instance', None)
    return prepare_demo(tmp_path/'data')


def test_production_configuration_rejects_missing_auth_or_unsafe_url(production, monkeypatch):
    monkeypatch.delenv('ASSAY_API_KEYS')
    with pytest.raises(ValueError, match='ASSAY_ACCOUNTS'):
        create_app()
    monkeypatch.setenv('ASSAY_API_KEYS', 'test-key')
    for value in ['http://assay.qfinverse.com', 'https://user:pass@assay.qfinverse.com',
                  'https://assay.qfinverse.com/subpath', 'https://assay.qfinverse.com?key=bad']:
        monkeypatch.setenv('ASSAY_PUBLIC_URL', value)
        with pytest.raises(ValueError):
            create_app()
    monkeypatch.setenv('ASSAY_PUBLIC_URL', 'https://assay.qfinverse.com')
    monkeypatch.setenv('ASSAY_MCP_HTTP','0')
    with pytest.raises(ValueError, match='MCP'):
        create_app()


def test_public_contract_auth_origin_host_alias_and_cors(production):
    headers={'Authorization':'Bearer test-deployment-key','Accept':'application/json, text/event-stream'}
    body={'jsonrpc':'2.0','id':1,'method':'tools/list','params':{}}
    with TestClient(create_app(), base_url='https://assay.qfinverse.com') as c:
        discovery=c.get('/.well-known/assay.json').json()
        assert discovery['mcp']['url']=='https://assay.qfinverse.com/mcp/'
        assert discovery['mcp']['tool_count']==40 and discovery['authentication']['required']
        assert not discovery['authentication']['oauth']
        assert discovery['research']['scope']=='service-workspace'
        assert discovery['research']['tenant_isolation'] is False
        assert discovery['research']['library_scope'] == 'local-shared'
        assert discovery['research']['panel_session_scope'] == 'process'
        assert discovery['research']['private_compute_registry'] is False
        schema=c.get('/openapi.json').json()
        assert schema['servers']==[{'url':'https://assay.qfinverse.com'}]
        assert schema['paths']['/v1/research/datasets']['post']['security']==[
            {'AssayBearer':[]},{'AssayApiKey':[]}]
        assert c.get('/ready').status_code==401
        assert c.get('/ready',headers=headers).json()['status']=='ready'
        assert c.get('/v1/research/backends').status_code==401
        assert c.post('/mcp/',json=body).status_code==401
        assert c.post('/mcp/',headers=headers,json=body).status_code==200
        assert c.post('/mcp/',headers=dict(headers,Origin='https://evil.example'),json=body).status_code==403
        assert c.post('/mcp/',headers=dict(headers,Host='evil.example'),json=body).status_code==400
        assert c.post('/mcp/',headers=dict(headers,Host='assay.qfinverse.com:443',
                                         Origin='https://assay.qfinverse.com:443'),json=body).status_code==200
        assert c.post('/mcp/',headers=dict(headers,Host='assay.qfinverse.com:444'),json=body).status_code==421
        response=c.post('/mcp',headers=headers,json=body,follow_redirects=False)
        assert response.status_code==308 and response.headers['location']=='/mcp/'
        response=c.options('/mcp/',headers={'Origin':'https://assay.qfinverse.com',
            'Access-Control-Request-Method':'POST','Access-Control-Request-Headers':'authorization,mcp-protocol-version'})
        assert response.status_code==200
        response=c.get('/health',headers={'Origin':'https://assay.qfinverse.com'})
        assert 'Mcp-Session-Id' in response.headers['access-control-expose-headers']


def test_readiness_reports_storage_failure_without_local_paths(production, monkeypatch):
    from importlib import import_module
    module = import_module('assay.api.app')
    def unavailable():
        raise OSError('/private/path is unavailable')
    # The endpoint captures this accessor at app construction.
    monkeypatch.setattr(module,'get_service',unavailable)
    with TestClient(create_app(),base_url='https://assay.qfinverse.com') as c:
        response=c.get('/ready',headers={'X-API-Key':'test-deployment-key'})
        assert response.status_code==503 and not response.json()['checks']['research_store']
        assert '/private' not in response.text


def test_rest_covers_dataset_handoff_inspection_and_replay(production):
    service,_=production
    with TestClient(create_app(),base_url='https://assay.qfinverse.com',
                    headers={'X-API-Key':'test-deployment-key'}) as c:
        ds=c.post('/v1/research/datasets',json=dict(name='remote',universe='DEMO',
            period=['2024-01-02','2024-06-28'],portfolio={'enabled':False})).json()
        # Dataset definition semantics are owned by the canonical workspace.
        assert 'id' in ds, ds
        session=c.post('/v1/research/sessions',json=dict(dataset_id=ds['id'],goal='remote parity',max_experiments=2)).json()
        sid=session['id']
        args=dict(expr='close',hypothesis='unavailable-universe fixture',request_id='a')
        attempt=c.post(f'/v1/research/sessions/{sid}/experiments',json=args).json()
        eid=attempt['id']
        assert attempt['status']=='failed'
        assert c.get(f'/v1/research/experiments/{eid}').json()['id']==eid
        assert c.get(f'/v1/research/experiments/{eid}?limit=101').status_code==422
        assert c.get(f'/v1/research/sessions/{sid}/context').json()['remaining_experiments']==1
        manifest=c.get(f'/v1/research/sessions/{sid}/manifest').json()
        assert c.post('/v1/research/sessions/import',json=manifest).json()==service.research.get_session(sid)
        replay=c.post(f'/v1/research/experiments/{eid}/replay',json={'session_id':sid,'request_id':'replay'}).json()
        assert replay['replay_of']==eid
        assert c.post('/v1/research/experiments/compare',json={'experiment_ids':[eid,replay['id']]}).status_code==200
        history=c.get(f"/v1/research/factors/{attempt['factor_id']}/history?limit=1").json()
        assert history['history_page']['limit']==1


@contextmanager
def running_server(app):
    import uvicorn
    sock=socket.socket()
    sock.bind(('127.0.0.1',0))
    port=sock.getsockname()[1]
    server=uvicorn.Server(uvicorn.Config(app,log_level='warning',lifespan='on'))
    thread=threading.Thread(target=server.run,kwargs={'sockets':[sock]},daemon=True)
    thread.start()
    try:
        deadline=time.monotonic()+10
        while not server.started and thread.is_alive() and time.monotonic()<deadline:
            time.sleep(.01)
        assert server.started
        yield f'http://127.0.0.1:{port}'
    finally:
        server.should_exit=True
        thread.join(10)
        sock.close()
        assert not thread.is_alive()


def test_remote_mcp_client_and_readonly_acceptance(production,monkeypatch):
    from assay.mcp.client import connect_mcp,validate_mcp_url
    from assay.agent import normalize_tool_result
    import importlib.util
    from pathlib import Path
    for url in ['http://remote.example/mcp/','https://user:key@remote.example/mcp/',
                'https://remote.example/mcp/?key=bad']:
        with pytest.raises(ValueError): validate_mcp_url(url)
    monkeypatch.setenv('ASSAY_DEPLOYMENT','development')
    monkeypatch.delenv('ASSAY_PUBLIC_URL')
    with running_server(create_app()) as base:
        async def scenario():
            async with connect_mcp(url=base+'/mcp/',api_key='test-deployment-key') as session:
                result=await session.call_tool('assay_lint',{'expr':'ts_mean(close,5)'})
                assert normalize_tool_result(result.model_dump(mode='json'))['ast'] is not None
                assert len((await session.list_tools()).tools)==40
            path=Path(__file__).resolve().parents[2]/'scripts/check_deployment.py'
            spec=importlib.util.spec_from_file_location('deployment_check',path)
            module=importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            report=await module.check(base,'test-deployment-key',10)
            assert report['status']=='passed',report
            assert not report['mutation_tools_called']
        asyncio.run(scenario())
