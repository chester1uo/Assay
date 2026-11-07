"""``/auth/*``: sign-in, invitation sign-up, own password and API keys, user admin.

Mirrors the QFinVerse hub's ``/auth`` surface on a smaller scale (two roles, no
groups or key scopes). Only active with ``ASSAY_ACCOUNTS=1``; ``GET /auth/status``
always answers so the WebUI can tell which mode the server is in.

House style: ``from __future__ import annotations``, type hints, concise docstrings.
"""

from __future__ import annotations

import hmac
import os
import threading
import time

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import JSONResponse

from assay.accounts import ROLES, AccountError, User, ensure_setup_token, setup_token_path, sign_session
from assay.api.auth import COOKIE, account_store, accounts_enabled, get_api_key, session_secret

router = APIRouter(prefix="/auth")

SESSION_DAYS = float(os.environ.get("ASSAY_SESSION_DAYS", "7"))


class _Throttle:
    """At most ``limit`` failures per key in ``window_s`` seconds."""

    def __init__(self, limit: int = 8, window_s: float = 600):
        self.limit, self.window_s = limit, window_s
        self._fails: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def blocked(self, key: str) -> bool:
        now = time.time()
        with self._lock:
            hits = [t for t in self._fails.get(key, []) if now - t < self.window_s]
            self._fails[key] = hits
            return len(hits) >= self.limit

    def fail(self, key: str) -> None:
        with self._lock:
            self._fails.setdefault(key, []).append(time.time())

    def ok(self, key: str) -> None:
        with self._lock:
            self._fails.pop(key, None)


_login_throttle = _Throttle()
_public_throttle = _Throttle()


def _client(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    return fwd.split(",")[0].strip() or (request.client.host if request.client else "?")


def _secure(request: Request) -> bool:
    return request.headers.get("x-forwarded-proto", request.url.scheme) == "https"


def _on() -> None:
    if not accounts_enabled():
        raise HTTPException(404, "accounts are not enabled on this server")


def _public(request: Request) -> str:
    """Rate-limit and same-origin check for the unauthenticated forms."""
    _on()
    client = _client(request)
    if _public_throttle.blocked(client) or _login_throttle.blocked(client):
        raise HTTPException(429, "too many failed attempts; try again in a few minutes")
    origin = request.headers.get("origin")
    if origin and origin.split("://", 1)[-1] != request.headers.get("host"):
        raise HTTPException(403, "cross-origin request refused")
    return client


def _signed_in(request: Request, body: dict, user: User) -> JSONResponse:
    ttl = int(SESSION_DAYS * 86400)
    resp = JSONResponse(body)
    resp.set_cookie(COOKIE, sign_session(session_secret(), user, ttl), max_age=ttl,
                    httponly=True, samesite="lax", secure=_secure(request), path="/")
    return resp


async def _me(request: Request) -> User:
    _on()
    await get_api_key(request, request.headers.get("x-api-key"), request.headers.get("authorization"))
    user = request.state.user
    if user is None or user.id == 0:
        raise HTTPException(401, "sign in first")
    return user


async def _admin(request: Request) -> User:
    user = await _me(request)
    if not user.is_admin:
        raise HTTPException(403, "requires an admin account")
    return user


def _bad(exc: AccountError) -> HTTPException:
    return HTTPException(404 if "not found" in str(exc) else 400, str(exc))


# ── public ────────────────────────────────────────────────────────────────

@router.get("/status")
def status():
    """Which mode the server runs in, and whether the first admin still has to be created."""
    if not accounts_enabled():
        return {"accounts": False, "setup_required": False, "registration": "closed"}
    return {"accounts": True, "setup_required": account_store().count_users() == 0,
            "registration": "invite_only"}


@router.get("/me")
async def whoami(request: Request):
    """The signed-in user, or ``{"user": null}``."""
    if not accounts_enabled():
        return {"user": None, "accounts": False}
    try:
        await get_api_key(request, request.headers.get("x-api-key"), request.headers.get("authorization"))
    except HTTPException:
        return {"user": None, "accounts": True}
    user = request.state.user
    return {"user": user.public() if user else None, "accounts": True,
            "via": getattr(request.state, "auth_via", "")}


@router.post("/setup")
def setup(request: Request, body: dict = Body(...)):
    """Create the first admin with the one-time token in ``<state>/setup_token``."""
    client = _public(request)
    store = account_store()
    if store.count_users() > 0:
        raise HTTPException(409, "already set up")
    if not hmac.compare_digest(str(body.get("token", "")).strip(), ensure_setup_token()):
        _public_throttle.fail(client)
        raise HTTPException(403, "invalid setup token")
    try:
        user = store.create_user(body.get("username", ""), body.get("password", ""), "admin",
                                 body.get("display_name", ""))
    except AccountError as exc:
        raise _bad(exc) from exc
    setup_token_path().unlink(missing_ok=True)
    store.audit(user.username, "setup", "first admin")
    return _signed_in(request, {"ok": True, "user": user.public()}, user)


@router.post("/login")
def login(request: Request, body: dict = Body(...)):
    _on()
    username = str(body.get("username", "")).strip().lower()
    client = _client(request)
    key = f"{client}|{username}"
    if _login_throttle.blocked(key) or _login_throttle.blocked(client):
        raise HTTPException(429, "too many failed attempts; try again in a few minutes")
    store = account_store()
    user = store.authenticate(username, str(body.get("password", "")))
    if user is None:
        _login_throttle.fail(key)
        store.audit(username, "login_failed", client)
        raise HTTPException(401, "wrong username or password")
    _login_throttle.ok(key)
    store.audit(user.username, "login", client)
    return _signed_in(request, {"ok": True, "user": user.public()}, user)


@router.post("/logout")
def logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE, path="/")
    return resp


