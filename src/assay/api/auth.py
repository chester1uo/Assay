"""Who is calling: accounts, personal API keys and service keys (architecture §4.1).

Three ways in, checked in this order:

1. ``X-API-Key`` (or ``Authorization: Bearer``) holding a personal key (``asy_...``)
   from the account store, or one of the service keys in ``ASSAY_API_KEYS`` /
   ``ASSAY_ADMIN_KEYS``;
2. the signed ``assay_session`` cookie of a signed-in user (WebUI).

With ``ASSAY_ACCOUNTS=1`` the site runs on accounts (see :mod:`assay.accounts`) and
every API call needs one of the above. Without it the old behaviour holds: service
keys when ``ASSAY_API_KEYS`` is set, and **no auth at all when it is unset** — the
offline / single-user default, so the app boots and the test client works without
credentials.

Usage in a route::

    @router.post("/evaluate")
    async def evaluate(req: EvaluateRequest, api_key: str | None = Depends(get_api_key)):
        ...

``request.state.user`` then holds the caller (a :class:`~assay.accounts.User`; service
keys appear as user ``service`` with id 0).

House style: ``from __future__ import annotations``, type hints, concise docstrings.
"""

from __future__ import annotations

import os
import threading

from fastapi import Header, HTTPException, Request, status

from assay.accounts import KEY_PREFIX, AccountStore, User, load_secret, read_session

__all__ = [
    "get_api_key", "require_admin", "allowed_keys", "admin_keys", "auth_enabled",
    "accounts_enabled", "account_store", "session_secret", "resolve_user", "COOKIE",
]

COOKIE = "assay_session"
_lock = threading.Lock()
_store: AccountStore | None = None
_secret: bytes | None = None


def allowed_keys() -> set[str]:
    """Parse ``ASSAY_API_KEYS`` (comma-separated) into a set; empty set => no service keys.

    Read on every call (not cached) so tests / runtime can toggle the env var without
    re-importing the module. Blank entries and surrounding whitespace are ignored.
    """
    raw = os.environ.get("ASSAY_API_KEYS", "")
    return {k.strip() for k in raw.split(",") if k.strip()}


def admin_keys() -> set[str]:
    """Service keys allowed on operator routes (``/admin``, ``/v1/admin/*``):
    ``ASSAY_ADMIN_KEYS``. Unset: the same keys as everything else (``ASSAY_API_KEYS``)."""
    raw = os.environ.get("ASSAY_ADMIN_KEYS", "")
    return {k.strip() for k in raw.split(",") if k.strip()}


def accounts_enabled() -> bool:
    """True when the site runs on user accounts (``ASSAY_ACCOUNTS=1``)."""
    return os.environ.get("ASSAY_ACCOUNTS", "").strip().lower() in ("1", "true", "yes", "on")


def auth_enabled() -> bool:
    """True when callers must identify themselves (accounts on, or service keys set)."""
    return accounts_enabled() or bool(allowed_keys())


def account_store() -> AccountStore:
    """The process-wide account store (opened on first use)."""
    global _store
    with _lock:
        if _store is None:
            _store = AccountStore()
        return _store


def session_secret() -> bytes:
    global _secret
    with _lock:
        if _secret is None:
            _secret = load_secret()
        return _secret


def _reset_for_tests() -> None:
    global _store, _secret
    with _lock:
        _store, _secret = None, None


def _presented(x_api_key: str | None, authorization: str | None) -> str | None:
    if x_api_key:
        return x_api_key.strip()
    if authorization:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() == "bearer" and token:
            return token.strip()
    return None


def _service_user(key: str) -> User | None:
    admins = admin_keys()
    if key in admins:
        return User(id=0, username="service", role="admin")
    if key in allowed_keys():
        return User(id=0, username="service", role="researcher" if admins else "admin")
    return None


def _same_origin(request: Request) -> bool:
    origin = request.headers.get("origin")
    return not origin or origin.split("://", 1)[-1] == request.headers.get("host")


def resolve_user(request: Request, presented: str | None = None) -> User | None:
    """The caller behind a key or the session cookie, or ``None``."""
    if presented:
        if presented.startswith(KEY_PREFIX) and accounts_enabled():
            user = account_store().verify_api_key(presented)
            if user is not None:
                request.state.auth_via = "key"
                return user
        user = _service_user(presented)
        if user is not None:
            request.state.auth_via = "service"
        return user
    token = request.cookies.get(COOKIE)
    if not token or not accounts_enabled():
        return None
    data = read_session(session_secret(), token)
    if not data:
        return None
    user = account_store().get_user(int(data.get("u", 0)))
    if (user is None or user.disabled or user.session_version != data.get("v")
            or not user.workspace_id or user.workspace_id != data.get("w")):
        return None
    # A cookie rides along with cross-site requests; changes must come from this site.
    if request.method not in ("GET", "HEAD", "OPTIONS") and not _same_origin(request):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Cross-origin request refused.")
    request.state.auth_via = "session"
    return user


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=detail,
                         headers={"WWW-Authenticate": "X-API-Key"})


async def get_api_key(
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> str | None:
    """FastAPI dependency: identify the caller, or allow all when auth is off.

    Returns the caller's username (``None`` when auth is off) and stores the user on
    ``request.state.user``. A missing or unknown key / session raises ``HTTP 401``.
    """
    request.state.user = None
    if not auth_enabled():
        return None
    presented = _presented(x_api_key, authorization)
    user = resolve_user(request, presented)
    if user is None:
        if presented:
            raise _unauthorized("Invalid API key.")
        raise _unauthorized("Sign in, or set the X-API-Key header.")
    request.state.user = user
    return user.username


async def require_admin(
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> str | None:
    """Like :func:`get_api_key`, but the caller must be an admin: an admin account, or
    a key in ``ASSAY_ADMIN_KEYS`` (any service key when that is unset), so a research
    key cannot change credentials, data jobs or schedules."""
    if not auth_enabled() and admin_keys():
        # only operator keys configured: the rest of the API is open, these routes are not
        if _presented(x_api_key, authorization) not in admin_keys():
            raise _unauthorized("This route needs an admin key.")
        return "service"
    name = await get_api_key(request, x_api_key, authorization)
    user = request.state.user
    if user is not None and not user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="This needs an admin account or key.")
    return name
