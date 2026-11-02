"""The factor registry: identities, scores per spec, jobs and usage (SQLite).

* ``factors`` — one row per canonical factor id: the canonical text, the first
  spelling submitted, who proposed it, tags, parent factors.
* ``scores`` — one row per (factor, spec, panel version): the metrics an agent sorts
  and filters on. A *spec* is (universe, start, end, horizons, execution).
* ``jobs`` — submitted batches: owner, spec, progress, status.
* ``usage`` — compute seconds and factors per owner per day (budgets).

Novelty fingerprints (each factor's cross-sectional ranks on a fixed sample of days)
are kept next to the database as ``fingerprints/<spec>.npz``.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from pathlib import Path

__all__ = ["Registry", "spec_key"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS factors (
    id TEXT PRIMARY KEY, canonical TEXT NOT NULL, expr TEXT NOT NULL, owner TEXT,
    created REAL NOT NULL, tags TEXT NOT NULL DEFAULT '[]', parents TEXT NOT NULL DEFAULT '[]',
    note TEXT NOT NULL DEFAULT '', submits INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS scores (
    factor_id TEXT NOT NULL, spec TEXT NOT NULL, version TEXT NOT NULL, owner TEXT,
    rank_ic REAL, rank_icir REAL, ic REAL, icir REAL, turnover REAL, coverage REAL,
    long_short REAL, decay_halflife REAL, metrics TEXT NOT NULL, created REAL NOT NULL,
    PRIMARY KEY (factor_id, spec, version)
);
CREATE INDEX IF NOT EXISTS scores_spec ON scores (spec, rank_icir);
CREATE TABLE IF NOT EXISTS specs (
    spec TEXT PRIMARY KEY, universe TEXT, start TEXT, end_ TEXT, horizons TEXT, execution TEXT
);
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY, owner TEXT, spec TEXT, status TEXT NOT NULL, total INTEGER NOT NULL,
    done INTEGER NOT NULL DEFAULT 0, failed INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL,
    started REAL, finished REAL, error TEXT, factor_ids TEXT NOT NULL DEFAULT '[]',
    exprs TEXT NOT NULL DEFAULT '[]', label TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS jobs_owner ON jobs (owner, created);
CREATE TABLE IF NOT EXISTS usage (
    owner TEXT NOT NULL, day TEXT NOT NULL, seconds REAL NOT NULL DEFAULT 0,
    factors INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (owner, day)
);
"""

_METRIC_COLS = ("rank_ic", "rank_icir", "ic", "icir", "turnover", "coverage", "long_short", "decay_halflife")


def spec_key(universe: str, start: str, end: str, horizons, execution: str) -> str:
    raw = json.dumps([universe, str(start), str(end), sorted(int(h) for h in horizons), execution])
    return hashlib.blake2b(raw.encode(), digest_size=8).hexdigest()


