"""Account research state must follow authenticated identity across REST and MCP."""
import json

from fastapi.testclient import TestClient

from assay.api import auth
from assay.api.app import create_app
from assay.demo import prepare_demo
from assay.service import AssayService


def _mcp(client, key, name, arguments):
    response = client.post('/mcp/', headers={'Authorization': 'Bearer ' + key,
        'Accept': 'application/json, text/event-stream'}, json={'jsonrpc':'2.0', 'id':1,
        'method':'tools/call', 'params':{'name':name, 'arguments':arguments}})
    assert response.status_code == 200, response.text
    if response.headers.get('content-type', '').startswith('application/json'):
        payload = response.json()
    else:
        payload = json.loads(next(line[6:] for line in response.text.splitlines() if line.startswith('data: ')))
    result = payload['result']
    if 'structuredContent' in result:
        return result['structuredContent']
    return json.loads(result['content'][0]['text'])


def test_account_workspaces_are_isolated_across_rest_and_mcp(tmp_path, monkeypatch):
    monkeypatch.setenv('ASSAY_ACCOUNTS', '1')
    monkeypatch.setenv('ASSAY_STATE_DIR', str(tmp_path / 'state'))
    monkeypatch.setenv('ASSAY_API_KEYS', 'service-secret')
    monkeypatch.delenv('ASSAY_ADMIN_KEYS', raising=False)
    monkeypatch.delenv('ASSAY_AUTH_DB', raising=False)
    monkeypatch.delenv('ASSAY_SECRET_KEY', raising=False)
    monkeypatch.setattr(AssayService, '_instance', None)
    auth._reset_for_tests()
    try:
        service, legacy = prepare_demo(tmp_path / 'data')
        store = auth.account_store()
        alice = store.create_user('alice', 'correct horse battery')
        bob = store.create_user('bob', 'correct horse battery')
        _, alice_key = store.create_api_key(alice.id, 'agent')
        _, bob_key = store.create_api_key(bob.id, 'agent')
        a = {'Authorization':'Bearer '+alice_key}; b = {'Authorization':'Bearer '+bob_key}
        with TestClient(create_app()) as client:
            discovery = client.get('/.well-known/assay.json').json()['research']
            assert discovery['scope'] == 'account-workspace' and discovery['tenant_isolation']
            assert discovery['library_scope'] == 'account'
            assert discovery['private_compute_registry'] and discovery['private_compute_requires_ades']
            assert not discovery['private_compute_configured']
            body = {'name':'private', 'universe':legacy['universe'], 'period':legacy['period'],
                    'as_of':legacy['as_of'], 'horizons':legacy['horizons'], 'execution':legacy['execution'],
                    'adj':legacy['adj']}
            dataset = client.post('/v1/research/datasets',headers=a,json=body)
            assert dataset.status_code == 200, dataset.text
            ds = dataset.json()['id']
            created = client.post('/v1/research/sessions',headers=a,json={'dataset_id':ds,'goal':'private goal'})
            assert created.status_code == 200, created.text
            sid = created.json()['id']
            result = client.post('/v1/research/sessions/'+sid+'/experiments',headers=a,
                json={'expr':'ts_returns(close,3)','hypothesis':'private hypothesis','request_id':'one'})
            assert result.status_code == 200, result.text
            assert result.json()['status'] == 'completed'
            assert client.get('/v1/research/sessions',headers=b).json() == []
            assert client.get('/v1/research/sessions/'+sid,headers=b).status_code == 400
            assert client.get('/v1/research/experiments/'+result.json()['id'],headers=b).status_code == 400
            assert _mcp(client,alice_key,'assay_research_session_list',{})['sessions'][0]['id']==sid
            assert _mcp(client,bob_key,'assay_research_session_list',{})['sessions']==[]
            assert _mcp(client,bob_key,'assay_dataset_list',{})['datasets']==[]
            assert _mcp(client,alice_key,'fdb_query',{'source':'research'})['count'] > 0
            assert _mcp(client,bob_key,'fdb_query',{'source':'research'})['count'] == 0
            assert _mcp(client,bob_key,'fdb_factor',{'factor_id':result.json()['factor_id'],
                                                     'source':'research'})['error']['code']=='NOT_FOUND'
            from assay.library import FactorReport
            report = FactorReport(factor_id='alice_factor', expr='close', expr_canonical='close',
                                  ic=.1, icir=.2, rank_ic=.1, rank_icir=.2).to_dict()
            saved = client.post('/v1/library/factors', headers=a, json=report)
            assert saved.status_code == 200 and saved.json()['factor_id'] == 'alice_factor'
            assert client.get('/v1/library/factors/alice_factor', headers=b).status_code == 404
            assert _mcp(client,alice_key,'assay_library_get',{'factor_id':'alice_factor'})['found']
            assert not _mcp(client,bob_key,'assay_library_get',{'factor_id':'alice_factor'})['found']
            assert client.request('DELETE','/v1/library/factors',headers=b,
                                  json={'factor_ids':['alice_factor']}).json()['deleted'] == 0
            assert client.get('/v1/library/factors/alice_factor', headers=a).status_code == 200
            bob_report = {**report, 'expr':'volume', 'expr_canonical':'volume'}
            assert client.post('/v1/library/factors',headers=b,json=bob_report).status_code == 200
            assert client.get('/v1/library/factors/alice_factor',headers=b).json()['expr'] == 'volume'
            assert client.get('/v1/library/factors/alice_factor',headers=a).json()['expr'] == 'close'
            assert client.post('/v1/library/factors', headers=b,
                               json={**report,'factor_id':'../escape'}).status_code == 422
            combo = client.post('/v1/combination/saved',headers=a,
                                json={'name':'alice only','result':{'method':'equal','weights':{'close':1}}})
            assert combo.status_code == 200, combo.text
            combo_id = combo.json()['id']
            assert client.get('/v1/combination/saved/'+combo_id,headers=b).status_code == 404
            assert client.request('DELETE','/v1/combination/saved',headers=b,
                                  json={'ids':[combo_id]}).json()['deleted'] == 0
            assert client.get('/v1/combination/saved/'+combo_id,headers=a).status_code == 200
            panel = client.post('/v1/session/create',headers=a,json={
                'universe':legacy['universe'], 'period':legacy['period'], 'as_of':legacy['as_of'], 'adj':legacy['adj']})
            assert panel.status_code == 200, panel.text
            panel_id = panel.json()['session_id']
            assert not client.delete('/v1/session/'+panel_id,headers=b).json()['expired']
            batch = client.post('/v1/factor/batch',headers=a,json={
                'exprs':['ts_returns(close,3)','ts_returns(close,5)'],
                'session_id':panel_id,'n_jobs':2,'save':True})
            assert batch.status_code == 200, batch.text
            assert all(r['failure_mode'] is None for r in batch.json()['reports'])
            for item in batch.json()['reports']:
                assert client.get('/v1/library/factors/'+item['factor_id'],headers=a).status_code == 200
                assert client.get('/v1/library/factors/'+item['factor_id'],headers=b).status_code == 404
            assert client.delete('/v1/session/'+panel_id,headers=a).json()['expired']
            assert client.get('/v1/fdb/jobs/guessed', headers=b).status_code == 503
            from assay.factordb import service as factordb_service
            class SharedCompute:
                def job(self, job_id, top):
                    return {'secret': 'shared-compute-sentinel'}
            monkeypatch.setattr(factordb_service, 'get', lambda: SharedCompute())
            assert _mcp(client,'service-secret','fdb_job',{'job_id':'guessed'})['secret'] == 'shared-compute-sentinel'
            denied = _mcp(client,bob_key,'fdb_job',{'job_id':'guessed'})
            assert 'error' in denied and 'shared-compute-sentinel' not in str(denied)
            spoofed = dict(b, **{'X-Assay-Tenant':'account:'+str(alice.id)})
            assert client.get('/v1/research/sessions',headers=spoofed).json() == []
            from concurrent.futures import ThreadPoolExecutor
            def check(key):
                return [r['id'] for r in _mcp(client,key,'assay_research_session_list',{})['sessions']]
            with ThreadPoolExecutor(max_workers=4) as pool:
                observed = list(pool.map(check, [alice_key,bob_key]*5))
            assert observed == [[sid],[]]*5
            assert client.post('/auth/login',json={'username':'alice','password':'correct horse battery'}).status_code == 200
            assert client.get('/v1/research/sessions').json()[0]['id']==sid
            assert not any(x['id']==legacy['id'] for x in client.get('/v1/research/datasets',headers=a).json())
            assert any(x['id']==legacy['id'] for x in client.get('/v1/research/datasets',headers={'X-API-Key':'service-secret'}).json())
            assert client.get('/v1/research/backends',headers=a).json()[0]['name']=='assay'
            service._research_tenants.clear()
            service._tenant_resources.clear()
            assert client.get('/v1/research/sessions',headers=a).json()[0]['id']==sid
            assert client.get('/v1/library/factors/alice_factor',headers=a).json()['expr']=='close'
            assert client.get('/v1/library/factors/alice_factor',headers=b).json()['expr']=='volume'
            assert client.get('/v1/combination/saved/'+combo_id,headers=a).status_code == 200
            assert client.get('/v1/combination/saved/'+combo_id,headers=b).status_code == 404
            from stat import S_IMODE
            account_root = service.config.data_dir / 'research' / 'accounts'
            assert S_IMODE(account_root.stat().st_mode) == 0o700
            assert all(S_IMODE(p.stat().st_mode) == 0o700 for p in account_root.iterdir() if p.is_dir())
    finally:
        auth._reset_for_tests()


