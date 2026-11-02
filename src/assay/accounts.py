"""User accounts, invitations, personal API keys and signed sessions (stdlib only).

Modelled on QFinVerse's account system:

* Users live in SQLite at ``<state>/auth.sqlite3`` (``ASSAY_AUTH_DB`` overrides; the
  state directory is ``ASSAY_STATE_DIR``, else ``$ASSAY_DATA_DIR/_state``). Passwords
  are hashed with scrypt; API keys and invitation / reset tokens are stored as SHA-256
  digests and shown in the clear exactly once.
* Registration is by invitation only. An admin creates an invitation (role, number
  of uses, expiry) and sends its link; the first admin is created with the one-time
  setup token in ``<state>/setup_token``.
* Browser sessions are HMAC-signed cookies carrying the user id and a per-user
  *session version*; changing a password, the role or disabling a user bumps the
  version, which signs out every browser. The secret comes from ``ASSAY_SECRET_KEY``
  or is generated once into ``<state>/auth_secret`` (mode 0600).

Roles: ``researcher`` (factors, backtests, library, own API keys) and ``admin``
(also the Data Manager, scheduler and user management).

House style: ``from __future__ import annotations``, type hints, concise docstrings.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "ROLES", "KEY_PREFIX", "AccountError", "User", "AccountStore", "default_db_path",
    "load_secret", "sign_session", "read_session", "ensure_setup_token", "setup_token_path",
]

ROLES = ("researcher", "admin")
KEY_PREFIX = "asy_"
_USERNAME_OK = set("abcdefghijklmnopqrstuvwxyz0123456789._-@")
_SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1}


class AccountError(ValueError):
    """A request the account store refuses (bad input, unknown user, invalid token)."""


def _state_dir() -> Path:
    """``ASSAY_STATE_DIR``, else ``$ASSAY_DATA_DIR/_state`` (keep it on a local disk: SQLite)."""
    if os.environ.get("ASSAY_STATE_DIR"):
        return Path(os.environ["ASSAY_STATE_DIR"]).expanduser()
    return Path(os.environ.get("ASSAY_DATA_DIR", "data")).expanduser() / "_state"


def default_db_path() -> Path:
    return Path(os.environ.get("ASSAY_AUTH_DB") or _state_dir() / "auth.sqlite3")


# ── passwords ────────────────────────────────────────────────────────────

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, dklen=32, **_SCRYPT)
    return "scrypt${n}${r}${p}${s}${h}".format(
        s=base64.b64encode(salt).decode(), h=base64.b64encode(dk).decode(), **_SCRYPT)


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt, digest = stored.split("$")
        if algo != "scrypt":
            return False
        dk = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), dklen=32,
                            n=int(n), r=int(r), p=int(p))
        return hmac.compare_digest(dk, base64.b64decode(digest))
    except (ValueError, TypeError):
        return False


def check_password(password: object) -> str:
    if not isinstance(password, str) or not 10 <= len(password) <= 1024:
        raise AccountError("password must be 10-1024 characters")
    return password


def check_username(username: object) -> str:
    if not isinstance(username, str):
        raise AccountError("invalid username")
    username = username.strip().lower()
    if not username or len(username) > 64 or set(username) - _USERNAME_OK:
        raise AccountError("username: 1-64 characters of a-z 0-9 . _ - @")
    return username


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# ── the store ────────────────────────────────────────────────────────────

@dataclass
class User:
    id: int
    username: str
    role: str
    display_name: str = ""
    disabled: bool = False
    session_version: int = 1
    created_at: float = 0.0
    last_login_at: float | None = None

    def public(self) -> dict:
        return {"id": self.id, "username": self.username, "role": self.role,
                "display_name": self.display_name or self.username, "disabled": self.disabled,
                "created_at": self.created_at, "last_login_at": self.last_login_at}

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    display_name TEXT NOT NULL DEFAULT '',
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL,
    disabled INTEGER NOT NULL DEFAULT 0,
    session_version INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL,
    last_login_at REAL
);
CREATE TABLE IF NOT EXISTS api_keys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    prefix TEXT NOT NULL,
    key_hash TEXT UNIQUE NOT NULL,
    created_at REAL NOT NULL,
    last_used_at REAL,
    revoked INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS invites (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_hash TEXT UNIQUE NOT NULL,
    prefix TEXT NOT NULL,
    label TEXT NOT NULL,
    role TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    max_uses INTEGER NOT NULL,
    uses INTEGER NOT NULL DEFAULT 0,
    revoked INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS password_resets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash TEXT UNIQUE NOT NULL,
    version INTEGER NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    used INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL, actor TEXT, action TEXT NOT NULL, detail TEXT
);
"""


