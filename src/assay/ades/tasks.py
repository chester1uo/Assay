"""The ADES task queue: leased factor slices in SQLite, owned by the controller.

A *task* is one slice of a FactorDB job: up to ``CHUNK`` (factor id, expression)
pairs on one spec. Workers never touch the database; they call the controller's
``/v1/ades/*`` routes, which use this class.

* **lease** — the oldest queued task of the owner served least recently (owners are
  interleaved, so one agent's sweep does not starve another's), restricted to the
  panel version the worker holds. The lease expires after ``lease_s`` seconds
  unless the worker heartbeats.
* **complete** — accepted only from the worker holding the lease, once: a late or
  repeated completion (after a lease expired and the task went to someone else) is
  ignored, so a slice is never counted twice.
* **fail / expiry** — the task is queued again until ``max_attempts``, then failed.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path

__all__ = ["TaskQueue"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY, job_id TEXT NOT NULL, owner TEXT NOT NULL, spec TEXT NOT NULL,
    version TEXT NOT NULL, items TEXT NOT NULL, n INTEGER NOT NULL,
    status TEXT NOT NULL, worker TEXT, lease_until REAL, attempts INTEGER NOT NULL DEFAULT 0,
    error TEXT, created REAL NOT NULL, updated REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS tasks_status ON tasks (status, owner, created);
CREATE TABLE IF NOT EXISTS owners (owner TEXT PRIMARY KEY, served REAL NOT NULL);
CREATE TABLE IF NOT EXISTS workers (
    name TEXT PRIMARY KEY, seen REAL NOT NULL, version TEXT, info TEXT NOT NULL DEFAULT '{}',
    done INTEGER NOT NULL DEFAULT 0, failed INTEGER NOT NULL DEFAULT 0, seconds REAL NOT NULL DEFAULT 0
);
"""