def test_rebuilt_account_database_cannot_reuse_workspace_or_cookie(tmp_path, monkeypatch):
    monkeypatch.setenv('ASSAY_ACCOUNTS', '1')
    monkeypatch.setenv('ASSAY_SECRET_KEY', 'same-secret-across-restores')
    monkeypatch.setenv('ASSAY_AUTH_DB', str(tmp_path/'old-auth.sqlite3'))
    monkeypatch.delenv('ASSAY_API_KEYS', raising=False)
    monkeypatch.delenv('ASSAY_ADMIN_KEYS', raising=False)
    monkeypatch.setattr(AssayService, '_instance', None)
    auth._reset_for_tests()
    try:
        _, legacy = prepare_demo(tmp_path/'data')
        old_user = auth.account_store().create_user('alice', 'correct horse battery')
        with TestClient(create_app()) as client:
            assert client.post('/auth/login',json={'username':'alice','password':'correct horse battery'}).status_code == 200
            dataset = client.post('/v1/research/datasets',json={
                'name':'old private', 'universe':legacy['universe'], 'period':legacy['period'],
                'as_of':legacy['as_of'], 'horizons':legacy['horizons'],
                'execution':legacy['execution'], 'adj':legacy['adj']})
            assert dataset.status_code == 200
            old_cookie = client.cookies.get('assay_session')
            monkeypatch.setenv('ASSAY_AUTH_DB', str(tmp_path/'new-auth.sqlite3'))
            auth._reset_for_tests()
            new_user = auth.account_store().create_user('bob', 'correct horse battery')
            assert old_user.id == new_user.id and old_user.workspace_id != new_user.workspace_id
            assert client.get('/v1/research/datasets').status_code == 401
            assert client.post('/auth/login',json={'username':'bob','password':'correct horse battery'}).status_code == 200
            assert client.get('/v1/research/datasets').json() == []
            client.cookies.set('assay_session',old_cookie,domain='testserver.local',path='/')
            assert client.get('/v1/research/datasets').status_code == 401
    finally:
        auth._reset_for_tests()


def test_existing_account_database_migrates_workspace_identity_once(tmp_path):
    import sqlite3
    from assay.accounts import AccountStore
    db = tmp_path/'old.sqlite3'
    with sqlite3.connect(db) as connection:
        connection.execute('CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, '
            'username TEXT UNIQUE NOT NULL, display_name TEXT NOT NULL DEFAULT "", '
            'password_hash TEXT NOT NULL, role TEXT NOT NULL, disabled INTEGER NOT NULL DEFAULT 0, '
            'session_version INTEGER NOT NULL DEFAULT 1, created_at REAL NOT NULL, last_login_at REAL)')
        connection.execute('INSERT INTO users (username,password_hash,role,created_at) VALUES (?,?,?,?)',
                           ('alice','legacy-hash','researcher',1.0))
    first = AccountStore(db).get_user(1)
    second = AccountStore(db).get_user(1)
    assert first.username == 'alice'
    assert len(first.workspace_id) == 32 and first.workspace_id == second.workspace_id