class AccountStore:
    """Thread-safe access to the account database (one short connection per call)."""

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else default_db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self._conn() as c:
            c.executescript(_SCHEMA)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path, timeout=10)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys = ON")
        return c

    @staticmethod
    def _user(row) -> User | None:
        if row is None:
            return None
        return User(id=row["id"], username=row["username"], role=row["role"],
                    display_name=row["display_name"], disabled=bool(row["disabled"]),
                    session_version=row["session_version"], created_at=row["created_at"],
                    last_login_at=row["last_login_at"])

    def audit(self, actor: str | None, action: str, detail: str = "") -> None:
        with self._lock, self._conn() as c:
            c.execute("INSERT INTO audit (ts, actor, action, detail) VALUES (?, ?, ?, ?)",
                      (time.time(), actor, action, detail[:500]))

    # users ---------------------------------------------------------------
    def count_users(self) -> int:
        with self._conn() as c:
            return int(c.execute("SELECT count(*) FROM users").fetchone()[0])

    def _insert_user(self, c, username: str, password: str, role: str, display_name: str) -> int:
        if role not in ROLES:
            raise AccountError(f"role must be one of {', '.join(ROLES)}")
        try:
            return c.execute(
                "INSERT INTO users (username, display_name, password_hash, role, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (username, str(display_name or "")[:120], hash_password(password), role, time.time()),
            ).lastrowid
        except sqlite3.IntegrityError as exc:
            raise AccountError("username already taken") from exc

    def create_user(self, username: str, password: str, role: str = "researcher",
                    display_name: str = "") -> User:
        username, password = check_username(username), check_password(password)
        with self._lock, self._conn() as c:
            uid = self._insert_user(c, username, password, role, display_name)
        return self.get_user(uid)

    def get_user(self, user_id: int) -> User | None:
        with self._conn() as c:
            return self._user(c.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone())

    def list_users(self) -> list[User]:
        with self._conn() as c:
            return [self._user(r) for r in c.execute("SELECT * FROM users ORDER BY id")]

    def authenticate(self, username: str, password: str) -> User | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM users WHERE username = ?",
                            ((username or "").strip().lower(),)).fetchone()
        if row is None:
            hash_password("timing-equaliser")         # same cost as a real check
            return None
        if not verify_password(password or "", row["password_hash"]) or row["disabled"]:
            return None
        with self._lock, self._conn() as c:
            c.execute("UPDATE users SET last_login_at = ? WHERE id = ?", (time.time(), row["id"]))
        return self.get_user(row["id"])

    def _admins_left(self, c, user_id: int) -> int:
        return int(c.execute("SELECT count(*) FROM users WHERE role = 'admin' AND disabled = 0 AND id != ?",
                             (user_id,)).fetchone()[0])

    def update_user(self, user_id: int, *, role: str | None = None, disabled: bool | None = None,
                    display_name: str | None = None) -> User:
        if role is not None and role not in ROLES:
            raise AccountError(f"role must be one of {', '.join(ROLES)}")
        if disabled is not None and not isinstance(disabled, bool):
            raise AccountError("disabled must be true or false")
        with self._lock, self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            if row is None:
                raise AccountError("user not found")
            losing_admin = row["role"] == "admin" and not row["disabled"] and (role not in (None, "admin") or disabled)
            if losing_admin and self._admins_left(c, user_id) == 0:
                raise AccountError("cannot demote or disable the last admin")
            if role is not None and role != row["role"]:
                c.execute("UPDATE users SET role = ?, session_version = session_version + 1 WHERE id = ?",
                          (role, user_id))
            if disabled is not None and disabled != bool(row["disabled"]):
                c.execute("UPDATE users SET disabled = ?, session_version = session_version + 1 WHERE id = ?",
                          (int(disabled), user_id))
                if disabled:
                    c.execute("UPDATE api_keys SET revoked = 1 WHERE user_id = ?", (user_id,))
            if display_name is not None:
                c.execute("UPDATE users SET display_name = ? WHERE id = ?", (str(display_name)[:120], user_id))
        return self.get_user(user_id)

    def set_password(self, user_id: int, password: str) -> None:
        hashed = hash_password(check_password(password))
        with self._lock, self._conn() as c:
            c.execute("UPDATE users SET password_hash = ?, session_version = session_version + 1 WHERE id = ?",
                      (hashed, user_id))

    def delete_user(self, user_id: int) -> None:
        with self._lock, self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            if row is None:
                raise AccountError("user not found")
            if row["role"] == "admin" and not row["disabled"] and self._admins_left(c, user_id) == 0:
                raise AccountError("cannot delete the last admin")
            c.execute("DELETE FROM users WHERE id = ?", (user_id,))

    # personal API keys ---------------------------------------------------
    @staticmethod
    def _key_row(r) -> dict:
        return {"id": r["id"], "user_id": r["user_id"], "username": r["username"], "name": r["name"],
                "prefix": f"{KEY_PREFIX}{r['prefix']}", "created_at": r["created_at"],
                "last_used_at": r["last_used_at"], "revoked": bool(r["revoked"])}

    _KEY_SQL = "SELECT k.*, u.username FROM api_keys k JOIN users u ON u.id = k.user_id"

    def create_api_key(self, user_id: int, name: str = "") -> tuple[dict, str]:
        if self.get_user(user_id) is None:
            raise AccountError("user not found")
        prefix = secrets.token_hex(4)
        token = f"{KEY_PREFIX}{prefix}_{secrets.token_urlsafe(32)}"
        with self._lock, self._conn() as c:
            kid = c.execute(
                "INSERT INTO api_keys (user_id, name, prefix, key_hash, created_at) VALUES (?, ?, ?, ?, ?)",
                (user_id, (name or "").strip()[:80] or "key", prefix, _digest(token), time.time())).lastrowid
            row = c.execute(self._KEY_SQL + " WHERE k.id = ?", (kid,)).fetchone()
        return self._key_row(row), token

    def list_api_keys(self, user_id: int | None = None) -> list[dict]:
        sql, args = self._KEY_SQL + " WHERE k.revoked = 0", ()
        if user_id is not None:
            sql, args = sql + " AND k.user_id = ?", (user_id,)
        with self._conn() as c:
            return [self._key_row(r) for r in c.execute(sql + " ORDER BY k.id", args)]

    def revoke_api_key(self, key_id: int, user_id: int | None = None) -> None:
        sql, args = "UPDATE api_keys SET revoked = 1 WHERE id = ? AND revoked = 0", [key_id]
        if user_id is not None:
            sql += " AND user_id = ?"
            args.append(user_id)
        with self._lock, self._conn() as c:
            if c.execute(sql, args).rowcount == 0:
                raise AccountError("key not found")

    def verify_api_key(self, token: str) -> User | None:
        if not token or not token.startswith(KEY_PREFIX):
            return None
        with self._conn() as c:
            row = c.execute("SELECT k.id AS kid, k.last_used_at, u.* FROM api_keys k "
                            "JOIN users u ON u.id = k.user_id WHERE k.key_hash = ? AND k.revoked = 0",
                            (_digest(token),)).fetchone()
        if row is None or row["disabled"]:
            return None
        now = time.time()
        if not row["last_used_at"] or now - row["last_used_at"] > 60:     # cheap, coarse
            with self._lock, self._conn() as c:
                c.execute("UPDATE api_keys SET last_used_at = ? WHERE id = ?", (now, row["kid"]))
        return self._user(row)

    # invitations ---------------------------------------------------------
    @staticmethod
    def _invite_row(r) -> dict:
        now = time.time()
        live = not r["revoked"] and r["expires_at"] > now and r["uses"] < r["max_uses"]
        return {"id": r["id"], "prefix": r["prefix"], "label": r["label"], "role": r["role"],
                "created_by": r["created_by"], "created_at": r["created_at"], "expires_at": r["expires_at"],
                "max_uses": r["max_uses"], "uses": r["uses"], "revoked": bool(r["revoked"]),
                "expired": r["expires_at"] <= now, "live": live}

    def create_invite(self, actor: str, *, role: str = "researcher", max_uses: int = 1,
                      expires_in_days: float = 7, label: str = "") -> tuple[dict, str]:
        if role not in ROLES:
            raise AccountError(f"role must be one of {', '.join(ROLES)}")
        if type(max_uses) is not int or not 1 <= max_uses <= 100:
            raise AccountError("max_uses must be between 1 and 100")
        if type(expires_in_days) not in (int, float) or not 0 < expires_in_days <= 90:
            raise AccountError("expiry must be between 0 and 90 days")
        token = "asi_" + secrets.token_urlsafe(24)
        now = time.time()
        with self._lock, self._conn() as c:
            iid = c.execute(
                "INSERT INTO invites (token_hash, prefix, label, role, created_by, created_at, expires_at, max_uses) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (_digest(token), token[:10], str(label or "")[:120], role, actor, now,
                 now + expires_in_days * 86400, max_uses)).lastrowid
            row = c.execute("SELECT * FROM invites WHERE id = ?", (iid,)).fetchone()
        return self._invite_row(row), token

    def list_invites(self) -> list[dict]:
        with self._conn() as c:
            return [self._invite_row(r) for r in c.execute("SELECT * FROM invites ORDER BY id DESC LIMIT 200")]

    def revoke_invite(self, invite_id: int) -> None:
        with self._lock, self._conn() as c:
            if not c.execute("UPDATE invites SET revoked = 1 WHERE id = ?", (invite_id,)).rowcount:
                raise AccountError("invitation not found")

    def check_invite(self, code: str) -> dict | None:
        """The invitation behind ``code`` if it can still be used (for the sign-up form)."""
        with self._conn() as c:
            row = c.execute("SELECT * FROM invites WHERE token_hash = ?", (_digest((code or "").strip()),)).fetchone()
        if row is None:
            return None
        inv = self._invite_row(row)
        return inv if inv["live"] else None

    def register(self, code: str, username: str, password: str, display_name: str = "") -> User:
        username, password = check_username(username), check_password(password)
        with self._lock, self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT * FROM invites WHERE token_hash = ?",
                            (_digest((code or "").strip()),)).fetchone()
            if row is None or not self._invite_row(row)["live"]:
                raise AccountError("invitation is invalid, expired or used up")
            uid = self._insert_user(c, username, password, row["role"], display_name)
            c.execute("UPDATE invites SET uses = uses + 1 WHERE id = ?", (row["id"],))
        return self.get_user(uid)

    # password resets -----------------------------------------------------
    def create_reset(self, user_id: int, minutes: int = 60) -> dict:
        if type(minutes) is not int or not 5 <= minutes <= 24 * 60:
            raise AccountError("reset lifetime must be 5 minutes to 24 hours")
        token = "asr_" + secrets.token_urlsafe(32)
        with self._lock, self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            if row is None or row["disabled"]:
                raise AccountError("active user not found")
            c.execute("UPDATE password_resets SET used = 1 WHERE user_id = ?", (user_id,))
            expires = time.time() + minutes * 60
            c.execute("INSERT INTO password_resets (user_id, token_hash, version, created_at, expires_at) "
                      "VALUES (?, ?, ?, ?, ?)", (user_id, _digest(token), row["session_version"], time.time(), expires))
        return {"token": token, "expires_at": expires}

    def redeem_reset(self, token: str, password: str) -> User:
        hashed = hash_password(check_password(password))
        with self._lock, self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT r.*, u.session_version, u.disabled FROM password_resets r "
                            "JOIN users u ON u.id = r.user_id WHERE token_hash = ?",
                            (_digest((token or "").strip()),)).fetchone()
            if (row is None or row["used"] or row["disabled"] or row["expires_at"] <= time.time()
                    or row["version"] != row["session_version"]):
                raise AccountError("reset link is invalid or expired")
            c.execute("UPDATE users SET password_hash = ?, session_version = session_version + 1 WHERE id = ?",
                      (hashed, row["user_id"]))
            c.execute("UPDATE password_resets SET used = 1 WHERE user_id = ?", (row["user_id"],))
        return self.get_user(row["user_id"])


