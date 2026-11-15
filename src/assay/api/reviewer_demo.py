"""Isolated, anonymous reviewer workbench.

Run this module in a dedicated process with its own ASSAY_STATE_DIR,
ASSAY_DATA_DIR and ASSAY_CONFIG_FILE. The seed is an exported research-session
manifest in ASSAY_DEMO_MANIFEST. The reverse proxy must route /demo and requests
bearing the routing cookie to this process; ordinary traffic stays on the main
service. This module must not be mounted in the production account process.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import os
import secrets
import threading
import time
from collections import defaultdict, deque
from pathlib import Path

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

# A separate cookie preserves any sign-in to the main application on this origin.
from assay.api import auth

auth.COOKIE = 'assay_review_session'
from assay.accounts import sign_session  # noqa: E402
from assay.api.app import app, get_service  # noqa: E402
from assay.api.routes import account  # noqa: E402
from assay.research_tenant import current_tenant, tenant_for_user  # noqa: E402
from assay.service import AssayService  # noqa: E402

account.COOKIE = auth.COOKIE

# Enforce the worker cap even for MCP tools that supply an internal default.
_original_batch = AssayService.batch
def _review_batch(self, *args, **kwargs):
    requested = kwargs.get('n_jobs')
    kwargs['n_jobs'] = min(requested, 2) if type(requested) is int and requested > 0 else 2
    return _original_batch(self, *args, **kwargs)
AssayService.batch = _review_batch

_ROUTE_COOKIE = 'assay_review_route'
_TTL = 7 * 86400
_CLEANUP_GRACE = 86400
_MAX_BODY = 65536
_lock = threading.Lock()
_visits: dict[str, deque] = defaultdict(deque)
_writes: dict[str, deque] = defaultdict(deque)
_reads: dict[str, deque] = defaultdict(deque)
_slots = asyncio.Semaphore(2)
_waiting = 0


def _allow(buckets, key, maximum, window):
    now = time.monotonic()
    with _lock:
        q = buckets[key]
        while q and now - q[0] >= window:
            q.popleft()
        if len(q) >= maximum:
            return False
        q.append(now)
        # Drop inactive keys so a public service cannot grow this table forever.
        if len(buckets) > 4096:
            for old in list(buckets):
                if not buckets[old] or now - buckets[old][-1] >= window:
                    del buckets[old]
            while len(buckets) > 4096:
                buckets.pop(next(iter(buckets)))
        return True


def _client(request):
    # Uvicorn is configured to trust forwarded addresses only from the local proxy.
    return request.client.host if request.client else 'unknown'


def _secure(request):
    return request.url.scheme == 'https'


def _reap_expired(store):
    """Remove expired demo-only users and their isolated workspaces after a grace period."""
    cutoff = time.time() - _TTL - _CLEANUP_GRACE
    with store._lock, store._conn() as db:
        rows = db.execute("SELECT id, workspace_id FROM users WHERE username LIKE 'guest-%' AND created_at < ?", (cutoff,)).fetchall()
        for row in rows:
            db.execute('DELETE FROM users WHERE id=?', (row['id'],))
    root = Path(os.environ['ASSAY_DATA_DIR']) / 'research' / 'accounts'
    for row in rows:
        digest = hashlib.sha256(('account:' + row['workspace_id']).encode()).hexdigest()
        shutil.rmtree(root / digest, ignore_errors=True)


@app.get('/', include_in_schema=False)
def demo_index():
    html = (Path(__file__).parent / 'static' / 'index.html').read_text()
    html = html.replace('</body>', '<script type="module" src="/js/reviewer-demo.js"></script></body>')
    return HTMLResponse(html, headers={'Cache-Control': 'no-store'})


@app.get('/demo', include_in_schema=False)
def enter_demo(request: Request):
    if not auth.accounts_enabled():
        raise HTTPException(503, 'Reviewer demo requires isolated account storage')
    user = auth.resolve_user(request)
    if user is None:
        if not _allow(_visits, _client(request), 20, 3600):
            raise HTTPException(429, 'Please try again later')
        seed_path = os.environ.get('ASSAY_DEMO_MANIFEST', '')
        if not seed_path:
            raise HTTPException(503, 'Reviewer example is not configured')
        try:
            manifest = json.loads(Path(seed_path).read_text())
        except (OSError, ValueError) as exc:
            raise HTTPException(503, 'Reviewer example is unavailable') from exc
        store = auth.account_store()
        # This process uses one worker. The lock keeps the user cap and creation
        # atomic within it and prevents concurrent provisioning races.
        with _lock:
            _reap_expired(store)
            if store.count_users() >= int(os.environ.get('ASSAY_DEMO_MAX_USERS', '1000')):
                raise HTTPException(503, 'Reviewer demo is at capacity')
            user = store.create_user('guest-' + secrets.token_hex(12), secrets.token_urlsafe(32),
                                     display_name='Guest researcher')
        token = current_tenant.set(tenant_for_user(user))
        try:
            get_service().research.import_session(manifest)
        except Exception:
            # Do not leave an unusable account consuming the guest capacity.
            with store._lock, store._conn() as db:
                db.execute('DELETE FROM users WHERE id=?', (user.id,))
            raise
        finally:
            current_tenant.reset(token)
    remaining = max(1, min(_TTL, int(user.created_at + _TTL - time.time())))
    response = RedirectResponse('/#/research', status_code=303,
                                headers={'Cache-Control': 'no-store'})
    response.set_cookie(auth.COOKIE, sign_session(auth.session_secret(), user, remaining),
                        max_age=remaining, httponly=True, secure=_secure(request), samesite='lax', path='/')
    response.set_cookie(_ROUTE_COOKIE, '1', max_age=remaining, httponly=True,
                        secure=_secure(request), samesite='lax', path='/')
    return response


@app.get('/demo/exit', include_in_schema=False)
def exit_demo():
    response = RedirectResponse('/', status_code=303, headers={'Cache-Control': 'no-store'})
    response.delete_cookie(auth.COOKIE, path='/')
    response.delete_cookie(_ROUTE_COOKIE, path='/')
    return response


# The SPA catch-all is registered before the two routes above; move it to the end.
_static = next(route for route in app.router.routes if getattr(route, 'name', None) == 'webui')
app.router.routes.remove(_static)
app.router.routes.append(_static)


class DemoLimits:
    """Limit anonymous writes until the entire ASGI response (including streams) ends."""

    def __init__(self, wrapped):
        self.wrapped = wrapped

    async def __call__(self, scope, receive, send):
        global _waiting
        if scope['type'] != 'http':
            return await self.wrapped(scope, receive, send)
        path, method = scope.get('path', ''), scope.get('method', '')
        if path.startswith(('/v1/admin', '/admin', '/auth/setup', '/auth/register',
                            '/auth/invite', '/auth/reset-password')):
            return await JSONResponse({'error': 'Not available in the reviewer demo'}, status_code=403)(scope, receive, send)
        write = method not in ('GET', 'HEAD', 'OPTIONS')
        # Every API GET counts too: some read endpoints perform computations.
        heavy = write or path.startswith('/v1/')
        host = (scope.get('client') or ('unknown',))[0]
        if not write and heavy and not _allow(_reads, host, 600, 60):
            return await JSONResponse({'error': 'Demo request limit reached; try again shortly'}, status_code=429)(scope, receive, send)
        if write:
            if not _allow(_writes, host, 120, 60):
                return await JSONResponse({'error': 'Demo request limit reached; try again shortly'}, status_code=429)(scope, receive, send)
            chunks, size = [], 0
            while True:
                message = await receive()
                if message['type'] == 'http.disconnect':
                    return
                chunk = message.get('body', b'')
                size += len(chunk)
                if size > _MAX_BODY:
                    return await JSONResponse({'error': 'Demo request is too large'}, status_code=413)(scope, receive, send)
                chunks.append(chunk)
                if not message.get('more_body', False):
                    break
            raw_body = b''.join(chunks)
            try:
                parsed = json.loads(raw_body)
            except (ValueError, UnicodeDecodeError, RecursionError):
                parsed = None
            pending = [parsed]
            while pending:
                item = pending.pop()
                if isinstance(item, dict):
                    for key, value in item.items():
                        if key in ('exprs', 'expressions', 'factors') and isinstance(value, list) and len(value) > 20:
                            return await JSONResponse({'error': 'Demo batches are limited to 20 factors'}, status_code=413)(scope, receive, send)
                        if key in ('n_jobs', 'n_workers') and value is not None and (
                            type(value) is not int or not 1 <= value <= 2
                        ):
                            return await JSONResponse({'error': 'Demo uses at most two workers'}, status_code=400)(scope, receive, send)
                        pending.append(value)
                elif isinstance(item, list):
                    pending.extend(item)
            original_receive = receive
            delivered = False
            async def replay():
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {'type': 'http.request', 'body': raw_body, 'more_body': False}
                return await original_receive()
            receive = replay
        if heavy:
            with _lock:
                available = _waiting < 32
                if available:
                    _waiting += 1
            if not available:
                return await JSONResponse({'error': 'Demo is busy; please retry shortly'}, status_code=429)(scope, receive, send)
            try:
                await asyncio.wait_for(_slots.acquire(), timeout=20)
            except TimeoutError:
                return await JSONResponse({'error': 'Demo is busy; please retry shortly'}, status_code=429)(scope, receive, send)
            finally:
                with _lock:
                    _waiting -= 1
        try:
            return await self.wrapped(scope, receive, send)
        finally:
            if heavy:
                _slots.release()


app.add_middleware(DemoLimits)
