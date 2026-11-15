import json
import sys
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
    seed.write_text(json.dumps({'demo': True}))
    monkeypatch.setenv('ASSAY_DEMO_MANIFEST', str(seed))

    from assay.api import reviewer_demo as demo
    from assay.research_tenant import current_tenant
    demo.auth._reset_for_tests()
    imports = []

    class Workspace:
        def import_session(self, manifest):
            imports.append((current_tenant.get(), manifest))

    class Service:
        @property
        def research(self):
            return Workspace()

    monkeypatch.setattr(demo, 'get_service', lambda: Service())
    with TestClient(demo.app) as first, TestClient(demo.app) as second:
        first.cookies.set('assay_session', 'existing-main-session')
        response = first.get('/demo', follow_redirects=False)
        assert response.status_code == 303
        assert response.headers['location'] == '/#/research'
        assert first.cookies.get('assay_session') == 'existing-main-session'
        assert first.cookies.get('assay_review_session')
        assert first.cookies.get('assay_review_route') == '1'
        assert first.get('/auth/me').json()['user']['role'] == 'researcher'
        second.get('/demo', follow_redirects=False)
        assert first.get('/auth/me').json()['user']['id'] != second.get('/auth/me').json()['user']['id']
        assert len(imports) == 2 and imports[0][0] != imports[1][0]
        assert first.get('/demo', follow_redirects=False).status_code == 303
        assert len(imports) == 2
        assert first.post('/auth/setup', json={}).status_code == 403
        assert first.get('/v1/admin/config').status_code == 403
        assert first.post('/auth/login', content=b'x' * 65537).status_code == 413
        assert 'reviewer-demo.js' in first.get('/').text
        assert first.get('/demo/exit', follow_redirects=False).status_code == 303
        assert first.cookies.get('assay_review_route') is None
        assert first.cookies.get('assay_review_session') is None
        assert first.cookies.get('assay_session') == 'existing-main-session'


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