# ── signed session cookies ──────────────────────────────────────────────

def _write_once(path: Path, value: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return path.read_text().strip()
    with os.fdopen(fd, "w") as f:
        f.write(value)
    return value


def load_secret() -> bytes:
    env = os.environ.get("ASSAY_SECRET_KEY", "").strip()
    if env:
        return env.encode()
    return _write_once(_state_dir() / "auth_secret", secrets.token_urlsafe(48)).encode()


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def sign_session(secret: bytes, user: User, ttl_s: int) -> str:
    payload = json.dumps({"u": user.id, "v": user.session_version, "exp": int(time.time()) + int(ttl_s)},
                         separators=(",", ":")).encode()
    return f"{_b64(payload)}.{_b64(hmac.new(secret, payload, hashlib.sha256).digest())}"


def read_session(secret: bytes, token: str) -> dict | None:
    try:
        p, s = token.split(".", 1)
        payload = _unb64(p)
        if not hmac.compare_digest(_unb64(s), hmac.new(secret, payload, hashlib.sha256).digest()):
            return None
        data = json.loads(payload)
        return data if int(data.get("exp", 0)) >= time.time() else None
    except (ValueError, TypeError, json.JSONDecodeError):
        return None


def setup_token_path() -> Path:
    return _state_dir() / "setup_token"


def ensure_setup_token() -> str:
    """One-time token that authorises creating the first admin from the web."""
    return _write_once(setup_token_path(), secrets.token_urlsafe(18))
