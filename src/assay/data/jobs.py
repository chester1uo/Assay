"""Background job runner for long data tasks (download / ingest).

A tiny, dependency-free task queue: jobs run **one at a time** on a single worker
thread (the spec wants markets updated *sequentially*, and downloads/ingests are
heavy), while the API event loop stays free. Each :class:`Job` carries live status,
a 0..1 progress fraction, a rolling log, and a result/error — so the WebUI (and
``/admin``) can poll and show what's happening.

Usage::

    job = jobs.submit("update", "CN", "update", lambda j: orchestrate.run(..., job=j))
    ...                                     # in fn: j.log("…"); j.progress_to(0.5, "ingesting")
    jobs.get(job.id).to_dict()

State is in-memory (process-lifetime) — fine for a single-worker research server.
"""

from __future__ import annotations

import threading
import time
import traceback
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable

__all__ = ["Job", "submit", "get", "list_jobs"]

_LOCK = threading.RLock()
_JOBS: "dict[str, Job]" = {}
_ORDER: deque[str] = deque(maxlen=200)  # ids, oldest-first
# Single worker => sequential execution (markets update one after another).
_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="assay-job")


@dataclass
class Job:
    """One background task: identity, lifecycle, progress, log, result."""

    id: str
    kind: str            # "init" | "update" | ...
    market: str          # "US" | "CN" | ...
    mode: str            # free-form label (e.g. "init 2015..2026")
    status: str = "queued"   # queued | running | done | error
    progress: float = 0.0    # 0..1
    message: str = ""
    created_ts: float = field(default_factory=time.time)
    started_ts: float | None = None
    ended_ts: float | None = None
    error: str | None = None
    result: Any = None
    _logs: deque = field(default_factory=lambda: deque(maxlen=400))

    # -- called from inside the worker fn --
    def log(self, line: str) -> None:
        with _LOCK:
            self._logs.append({"ts": time.time(), "line": str(line)})

    def progress_to(self, frac: float, message: str | None = None) -> None:
        with _LOCK:
            self.progress = max(0.0, min(1.0, float(frac)))
            if message is not None:
                self.message = str(message)
                self._logs.append({"ts": time.time(), "line": str(message)})

    def to_dict(self, *, with_logs: bool = True) -> dict[str, Any]:
        with _LOCK:
            d = {
                "id": self.id, "kind": self.kind, "market": self.market, "mode": self.mode,
                "status": self.status, "progress": round(self.progress, 4), "message": self.message,
                "created_ts": self.created_ts, "started_ts": self.started_ts, "ended_ts": self.ended_ts,
                "error": self.error, "result": self.result,
            }
            if with_logs:
                d["logs"] = list(self._logs)
            return d


def submit(kind: str, market: str, mode: str, fn: Callable[["Job"], Any]) -> Job:
    """Queue ``fn(job)`` to run on the worker thread; returns the :class:`Job` at once."""
    job = Job(id=uuid.uuid4().hex[:12], kind=kind, market=market, mode=mode)
    with _LOCK:
        _JOBS[job.id] = job
        _ORDER.append(job.id)
        # evict the record of any id that aged out of the bounded order deque
        live = set(_ORDER)
        for jid in [j for j in _JOBS if j not in live]:
            _JOBS.pop(jid, None)
    _EXECUTOR.submit(_run, job, fn)
    return job


def _run(job: Job, fn: Callable[["Job"], Any]) -> None:
    with _LOCK:
        job.status = "running"
        job.started_ts = time.time()
        job.message = "started"
    try:
        result = fn(job)
        with _LOCK:
            job.result = result
            job.status = "done"
            job.progress = 1.0
            job.message = "completed"
            job.ended_ts = time.time()
    except Exception as exc:  # noqa: BLE001 - top-level worker boundary
        with _LOCK:
            job.status = "error"
            job.error = f"{type(exc).__name__}: {exc}"
            job.message = job.error
            job.ended_ts = time.time()
            job._logs.append({"ts": time.time(), "line": traceback.format_exc().strip().splitlines()[-1]})


def get(job_id: str) -> Job | None:
    with _LOCK:
        return _JOBS.get(job_id)


def list_jobs() -> list[Job]:
    """All known jobs, newest first."""
    with _LOCK:
        return [_JOBS[j] for j in reversed(_ORDER) if j in _JOBS]
