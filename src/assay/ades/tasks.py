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
import hashlib
import math
import sqlite3
import threading
import time
import uuid
from pathlib import Path

__all__ = ["TaskQueue"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS job_cancellations (
    job_id TEXT PRIMARY KEY, reason TEXT NOT NULL, created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS task_results (
    task_id TEXT PRIMARY KEY, payload TEXT NOT NULL, seconds REAL NOT NULL
);
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
            if 'lease_token' not in {r[1] for r in c.execute('PRAGMA table_info(tasks)')}:
                c.execute('ALTER TABLE tasks ADD COLUMN lease_token TEXT')

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        return c

    # ── controller side ──────────────────────────────────────────────────────
    def enqueue(self, job_id: str, owner: str, spec: str, version: str, items: list, chunk: int, *, idempotent: bool = False) -> int:
        """Split ``items`` ((factor_id, expr) pairs) into tasks; returns how many."""
        now = time.time()
        rows = [("rtsk_" + hashlib.sha256(f"{job_id}:{i}".encode()).hexdigest()[:24] if idempotent else "t_" + uuid.uuid4().hex[:16], job_id, owner, spec, version, json.dumps(items[i:i + chunk]),
                 len(items[i:i + chunk]), "queued", now, now) for i in range(0, len(items), chunk)]
        with self._lock, self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            for row in rows:
                previous = c.execute("SELECT job_id,owner,spec,version,items,n FROM tasks WHERE id=?", (row[0],)).fetchone()
                if previous is not None and tuple(previous) != tuple(row[1:7]):
                    raise ValueError("task identity belongs to different inputs")
            if c.execute("SELECT 1 FROM job_cancellations WHERE job_id=?", (job_id,)).fetchone():
                rows = [(*row[:7], "cancelled", *row[8:]) for row in rows]
            c.executemany("INSERT OR IGNORE INTO tasks (id, job_id, owner, spec, version, items, n, status, created, updated)"
                          " VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
            c.execute("INSERT OR IGNORE INTO owners VALUES (?, 0)", (owner,))
        return len(rows)

    def reap(self, now: float | None = None) -> list[dict]:
        """Requeue (or fail) tasks whose lease ran out; returns the tasks that failed for good."""
        now = now or time.time()
        failed = []
        with self._lock, self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            for r in c.execute("SELECT * FROM tasks WHERE status='leased' AND lease_until < ?", (now,)).fetchall():
                failed += self._retry(c, r, f"lease expired on {r['worker']}", now)
        return failed

    def _retry(self, c, r, error: str, now: float) -> list[dict]:
        if r["attempts"] >= self.max_attempts:
            c.execute("UPDATE tasks SET status='failed', error=?, worker=NULL, lease_until=NULL, updated=? WHERE id=?",
                      (error, now, r["id"]))
            results = [{"factor_id": fid, "metrics": None, "error": error}
                       for fid, _ in json.loads(r["items"])]
            c.execute("INSERT OR IGNORE INTO task_results VALUES (?,?,?)",
                      (r["id"], json.dumps(results), 0.0))
            return [{**dict(r), "error": error, "updated": now}]
        c.execute("UPDATE tasks SET status='queued', error=?, worker=NULL, lease_until=NULL, updated=? WHERE id=?",
                  (error, now, r["id"]))
        return []

    def cancel_job(self, job_id: str, reason: str) -> dict:
        """Fence queued/leased tasks atomically; accepted completion wins the race.

        A tombstone also fences a dispatch arriving after cancellation. This does
        not interrupt native code already computing on a remote worker.
        """
        with self._lock, self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            previous = c.execute("SELECT * FROM job_cancellations WHERE job_id=?", (job_id,)).fetchone()
            if previous is not None:
                return {**dict(previous), "status": "cancelled"}
            rows = c.execute("SELECT status FROM tasks WHERE job_id=?", (job_id,)).fetchall()
            if rows and all(r["status"] in {"done", "failed"} for r in rows):
                return {"job_id": job_id, "status": "finished"}
            now = time.time()
            c.execute("INSERT INTO job_cancellations VALUES (?,?,?)", (job_id, reason, now))
            c.execute("UPDATE tasks SET status='cancelled', worker=NULL, lease_until=NULL,"
                      " lease_token=NULL, error=?, updated=? WHERE job_id=? AND status IN ('queued','leased')",
                      (reason, now, job_id))
        return {"job_id": job_id, "reason": reason, "created": now, "status": "cancelled"}

    def cancellation_receipts(self, job_id=None):
        with self._conn() as c:
            return [dict(row) for row in c.execute(
                "SELECT * FROM job_cancellations" + (" WHERE job_id=?" if job_id else ""),
                (job_id,) if job_id else ()).fetchall()]

    def drop_job(self, job_id: str) -> None:
        with self._lock, self._conn() as c:
            c.execute("DELETE FROM tasks WHERE job_id=? AND status='queued'", (job_id,))

    # ── worker side (through the controller's routes) ────────────────────────
    def lease(self, worker: str, version: str | list[str], info: dict | None = None,
              *, supports_research_leases: bool = True) -> dict | None:
        versions = list(dict.fromkeys([version] if isinstance(version, str) else version))
        if not versions or len(versions) > 8 or any(not isinstance(v,str) or not v for v in versions):
            raise ValueError('workers must advertise between one and eight panel versions')
        with self._lock, self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            now = time.time()
            self._seen(c, worker, versions[0], {**(info or {}), 'panels':versions}, now)
            r = c.execute(
                "SELECT t.* FROM tasks t JOIN owners o ON o.owner = t.owner"
                " WHERE t.status='queued' AND t.version IN (" + ','.join('?' for _ in versions) + ')'
                + ('' if supports_research_leases else " AND substr(t.id,1,5) != 'rtsk_'")
                + " ORDER BY o.served, t.created LIMIT 1", versions).fetchone()
            if r is None:
                c.execute("COMMIT")
                return None
            token = uuid.uuid4().hex
            c.execute("UPDATE tasks SET status='leased', worker=?, lease_until=?, attempts=attempts+1, updated=?, lease_token=?"
                      " WHERE id=?", (worker, now + self.lease_s, now, token, r["id"]))
            c.execute("UPDATE owners SET served=? WHERE owner=?", (now, r["owner"]))
            c.execute("COMMIT")
        return {"task_id": r["id"], "job_id": r["job_id"], "owner": r["owner"], "spec": r["spec"],
                "version": r["version"], "items": json.loads(r["items"]), "lease_s": self.lease_s,
                "attempt": r["attempts"] + 1, "lease_token":token}

    def retire_versions(self, current: list[str]) -> list[dict]:
        """Retain durable failure receipts for queued work on retired snapshots."""
        if not current:
            return []
        error = 'data snapshot retired; resubmit against the current snapshot'
        now = time.time()
        with self._lock, self._conn() as c:
            c.execute('BEGIN IMMEDIATE')
            rows = c.execute("SELECT * FROM tasks WHERE status='queued' AND version NOT IN ("
                             + ','.join('?' for _ in current) + ')',current).fetchall()
            for row in rows:
                c.execute("UPDATE tasks SET status='failed',error=?,updated=? WHERE id=?",(error,now,row['id']))
                results = [dict(factor_id=fid,metrics=None,error=error) for fid,_ in json.loads(row['items'])]
                c.execute('INSERT OR IGNORE INTO task_results VALUES (?,?,?)',(row['id'],json.dumps(results),0.0))
        return [dict(row,error=error,updated=now) for row in rows]

    def heartbeat(self, task_id: str, worker: str, lease_token: str | None = None) -> bool:
        if task_id.startswith("rtsk_") and not lease_token:
            return False
        with self._lock, self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            now = time.time()
            cur = c.execute("UPDATE tasks SET lease_until=?, updated=? WHERE id=? AND worker=? AND status='leased' AND lease_until>=? AND (? IS NULL OR lease_token=?)",
                            (now + self.lease_s, now, task_id, worker, now, lease_token, lease_token))
            c.execute("UPDATE workers SET seen=? WHERE name=?", (now, worker))
            return cur.rowcount == 1

    def complete(self, task_id: str, worker: str, done: int, failed: int, seconds: float, *, lease_token: str | None = None, results: list | None = None) -> dict | None:
        """Mark a leased task done; returns the task, or ``None`` when this worker no longer holds it."""
        if task_id.startswith("rtsk_") and not lease_token:
            return None
        if not math.isfinite(seconds) or seconds < 0:
            raise ValueError("invalid task duration")
        with self._lock, self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            now = time.time()
            r = c.execute("SELECT * FROM tasks WHERE id=? AND worker=? AND status='leased' AND lease_until>=? AND (? IS NULL OR lease_token=?)",
                          (task_id, worker, now, lease_token, lease_token)).fetchone()
            if r is None:
                return None
            if results is not None:
                expected = {item[0] for item in json.loads(r["items"])}
                if not isinstance(results, list) or any(not isinstance(item, dict) for item in results):
                    raise ValueError("task results must be a list of objects")
                for item in results:
                    if item.get("metrics") is not None:
                        if not isinstance(item["metrics"], dict) or item["metrics"].get("error"):
                            raise ValueError("successful metrics must be an object without an error")
                    elif not item.get("error"):
                        raise ValueError("failed results need diagnostics")
                if done != sum(item.get("metrics") is not None for item in results) or done + failed != len(results):
                    raise ValueError("task result counts disagree")
                ids = [item.get("factor_id") for item in results]
                if len(ids) != len(expected) or set(ids) != expected:
                    raise ValueError("task results must cover each leased factor exactly once")
                c.execute("INSERT INTO task_results VALUES (?,?,?)",
                          (task_id, json.dumps(results, allow_nan=False), seconds))
            c.execute("UPDATE tasks SET status='done', lease_until=NULL, updated=? WHERE id=?", (now, task_id))
            c.execute("UPDATE workers SET seen=?, done=done+?, failed=failed+?, seconds=seconds+? WHERE name=?",
                      (now, done, failed, seconds, worker))
        return {**dict(r), "updated": now}

    def fail(self, task_id: str, worker: str, error: str, lease_token: str | None = None) -> list[dict]:
        if task_id.startswith("rtsk_") and not lease_token:
            return []
        with self._lock, self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            now = time.time()
            r = c.execute("SELECT * FROM tasks WHERE id=? AND worker=? AND status='leased' AND lease_until>=? AND (? IS NULL OR lease_token=?)",
                          (task_id, worker, now, lease_token, lease_token)).fetchone()
            return self._retry(c, r, f"{worker}: {error}"[:500], now) if r is not None else []

    def release(self, task_id: str, worker: str, lease_token: str | None = None) -> bool:
        """Hand a task back unfinished (the worker is stopping): queued again, attempt not counted."""
        if task_id.startswith("rtsk_") and not lease_token:
            return False
        with self._lock, self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            now = time.time()
            cur = c.execute("UPDATE tasks SET status='queued', worker=NULL, lease_until=NULL,"
                            " attempts=MAX(attempts-1, 0), updated=? WHERE id=? AND worker=? AND status='leased' AND lease_until>=? AND (? IS NULL OR lease_token=?)",
                            (now, task_id, worker, now, lease_token, lease_token))
            return cur.rowcount == 1

    def result_receipts(self, job_id=None):
        with self._conn() as c:
            rows = c.execute("SELECT t.*, r.payload, r.seconds FROM tasks t JOIN task_results r ON t.id=r.task_id"
                             + (" WHERE t.job_id=?" if job_id else ""), (job_id,) if job_id else ()).fetchall()
        return [(dict(row), json.loads(row["payload"]), row["seconds"]) for row in rows]

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