class Registry:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self._conn() as c:
            c.executescript(_SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        return c

    # ── factors ──────────────────────────────────────────────────────────────
    def add_factor(self, fid: str, canonical: str, expr: str, owner: str | None,
                   tags=(), parents=(), note: str = "") -> bool:
        """Record a factor; returns True if it was new."""
        with self._lock, self._conn() as c:
            cur = c.execute("UPDATE factors SET submits = submits + 1 WHERE id = ?", (fid,))
            if cur.rowcount:
                return False
            c.execute("INSERT INTO factors (id, canonical, expr, owner, created, tags, parents, note) "
                      "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                      (fid, canonical, expr, owner, time.time(), json.dumps(list(tags)),
                       json.dumps(list(parents)), note[:500]))
            return True

    def factor(self, fid: str) -> dict | None:
        with self._conn() as c:
            r = c.execute("SELECT * FROM factors WHERE id = ?", (fid,)).fetchone()
            if r is None:
                return None
            scores = [self._score_row(s) for s in c.execute(
                "SELECT s.*, p.universe, p.start, p.end_, p.horizons FROM scores s LEFT JOIN specs p "
                "ON p.spec = s.spec WHERE factor_id = ? ORDER BY created DESC LIMIT 20", (fid,))]
        return {**self._factor_row(r), "scores": scores}

    @staticmethod
    def _factor_row(r) -> dict:
        return {"id": r["id"], "expr": r["expr"], "canonical": r["canonical"], "owner": r["owner"],
                "created": r["created"], "tags": json.loads(r["tags"]), "parents": json.loads(r["parents"]),
                "note": r["note"], "submits": r["submits"]}

    # ── scores ───────────────────────────────────────────────────────────────
    def add_spec(self, spec: str, universe, start, end, horizons, execution) -> None:
        with self._lock, self._conn() as c:
            c.execute("INSERT OR IGNORE INTO specs VALUES (?, ?, ?, ?, ?, ?)",
                      (spec, universe, str(start), str(end), json.dumps(list(horizons)), execution))

    def score(self, fid: str, spec: str, version: str) -> dict | None:
        with self._conn() as c:
            r = c.execute("SELECT * FROM scores WHERE factor_id = ? AND spec = ? AND version = ?",
                          (fid, spec, version)).fetchone()
        return self._score_row(r) if r else None

    def put_score(self, fid: str, spec: str, version: str, owner: str | None, metrics: dict) -> None:
        vals = [metrics.get(k) for k in _METRIC_COLS]
        with self._lock, self._conn() as c:
            c.execute("INSERT OR REPLACE INTO scores (factor_id, spec, version, owner, "
                      + ", ".join(_METRIC_COLS) + ", metrics, created) VALUES (?, ?, ?, ?, "
                      + ", ".join("?" * len(_METRIC_COLS)) + ", ?, ?)",
                      (fid, spec, version, owner, *vals, json.dumps(metrics), time.time()))

    @staticmethod
    def _score_row(r) -> dict:
        d = {k: r[k] for k in _METRIC_COLS}
        d.update(spec=r["spec"], version=r["version"], created=r["created"])
        keys = r.keys()
        if "universe" in keys:
            d.update(universe=r["universe"], start=r["start"], end=r["end_"],
                     horizons=json.loads(r["horizons"]) if r["horizons"] else None)
        return d

    def query(self, spec: str | None = None, *, sort_by: str = "rank_icir", descending: bool = True,
              min_abs_rank_icir: float | None = None, owner: str | None = None, tag: str | None = None,
              limit: int = 20, offset: int = 0) -> list[dict]:
        if sort_by not in _METRIC_COLS and sort_by != "created":
            raise ValueError(f"sort_by is one of {_METRIC_COLS + ('created',)}")
        where, args = [], []
        if spec:
            where.append("s.spec = ?"); args.append(spec)
        if min_abs_rank_icir is not None:
            where.append("abs(s.rank_icir) >= ?"); args.append(float(min_abs_rank_icir))
        if owner:
            where.append("f.owner = ?"); args.append(owner)
        if tag:
            where.append("f.tags LIKE ?"); args.append(f'%"{tag}"%')
        order = f"s.{sort_by}" if sort_by != "created" else "s.created"
        sql = ("SELECT f.id, f.expr, f.owner, f.tags, s.spec, s.version, " + ", ".join(f"s.{k}" for k in _METRIC_COLS)
               + " FROM scores s JOIN factors f ON f.id = s.factor_id"
               + (" WHERE " + " AND ".join(where) if where else "")
               + f" ORDER BY {order} IS NULL, {'abs(' + order + ')' if sort_by in ('rank_ic', 'rank_icir', 'ic', 'icir') else order} "
               + ("DESC" if descending else "ASC") + " LIMIT ? OFFSET ?")
        with self._conn() as c:
            rows = c.execute(sql, (*args, int(limit), int(offset))).fetchall()
        return [{"id": r["id"], "expr": r["expr"], "owner": r["owner"], "tags": json.loads(r["tags"]),
                 "spec": r["spec"], **{k: _round(r[k]) for k in _METRIC_COLS}} for r in rows]

    def count(self, spec: str | None = None) -> int:
        with self._conn() as c:
            if spec:
                return int(c.execute("SELECT count(*) FROM scores WHERE spec = ?", (spec,)).fetchone()[0])
            return int(c.execute("SELECT count(*) FROM factors").fetchone()[0])

    # ── jobs ─────────────────────────────────────────────────────────────────
    def create_job(self, jid: str, owner: str | None, spec: str, exprs: list[str], label: str = "") -> None:
        with self._lock, self._conn() as c:
            c.execute("INSERT INTO jobs (id, owner, spec, status, total, created, exprs, label) "
                      "VALUES (?, ?, ?, 'queued', ?, ?, ?, ?)",
                      (jid, owner, spec, len(exprs), time.time(), json.dumps(exprs), label[:120]))

    def update_job(self, jid: str, **fields) -> None:
        if not fields:
            return
        if "factor_ids" in fields:
            fields["factor_ids"] = json.dumps(fields["factor_ids"])
        with self._lock, self._conn() as c:
            c.execute(f"UPDATE jobs SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?", (*fields.values(), jid))

    def job(self, jid: str) -> dict | None:
        with self._conn() as c:
            r = c.execute("SELECT * FROM jobs WHERE id = ?", (jid,)).fetchone()
        if r is None:
            return None
        d = dict(r)
        d["factor_ids"] = json.loads(d["factor_ids"])
        d["exprs"] = json.loads(d["exprs"])
        return d

    def jobs(self, owner: str | None = None, limit: int = 20) -> list[dict]:
        with self._conn() as c:
            rows = c.execute("SELECT id, owner, spec, status, total, done, failed, created, finished, label "
                             "FROM jobs" + (" WHERE owner = ?" if owner else "") + " ORDER BY created DESC LIMIT ?",
                             ((owner, limit) if owner else (limit,))).fetchall()
        return [dict(r) for r in rows]

    # ── usage ────────────────────────────────────────────────────────────────
    def add_usage(self, owner: str | None, seconds: float, factors: int) -> None:
        day = time.strftime("%Y-%m-%d", time.gmtime())
        with self._lock, self._conn() as c:
            c.execute("INSERT INTO usage VALUES (?, ?, ?, ?) ON CONFLICT(owner, day) DO UPDATE SET "
                      "seconds = seconds + excluded.seconds, factors = factors + excluded.factors",
                      (owner or "anonymous", day, float(seconds), int(factors)))

    def usage_today(self, owner: str | None) -> dict:
        day = time.strftime("%Y-%m-%d", time.gmtime())
        with self._conn() as c:
            r = c.execute("SELECT seconds, factors FROM usage WHERE owner = ? AND day = ?",
                          (owner or "anonymous", day)).fetchone()
        return {"day": day, "seconds": float(r["seconds"]) if r else 0.0, "factors": int(r["factors"]) if r else 0}


def _round(v):
    return round(v, 5) if isinstance(v, float) else v
