import json
import sys
import threading
import time
from pathlib import Path
from fastapi.testclient import TestClient
from _pytest.monkeypatch import MonkeyPatch

def run(tmp_path, monkeypatch):
    monkeypatch.setenv('ASSAY_ACCOUNTS', '1')
    monkeypatch.setenv('ASSAY_STATE_DIR', str(tmp_path / 'state'))
    monkeypatch.setenv('ASSAY_DATA_DIR', str(tmp_path / 'data'))
    monkeypatch.setenv('ASSAY_CONFIG_FILE', str(tmp_path / 'config.json'))
    monkeypatch.setenv('ASSAY_MCP_HTTP', '0')
    seed = tmp_path / 'seed.json'
    seed.write_text(json.dumps({'demo': True, 'state': {'id': 'demo-session', 'experiments': [
        {'id': 'e1'}, {'id': 'e2'}, {'id': 'failed'}]}}))
    monkeypatch.setenv('ASSAY_DEMO_MANIFEST', str(seed))

    from assay.api import reviewer_demo as demo
    from assay.library import FactorLibrary
    from assay.research import ResearchWorkspace
    from assay.research_tenant import current_tenant
    demo.auth._reset_for_tests()
    monkeypatch.setattr(ResearchWorkspace, 'verify_manifest', staticmethod(lambda manifest: True))
    imports = []

    class Workspace:
        def import_session(self, manifest):
            imports.append((current_tenant.get(), manifest))
            return {'experiments': [
                {'id': 'e1', 'status': 'completed', 'report': {'factor_id': 'demo1', 'expr': 'close',
                 'expr_canonical': 'close', 'rank_icir': -0.08, 'rank_ic': -0.02,
                 'ic': -0.01, 'universe_id': 'NASDAQ100'}},
                {'id': 'e2', 'status': 'completed', 'report': {'factor_id': 'demo2', 'expr': 'open',
                 'expr_canonical': 'open', 'rank_icir': -0.03, 'rank_ic': -0.01,
                 'ic': -0.01, 'universe_id': 'NASDAQ100'}},
                {'id': 'failed', 'status': 'failed', 'report': {'factor_id': 'bad', 'failure_mode': 'SYNTAX_ERROR'}},
            ]}
        def get_session(self, session_id):
            assert session_id == 'demo-session'
            return self.import_session({'existing': True})

    class Service:
        def __init__(self):
            self._tenant_resources_lock = threading.RLock()
            self._tenant_resources = {}

        @property
        def research(self):
            return Workspace()
        def _private_resources(self):
            import hashlib
            name = hashlib.sha256(current_tenant.get().encode()).hexdigest()
            root = tmp_path / 'data' / 'research' / 'accounts' / name
            root.mkdir(parents=True, exist_ok=True)
            return {'root': root}
        @property
        def library(self):
            tenant = current_tenant.get()
            if tenant not in self._tenant_resources:
                import hashlib
                name = hashlib.sha256(tenant.encode()).hexdigest()
                self._tenant_resources[tenant] = FactorLibrary(tmp_path / 'data' / 'research' / 'accounts' / name / 'library')
            return self._tenant_resources[tenant]
        def library_query(self, **filters):
            return self.library.list(**filters)

    service = Service()
    monkeypatch.setattr(demo, 'get_service', lambda: service)
    from assay.api.routes import library as library_routes
    monkeypatch.setattr(library_routes, 'get_service', lambda: service)
    with TestClient(demo.app) as first, TestClient(demo.app) as second:
        first.cookies.set('assay_session', 'existing-main-session')
        response = first.get('/demo', follow_redirects=False)
        assert response.status_code == 303
        assert response.headers['location'] == '/#/research'
        assert demo._TTL == 15 * 86400
        session_cookie = next(value for value in response.headers.get_list('set-cookie') if value.startswith('assay_review_session='))
        max_age = int(session_cookie.split('Max-Age=', 1)[1].split(';', 1)[0])
        assert demo._TTL - 10 <= max_age <= demo._TTL
        assert first.cookies.get('assay_session') == 'existing-main-session'
        assert first.cookies.get('assay_review_session')
        assert first.cookies.get('assay_review_route') == '1'
        assert first.get('/auth/me').json()['user']['role'] == 'researcher'
        second.get('/demo', follow_redirects=False)
        assert first.get('/auth/me').json()['user']['id'] != second.get('/auth/me').json()['user']['id']
        assert len(imports) == 2 and imports[0][0] != imports[1][0]
        first_rows = first.get('/v1/library/factors?min_rank_icir=-1').json()['factors']
        second_rows = second.get('/v1/library/factors?min_rank_icir=-1').json()['factors']
        assert {row['factor_id'] for row in first_rows} == {'demo1', 'demo2'}
        assert {row['factor_id'] for row in second_rows} == {'demo1', 'demo2'}
        assert {row['factor_id']: row['rank_icir'] for row in first_rows} == {'demo1': -0.08, 'demo2': -0.03}
        assert first.request('DELETE', '/v1/library/factors', json={'factor_ids': ['demo1']}).status_code == 200
        assert len(first.get('/v1/library/factors?min_rank_icir=-1').json()['factors']) == 1
        assert len(second.get('/v1/library/factors?min_rank_icir=-1').json()['factors']) == 2
        first_root = tmp_path / 'data' / 'research' / 'accounts' / __import__('hashlib').sha256(imports[0][0].encode()).hexdigest()
        (first_root / '.reviewer-library-seeded').unlink()
        assert first.get('/demo', follow_redirects=False).status_code == 303
        assert len(first.get('/v1/library/factors?min_rank_icir=-1').json()['factors']) == 2
        assert first.request('DELETE', '/v1/library/factors', json={'factor_ids': ['demo1']}).status_code == 200
        assert first.get('/demo', follow_redirects=False).status_code == 303
        assert len(first.get('/v1/library/factors?min_rank_icir=-1').json()['factors']) == 1
        assert len(imports) == 3
        assert first.post('/auth/setup', json={}).status_code == 403
        assert first.get('/v1/admin/config').status_code == 403
        assert first.post('/auth/login', content=b'x' * 65537).status_code == 413
        html = first.get('/').text
        assert 'reviewer-demo.js' in html
        assert 'window.__ASSAY_REVIEW_DEMO__ = true' in html
        assert first.get('/demo/exit', follow_redirects=False).status_code == 303
        assert first.cookies.get('assay_review_route') is None
        assert first.cookies.get('assay_review_session') is None
        assert first.cookies.get('assay_session') == 'existing-main-session'

    # A failed seed must not leave an account, private directory or service cache.
    account_root = tmp_path / 'data' / 'research' / 'accounts'
    before_dirs = {p.name for p in account_root.iterdir()}
    store = demo.auth.account_store()
    before_users = store.count_users()
    before_tenants = set(service._tenant_resources)
    original_save = FactorLibrary.save
    def fail_save(self, report):
        raise OSError('injected seed failure')
    monkeypatch.setattr(FactorLibrary, 'save', fail_save)
    with TestClient(demo.app, raise_server_exceptions=False) as failed:
        assert failed.get('/demo', follow_redirects=False).status_code == 500
    assert store.count_users() == before_users
    assert {p.name for p in account_root.iterdir()} == before_dirs
    assert set(service._tenant_resources) == before_tenants
    monkeypatch.setattr(FactorLibrary, 'save', original_save)


patch = MonkeyPatch()
run(Path(sys.argv[1]), patch)

# Reclaim old guests and their private workspace; current guests remain.
from assay.api import reviewer_demo as demo
store = demo.auth.account_store()
old = store.create_user('guest-expired', 'long-random-password')
import hashlib
root = Path(__import__('os').environ['ASSAY_DATA_DIR']) / 'research' / 'accounts'
folder = root / hashlib.sha256(('account:' + old.workspace_id).encode()).hexdigest()
folder.mkdir(parents=True)
with store._conn() as db:
    db.execute('UPDATE users SET created_at=? WHERE id=?', (time.time()-demo._TTL-demo._CLEANUP_GRACE-10, old.id))
demo._reap_expired(store)
assert store.get_user(old.id) is None and not folder.exists()