@router.get("/invite")
def check_invite(request: Request, code: str = ""):
    """Whether an invitation can still be used (the sign-up form asks before showing)."""
    client = _public(request)
    invite = account_store().check_invite(code)
    if invite is None:
        _public_throttle.fail(client)
        raise HTTPException(404, "invitation is invalid, expired or used up")
    return {"role": invite["role"], "expires_at": invite["expires_at"]}


@router.post("/register")
def register(request: Request, body: dict = Body(...)):
    client = _public(request)
    store = account_store()
    try:
        user = store.register(str(body.get("invite_code", "")), body.get("username", ""),
                              body.get("password", ""), body.get("display_name", ""))
    except AccountError as exc:
        if "invitation" in str(exc):
            _public_throttle.fail(client)
        raise _bad(exc) from exc
    store.audit(user.username, "registered")
    return _signed_in(request, {"ok": True, "user": user.public()}, user)


@router.post("/reset-password")
def reset_password(request: Request, body: dict = Body(...)):
    client = _public(request)
    store = account_store()
    try:
        user = store.redeem_reset(str(body.get("token", "")), body.get("password", ""))
    except AccountError as exc:
        if "reset link" in str(exc):
            _public_throttle.fail(client)
        raise _bad(exc) from exc
    store.audit(user.username, "password_reset")
    return _signed_in(request, {"ok": True, "user": user.public()}, user)


# ── own account ───────────────────────────────────────────────────────────

@router.post("/password")
async def change_password(request: Request, body: dict = Body(...)):
    user = await _me(request)
    store = account_store()
    if store.authenticate(user.username, str(body.get("current_password", ""))) is None:
        raise HTTPException(403, "current password is wrong")
    try:
        store.set_password(user.id, body.get("new_password", ""))
    except AccountError as exc:
        raise _bad(exc) from exc
    store.audit(user.username, "password_changed")
    return _signed_in(request, {"ok": True}, store.get_user(user.id))     # version bumped


@router.patch("/profile")
async def update_profile(request: Request, body: dict = Body(...)):
    user = await _me(request)
    fresh = account_store().update_user(user.id, display_name=str(body.get("display_name", "")))
    return {"user": fresh.public()}