class TaskQueue:
    def __init__(self, path: str | Path, *, lease_s: float = 120.0, max_attempts: int = 3):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lease_s, self.max_attempts = lease_s, max_attempts
        self._lock = threading.RLock()
        with self._conn() as c:
            c.executescript(_SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        return c

    # ── controller side ──────────────────────────────────────────────────────
    def enqueue(self, job_id: str, owner: str, spec: str, version: str, items: list, chunk: int) -> int:
        """Split ``items`` ((factor_id, expr) pairs) into tasks; returns how many."""
        now = time.time()
        rows = [("t_" + uuid.uuid4().hex[:16], job_id, owner, spec, version, json.dumps(items[i:i + chunk]),
                 len(items[i:i + chunk]), "queued", now, now) for i in range(0, len(items), chunk)]
        with self._lock, self._conn() as c:
            c.executemany("INSERT INTO tasks (id, job_id, owner, spec, version, items, n, status, created, updated)"
                          " VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
            c.execute("INSERT OR IGNORE INTO owners VALUES (?, 0)", (owner,))
        return len(rows)

    def reap(self, now: float | None = None) -> list[dict]:
        """Requeue (or fail) tasks whose lease ran out; returns the tasks that failed for good."""
        now = now or time.time()
        failed = []
        with self._lock, self._conn() as c:
            for r in c.execute("SELECT * FROM tasks WHERE status='leased' AND lease_until < ?", (now,)).fetchall():
                failed += self._retry(c, r, f"lease expired on {r['worker']}", now)
        return failed

    def _retry(self, c, r, error: str, now: float) -> list[dict]:
        if r["attempts"] >= self.max_attempts:
            c.execute("UPDATE tasks SET status='failed', error=?, worker=NULL, lease_until=NULL, updated=? WHERE id=?",
                      (error, now, r["id"]))
            return [{**dict(r), "error": error}]
        c.execute("UPDATE tasks SET status='queued', error=?, worker=NULL, lease_until=NULL, updated=? WHERE id=?",
                  (error, now, r["id"]))
        return []

    def drop_job(self, job_id: str) -> None:
        with self._lock, self._conn() as c:
            c.execute("DELETE FROM tasks WHERE job_id=? AND status='queued'", (job_id,))

    # ── worker side (through the controller's routes) ────────────────────────
    def lease(self, worker: str, version: str, info: dict | None = None) -> dict | None:
        now = time.time()
        with self._lock, self._conn() as c:
            self._seen(c, worker, version, info, now)
            c.execute("BEGIN IMMEDIATE")
            r = c.execute(
                "SELECT t.* FROM tasks t JOIN owners o ON o.owner = t.owner"
                " WHERE t.status='queued' AND t.version=? ORDER BY o.served, t.created LIMIT 1", (version,)).fetchone()
            if r is None:
                c.execute("COMMIT")
                return None
            c.execute("UPDATE tasks SET status='leased', worker=?, lease_until=?, attempts=attempts+1, updated=?"
                      " WHERE id=?", (worker, now + self.lease_s, now, r["id"]))
            c.execute("UPDATE owners SET served=? WHERE owner=?", (now, r["owner"]))
            c.execute("COMMIT")
        return {"task_id": r["id"], "job_id": r["job_id"], "owner": r["owner"], "spec": r["spec"],
                "version": r["version"], "items": json.loads(r["items"]), "lease_s": self.lease_s,
                "attempt": r["attempts"] + 1}

    def heartbeat(self, task_id: str, worker: str) -> bool:
        now = time.time()
        with self._lock, self._conn() as c:
            cur = c.execute("UPDATE tasks SET lease_until=?, updated=? WHERE id=? AND worker=? AND status='leased'",
                            (now + self.lease_s, now, task_id, worker))
            c.execute("UPDATE workers SET seen=? WHERE name=?", (now, worker))
            return cur.rowcount == 1

    def complete(self, task_id: str, worker: str, done: int, failed: int, seconds: float) -> dict | None:
        """Mark a leased task done; returns the task, or ``None`` when this worker no longer holds it."""
        now = time.time()
        with self._lock, self._conn() as c:
            r = c.execute("SELECT * FROM tasks WHERE id=? AND worker=? AND status='leased'",
                          (task_id, worker)).fetchone()
            if r is None:
                return None
            c.execute("UPDATE tasks SET status='done', lease_until=NULL, updated=? WHERE id=?", (now, task_id))
            c.execute("UPDATE workers SET seen=?, done=done+?, failed=failed+?, seconds=seconds+? WHERE name=?",
                      (now, done, failed, seconds, worker))
        return dict(r)

    def fail(self, task_id: str, worker: str, error: str) -> list[dict]:
        now = time.time()
        with self._lock, self._conn() as c:
            r = c.execute("SELECT * FROM tasks WHERE id=? AND worker=? AND status='leased'",
                          (task_id, worker)).fetchone()
            return self._retry(c, r, f"{worker}: {error}"[:500], now) if r is not None else []

    def _seen(self, c, worker: str, version: str, info: dict | None, now: float) -> None:
        c.execute("INSERT INTO workers (name, seen, version, info) VALUES (?,?,?,?) ON CONFLICT(name) DO UPDATE"
                  " SET seen=excluded.seen, version=excluded.version, info=excluded.info",
                  (worker, now, version, json.dumps(info or {})))

    # ── status ───────────────────────────────────────────────────────────────
    def status(self) -> dict:
        now = time.time()
        with self._lock, self._conn() as c:
            counts = {r["status"]: r["n"] for r in c.execute("SELECT status, COUNT(*) n FROM tasks GROUP BY status")}
            workers = [{"name": r["name"], "seen_s_ago": round(now - r["seen"], 1), "panel": r["version"],
                        "done": r["done"], "failed": r["failed"], "seconds": round(r["seconds"], 1),
                        **json.loads(r["info"] or "{}")}
                       for r in c.execute("SELECT * FROM workers ORDER BY name")]
        return {"tasks": counts, "workers": workers}
