"""Accounts: first-admin setup, invitation sign-up, sessions, personal keys, user admin.

Offline: a temporary state directory, no data store. ``/v1/factor/lint`` stands in for
any protected API route (it needs no service), ``/v1/admin/config`` for admin routes.

Run with::

    PYTHONPATH=src python -m pytest tests/api/test_accounts.py -q
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from assay.api import auth
from assay.api.app import app
from assay.api.routes import account

LINT = ("/v1/factor/lint", {"expr": "rank(close)"})
PASSWORD = "correct horse battery"


@pytest.fixture
def accounts(tmp_path, monkeypatch):
    """Accounts on, with a fresh state directory and no service keys."""
    monkeypatch.setenv("ASSAY_ACCOUNTS", "1")
    monkeypatch.setenv("ASSAY_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("ASSAY_AUTH_DB", raising=False)
    monkeypatch.delenv("ASSAY_SECRET_KEY", raising=False)
    monkeypatch.delenv("ASSAY_API_KEYS", raising=False)
    monkeypatch.delenv("ASSAY_ADMIN_KEYS", raising=False)
    auth._reset_for_tests()
    account._public_throttle._fails.clear()
    account._login_throttle._fails.clear()
    yield tmp_path
    auth._reset_for_tests()


def _client() -> TestClient:
    return TestClient(app, base_url="https://testserver")


def _admin(state) -> TestClient:
    c = _client()
    assert c.get("/auth/status").json()["setup_required"] is True
    from assay.accounts import ensure_setup_token
    token = ensure_setup_token()
    assert c.post("/auth/setup", json={"token": "wrong", "username": "a", "password": PASSWORD}).status_code == 403
    r = c.post("/auth/setup", json={"token": token, "username": "Chief", "password": PASSWORD})
    assert r.status_code == 200 and r.json()["user"]["role"] == "admin"
    assert not (state / "setup_token").exists()
    return c


def _invite(admin: TestClient, role: str = "researcher", uses: int = 1) -> str:
    r = admin.post("/auth/invites", json={"role": role, "max_uses": uses, "label": "test"})
    assert r.status_code == 200
    return r.json()["path"].split("invite=", 1)[1]


def test_signed_out_callers_are_refused(accounts) -> None:
    c = _client()
    assert c.post(LINT[0], json=LINT[1]).status_code == 401
    assert c.get("/auth/me").json() == {"user": None, "accounts": True}
    assert c.get("/health").status_code == 200          # liveness stays public
    assert c.get("/login").status_code == 200


def test_first_setup_token_is_available_before_form_submission(accounts) -> None:
    import stat
    token_path=accounts/'setup_token'
    c=_client()
    response=c.get('/auth/status')
    assert response.json()['setup_required']
    token=token_path.read_text()
    assert token and token not in response.text
    assert stat.S_IMODE(token_path.stat().st_mode)==0o600
    assert c.get('/auth/status').status_code==200 and token_path.read_text()==token
    assert c.post('/auth/setup',json={'token':token,'username':'first','password':PASSWORD}).status_code==200
    assert not token_path.exists()
    assert not c.get('/auth/status').json()['setup_required']
    assert not token_path.exists()


def test_setup_then_session_reaches_the_api(accounts) -> None:
    admin = _admin(accounts)
    assert admin.post(LINT[0], json=LINT[1]).status_code == 200
    assert admin.get("/auth/me").json()["user"]["username"] == "chief"
    assert _client().post("/auth/setup", json={"token": "x", "username": "b", "password": PASSWORD}).status_code == 409


def test_invitation_signup_roles_and_limits(accounts) -> None:
    admin = _admin(accounts)
    code = _invite(admin)
    guest = _client()
    assert guest.get(f"/auth/invite?code={code}").json()["role"] == "researcher"
    assert guest.post("/auth/register", json={"invite_code": code, "username": "ann", "password": "short"}).status_code == 400
    r = guest.post("/auth/register", json={"invite_code": code, "username": "ann", "password": PASSWORD})
    assert r.status_code == 200
    assert guest.post(LINT[0], json=LINT[1]).status_code == 200
    # researchers cannot reach admin surfaces
    assert guest.get("/auth/users").status_code == 403
    assert guest.get("/v1/admin/config").status_code == 403
    # the invitation was for one person
    other = _client()
    assert other.post("/auth/register", json={"invite_code": code, "username": "bob", "password": PASSWORD}).status_code == 400
    assert other.post("/auth/register", json={"invite_code": "asi_made_up", "username": "bob", "password": PASSWORD}).status_code == 400


def test_login_logout_and_wrong_password(accounts) -> None:
    _admin(accounts)
    c = _client()
    assert c.post("/auth/login", json={"username": "chief", "password": "nope-nope-nope"}).status_code == 401
    assert c.post("/auth/login", json={"username": "CHIEF", "password": PASSWORD}).status_code == 200
    assert c.post(LINT[0], json=LINT[1]).status_code == 200
    c.post("/auth/logout")
    assert c.post(LINT[0], json=LINT[1]).status_code == 401


def test_personal_api_keys(accounts) -> None:
    admin = _admin(accounts)
    res = admin.post("/auth/keys", json={"name": "laptop"}).json()
    token, kid = res["token"], res["key"]["id"]
    assert token.startswith("asy_")
    bare = _client()
    assert bare.post(LINT[0], json=LINT[1], headers={"X-API-Key": token}).status_code == 200
    assert bare.post(LINT[0], json=LINT[1], headers={"Authorization": f"Bearer {token}"}).status_code == 200
    # a key cannot mint more keys
    assert bare.post("/auth/keys", json={}, headers={"X-API-Key": token}).status_code == 403
    assert [k["name"] for k in admin.get("/auth/keys").json()["keys"]] == ["laptop"]
    assert admin.delete(f"/auth/keys/{kid}").status_code == 200
    assert bare.post(LINT[0], json=LINT[1], headers={"X-API-Key": token}).status_code == 401


def test_disable_password_change_and_reset_end_sessions(accounts) -> None:
    admin = _admin(accounts)
    ann = _client()
    ann.post("/auth/register", json={"invite_code": _invite(admin), "username": "ann", "password": PASSWORD})
    key = ann.post("/auth/keys", json={}).json()["token"]
    uid = ann.get("/auth/me").json()["user"]["id"]

    # disabling signs her out and revokes her keys
    assert admin.patch(f"/auth/users/{uid}", json={"disabled": True}).status_code == 200
    assert ann.post(LINT[0], json=LINT[1]).status_code == 401
    assert _client().post(LINT[0], json=LINT[1], headers={"X-API-Key": key}).status_code == 401
    admin.patch(f"/auth/users/{uid}", json={"disabled": False})

    # a reset link works once and ends other sessions
    ann.post("/auth/login", json={"username": "ann", "password": PASSWORD})
    link = admin.post(f"/auth/users/{uid}/reset", json={}).json()["path"]
    token = link.split("reset=", 1)[1]
    other = _client()
    assert other.post("/auth/reset-password", json={"token": token, "password": "a new long password"}).status_code == 200
    assert ann.post(LINT[0], json=LINT[1]).status_code == 401
    assert other.post("/auth/reset-password", json={"token": token, "password": "another password!"}).status_code == 400
    assert other.post(LINT[0], json=LINT[1]).status_code == 200

    # changing the password keeps the browser that changed it signed in
    assert other.post("/auth/password", json={"current_password": "a new long password",
                                              "new_password": "third long password"}).status_code == 200
    assert other.post(LINT[0], json=LINT[1]).status_code == 200


def test_last_admin_is_protected_and_roles_change(accounts) -> None:
    admin = _admin(accounts)
    me = admin.get("/auth/me").json()["user"]["id"]
    assert admin.patch(f"/auth/users/{me}", json={"role": "researcher"}).status_code == 400
    assert admin.delete(f"/auth/users/{me}").status_code == 400
    bob = _client()
    bob.post("/auth/register", json={"invite_code": _invite(admin), "username": "bob", "password": PASSWORD})
    uid = bob.get("/auth/me").json()["user"]["id"]
    admin.patch(f"/auth/users/{uid}", json={"role": "admin"})
    assert bob.get("/auth/users").status_code == 401        # role change signs out
    bob.post("/auth/login", json={"username": "bob", "password": PASSWORD})
    assert bob.get("/auth/users").status_code == 200


def test_cookie_requests_from_other_sites_are_refused(accounts) -> None:
    admin = _admin(accounts)
    r = admin.post(LINT[0], json=LINT[1], headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    assert admin.post(LINT[0], json=LINT[1], headers={"Origin": "https://testserver"}).status_code == 200


def test_service_keys_still_work_with_accounts(accounts, monkeypatch) -> None:
    monkeypatch.setenv("ASSAY_API_KEYS", "svc-research,svc-admin")
    monkeypatch.setenv("ASSAY_ADMIN_KEYS", "svc-admin")
    c = _client()
    assert c.post(LINT[0], json=LINT[1], headers={"X-API-Key": "svc-research"}).status_code == 200
    assert c.get("/v1/admin/config", headers={"X-API-Key": "svc-research"}).status_code == 403
    assert c.get("/auth/keys", headers={"X-API-Key": "svc-research"}).status_code == 401   # not a person


def test_without_accounts_nothing_changes(monkeypatch) -> None:
    monkeypatch.delenv("ASSAY_ACCOUNTS", raising=False)
    monkeypatch.delenv("ASSAY_API_KEYS", raising=False)
    monkeypatch.delenv("ASSAY_ADMIN_KEYS", raising=False)
    c = _client()
    assert c.post(LINT[0], json=LINT[1]).status_code == 200
    assert c.get("/auth/status").json()["accounts"] is False
    assert c.post("/auth/login", json={"username": "x", "password": PASSWORD}).status_code == 404
    monkeypatch.setenv("ASSAY_API_KEYS", "k1")
    assert c.post(LINT[0], json=LINT[1]).status_code == 401
    assert c.post(LINT[0], json=LINT[1], headers={"X-API-Key": "k1"}).status_code == 200