@router.get("/keys")
async def keys(request: Request):
    user = await _me(request)
    return {"keys": account_store().list_api_keys(user.id)}


@router.post("/keys")
async def create_key(request: Request, body: dict = Body(default={})):
    user = await _me(request)
    if getattr(request.state, "auth_via", "") == "key":
        raise HTTPException(403, "API keys are created from a signed-in browser")
    meta, token = account_store().create_api_key(user.id, str(body.get("name", "")))
    account_store().audit(user.username, "key_created", meta["prefix"])
    return {"key": meta, "token": token}


@router.delete("/keys/{key_id}")
async def revoke_key(request: Request, key_id: int):
    user = await _me(request)
    try:
        account_store().revoke_api_key(key_id, None if user.is_admin else user.id)
    except AccountError as exc:
        raise _bad(exc) from exc
    account_store().audit(user.username, "key_revoked", str(key_id))
    return {"ok": True}


# ── user administration (admins) ──────────────────────────────────────────

@router.get("/users")
async def users(request: Request):
    await _admin(request)
    store = account_store()
    keys_by_user: dict[int, int] = {}
    for k in store.list_api_keys():
        keys_by_user[k["user_id"]] = keys_by_user.get(k["user_id"], 0) + 1
    return {"users": [{**u.public(), "keys": keys_by_user.get(u.id, 0)} for u in store.list_users()],
            "roles": list(ROLES)}


@router.patch("/users/{user_id}")
async def update_user(request: Request, user_id: int, body: dict = Body(...)):
    actor = await _admin(request)
    try:
        user = account_store().update_user(
            user_id, role=body.get("role"), disabled=body.get("disabled"),
            display_name=body.get("display_name"))
    except AccountError as exc:
        raise _bad(exc) from exc
    account_store().audit(actor.username, "user_updated", f"{user.username}:{sorted(body)}")
    return {"user": user.public()}


@router.delete("/users/{user_id}")
async def delete_user(request: Request, user_id: int):
    actor = await _admin(request)
    if user_id == actor.id:
        raise HTTPException(400, "you cannot delete your own account")
    try:
        account_store().delete_user(user_id)
    except AccountError as exc:
        raise _bad(exc) from exc
    account_store().audit(actor.username, "user_deleted", str(user_id))
    return {"ok": True}


@router.post("/users/{user_id}/reset")
async def reset_link(request: Request, user_id: int, body: dict = Body(default={})):
    """A one-time password reset link for a user (the admin passes it on)."""
    actor = await _admin(request)
    try:
        reset = account_store().create_reset(user_id, int(body.get("minutes", 60)))
    except (AccountError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    account_store().audit(actor.username, "reset_issued", str(user_id))
    return {"token": reset["token"], "expires_at": reset["expires_at"],
            "path": f"/login?reset={reset['token']}"}


@router.get("/invites")
async def invites(request: Request):
    await _admin(request)
    return {"invites": account_store().list_invites()}


@router.post("/invites")
async def create_invite(request: Request, body: dict = Body(default={})):
    actor = await _admin(request)
    try:
        invite, token = account_store().create_invite(
            actor.username, role=body.get("role", "researcher"), max_uses=body.get("max_uses", 1),
            expires_in_days=body.get("expires_in_days", 7), label=str(body.get("label", "")))
    except AccountError as exc:
        raise _bad(exc) from exc
    account_store().audit(actor.username, "invite_created", f"{invite['prefix']} {invite['role']}")
    return {"invite": invite, "token": token, "path": f"/login?invite={token}"}


@router.delete("/invites/{invite_id}")
async def revoke_invite(request: Request, invite_id: int):
    actor = await _admin(request)
    try:
        account_store().revoke_invite(invite_id)
    except AccountError as exc:
        raise _bad(exc) from exc
    account_store().audit(actor.username, "invite_revoked", str(invite_id))
    return {"ok": True}
