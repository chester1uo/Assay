"""Agent-native factor research on FactorDB: batch jobs, dedup, novelty, budgets.

Every call returns a small JSON-safe dict with stable keys. Large inputs (hundreds of
expressions) go through :meth:`FactorService.submit`, which answers at once with what it
already knows (rejects with hints, duplicates, factors already scored on this spec) and
queues the rest as a job. Jobs from different owners are interleaved chunk by chunk, so
one agent's 10,000-factor sweep does not block another's 20 factors.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
import uuid
from collections import OrderedDict, deque

import numpy as np

from assay import evaluator
from assay.engine import operators
from assay.engine.parsing import parse
from assay.factordb import canonical as cn
from assay.factordb.db import FactorDB
from assay.factordb.registry import Registry, spec_key

log = logging.getLogger("assay.factordb")

__all__ = ["FactorService"]

CHUNK = 64                 # expressions per scheduling slice
FINGERPRINT_DAYS = 60      # sampled days for novelty
DEFAULT_HORIZONS = (1, 5, 10, 20)


def _f(x, nd=5):
    return None if x is None or not np.isfinite(x) else round(float(x), nd)


def score(values: np.ndarray, fwd: dict[int, np.ndarray]) -> dict:
    """Compact metrics for one factor (the fields agents sort and filter on)."""
    m = evaluator.evaluate_ic(values, fwd)
    h0 = min(fwd)
    groups = evaluator.group_returns(values, fwd[h0])
    q = groups.get("quintile_returns") if isinstance(groups, dict) else None
    long_short = (q[-1] - q[0]) if q and len(q) >= 2 and q[-1] is not None and q[0] is not None else None
    coverage = float(np.isfinite(values).mean()) if values.size else 0.0
    return {
        "rank_ic": _f(m.get("rank_ic")), "rank_icir": _f(m.get("rank_icir")),
        "ic": _f(m.get("ic")), "icir": _f(m.get("icir")),
        "ic_by_horizon": {int(h): _f(v) for h, v in (m.get("ic_by_horizon") or {}).items()},
        "decay_halflife": _f(evaluator.decay_halflife(m.get("ic_by_horizon") or {}), 2),
        "turnover": _f(evaluator.turnover(values)), "coverage": _f(coverage, 4),
        "long_short": _f(long_short),
    }


def _ranks(x: np.ndarray) -> np.ndarray:
    """Per-row ranks scaled to [-1, 1], NaN kept (for novelty correlations)."""
    out = np.full(x.shape, np.nan)
    for i, row in enumerate(x):
        ok = np.isfinite(row)
        n = int(ok.sum())
        if n > 2:
            r = np.empty(n)
            r[np.argsort(row[ok], kind="stable")] = np.arange(n)
            out[i, ok] = 2 * r / (n - 1) - 1
    return out


class FactorService:
    def __init__(self, db: FactorDB, registry: Registry, *, daily_budget_s: float = 3600.0):
        self.db, self.reg = db, registry
        self.daily_budget_s = daily_budget_s
        self._queues: "OrderedDict[str, deque]" = OrderedDict()   # owner -> deque of (job_id, exprs)
        self._wake = threading.Event()
        self._lock = threading.Lock()
        self._fwd: dict = {}                                      # spec -> (dates, labels, fwd, close)
        self._prints: dict = {}                                   # spec -> {factor_id: ranks on sample days}
        self._thread = threading.Thread(target=self._run, name="factordb-jobs", daemon=True)
        self._thread.start()

    # ── submit ───────────────────────────────────────────────────────────────
    def submit(self, owner: str | None, exprs: list[str], *, universe: str, start: str, end: str | None = None,
               horizons=DEFAULT_HORIZONS, execution: str = "next_open", tags=(), parents=(), label: str = "") -> dict:
        end = end or str(self.db.grid().end)
        horizons = sorted({int(h) for h in horizons})
        spec = spec_key(universe, start, end, horizons, execution)
        self.reg.add_spec(spec, universe, start, end, horizons, execution)
        version = self.db.qfv.snapshot_id
        accepted, rejected, known, duplicates, seen = [], [], [], [], {}
        for e in exprs:
            try:
                node = parse(e)
                bad = sorted(op for op in _ops(node) if not operators.is_registered(op))
                if bad:
                    raise ValueError(f"unknown operators {bad}; see fdb_operators")
                canon = cn.canonicalize(node)
                fid = cn.factor_id(canon)
            except Exception as exc:  # noqa: BLE001
                rejected.append({"expr": e, "code": "PARSE_ERROR", "message": str(exc)[:300],
                                 "hint": "check operator names and arity with fdb_operators"})
                continue
            if fid in seen:
                duplicates.append({"expr": e, "factor_id": fid, "same_as": seen[fid]})
                continue
            seen[fid] = e
            new = self.reg.add_factor(fid, str(canon), e, owner, tags, parents)
            have = self.reg.score(fid, spec, version)
            if have is not None:
                known.append({"factor_id": fid, "expr": e, "new": False, **_brief(have)})
            else:
                accepted.append((fid, e, new))
        job_id = None
        if accepted:
            job_id = "job_" + uuid.uuid4().hex[:12]
            self.reg.create_job(job_id, owner, spec, [e for _, e, _ in accepted], label)
            self.reg.update_job(job_id, factor_ids=[f for f, _, _ in accepted])
            with self._lock:
                q = self._queues.setdefault(owner or "anonymous", deque())
                for i in range(0, len(accepted), CHUNK):
                    q.append((job_id, spec, universe, start, end, horizons, execution, accepted[i:i + CHUNK]))
            self._wake.set()
        return {"job_id": job_id, "spec": spec, "queued": len(accepted), "already_scored": known[:50],
                "n_already_scored": len(known), "duplicates": duplicates[:50], "rejected": rejected[:50],
                "next": "poll fdb_job with job_id" if job_id else "nothing to compute"}

    # ── worker ───────────────────────────────────────────────────────────────
    def _next_slice(self):
        with self._lock:
            for owner in list(self._queues):
                q = self._queues[owner]
                if q:
                    item = q.popleft()
                    self._queues.move_to_end(owner)     # round-robin across owners
                    return owner, item
                del self._queues[owner]
        return None

    def _run(self) -> None:
        while True:
            nxt = self._next_slice()
            if nxt is None:
                self._wake.wait(5)
                self._wake.clear()
                continue
            owner, (job_id, spec, universe, start, end, horizons, execution, items) = nxt
            job = self.reg.job(job_id)
            if job and job["status"] == "queued":
                self.reg.update_job(job_id, status="running", started=time.time())
            t0 = time.time()
            done = failed = 0
            try:
                fwd = self._forward(spec, universe, start, end, horizons, execution)
                _, _, res = self.db.evaluate([e for _, e, _ in items], universe, start, end)
                version = self.db.qfv.snapshot_id
                for (fid, _, _), r in zip(items, res):
                    if r.values is None:
                        failed += 1
                        self.reg.put_score(fid, spec, version, owner, {"error": r.error})
                        continue
                    self.reg.put_score(fid, spec, version, owner, score(r.values, fwd[2]))
                    self._remember(spec, fid, r.values)
                    done += 1
            except Exception as exc:  # noqa: BLE001 — a failing slice fails its job, not the worker
                log.exception("factordb job %s failed", job_id)
                self.reg.update_job(job_id, status="failed", error=str(exc)[:500], finished=time.time())
                continue
            self.reg.add_usage(owner, time.time() - t0, done)
            job = self.reg.job(job_id)
            nd, nf = job["done"] + done, job["failed"] + failed
            fields = {"done": nd, "failed": nf}
            if nd + nf >= job["total"]:
                fields.update(status="done", finished=time.time())
            self.reg.update_job(job_id, **fields)

    def _forward(self, spec, universe, start, end, horizons, execution):
        hit = self._fwd.get(spec)
        if hit is not None:
            return hit
        dates, labels, _ = self.db.evaluate(["close"], universe, start, end)   # the request's axes
        sids = self.db.panel.sids(labels, on=str(dates[-1]))
        m = self.db.panel.matrix(["close", "open"], sids, str(dates[0]), str(dates[-1]), adjust=self.db.adjust)
        # unmasked prices: a security's return after it leaves the universe still counts
        fwd = evaluator.forward_returns(np.asarray(m.data["close"], float), np.asarray(m.data["open"], float),
                                        list(horizons), execution)
        out = (dates, labels, fwd)
        self._fwd[spec] = out
        return out

    def _remember(self, spec: str, fid: str, values: np.ndarray) -> None:
        idx = np.linspace(0, len(values) - 1, min(FINGERPRINT_DAYS, len(values))).astype(int)
        self._prints.setdefault(spec, {})[fid] = _ranks(values[idx]).astype(np.float32)

    # ── read side ────────────────────────────────────────────────────────────
    def job(self, job_id: str, top: int = 10) -> dict:
        j = self.reg.job(job_id)
        if j is None:
            return {"error": {"code": "NOT_FOUND", "message": f"no job {job_id}"}}
        rows = []
        for fid, e in zip(j["factor_ids"], j["exprs"]):
            s = self.reg.score(fid, j["spec"], self.db.qfv.snapshot_id)
            if s is not None and s.get("rank_icir") is not None:
                rows.append({"factor_id": fid, "expr": e, **_brief(s)})
        rows.sort(key=lambda r: abs(r["rank_icir"] or 0), reverse=True)
        elapsed = (j["finished"] or time.time()) - (j["started"] or j["created"])
        rate = (j["done"] + j["failed"]) / elapsed if elapsed > 0 else 0
        left = j["total"] - j["done"] - j["failed"]
        return {"job_id": job_id, "status": j["status"], "total": j["total"], "done": j["done"], "failed": j["failed"],
                "eta_s": round(left / rate, 1) if rate and left else 0, "error": j["error"], "spec": j["spec"],
                "top": rows[:top]}

    def query(self, **kw) -> dict:
        rows = self.reg.query(**kw)
        return {"count": len(rows), "factors": rows}

    def factor(self, factor_id: str) -> dict:
        f = self.reg.factor(factor_id)
        return f or {"error": {"code": "NOT_FOUND", "message": f"no factor {factor_id}"}}

    def novelty(self, expr_or_id: str, *, universe: str, start: str, end: str | None = None,
                horizons=DEFAULT_HORIZONS, execution: str = "next_open", top: int = 5) -> dict:
        end = end or str(self.db.grid().end)
        spec = spec_key(universe, start, end, sorted({int(h) for h in horizons}), execution)
        prints = self._prints.get(spec, {})
        if not prints:
            return {"spec": spec, "library": 0, "max_abs_corr": None, "nearest": [],
                    "note": "no scored factors on this spec yet"}
        expr = expr_or_id
        f = self.reg.factor(expr_or_id) if not any(c in expr_or_id for c in "(,") else None
        if f:
            expr = f["expr"]
        _, _, res = self.db.evaluate([expr], universe, start, end)
        if res[0].values is None:
            return {"error": res[0].error}
        v = res[0].values
        idx = np.linspace(0, len(v) - 1, min(FINGERPRINT_DAYS, len(v))).astype(int)
        mine = _ranks(v[idx])
        own = cn.factor_id(parse(expr))
        out = []
        for fid, other in prints.items():
            if fid == own:
                continue
            ok = np.isfinite(mine) & np.isfinite(other)
            per_day = []
            for row in range(mine.shape[0]):
                k = ok[row]
                if k.sum() > 10:
                    per_day.append(np.corrcoef(mine[row, k], other[row, k])[0, 1])
            if per_day:
                out.append((float(np.nanmean(per_day)), fid))
        out.sort(key=lambda t: abs(t[0]), reverse=True)
        nearest = [{"factor_id": fid, "corr": round(c, 4), "expr": (self.reg.factor(fid) or {}).get("expr")}
                   for c, fid in out[:top]]
        return {"spec": spec, "library": len(prints), "max_abs_corr": round(abs(out[0][0]), 4) if out else None,
                "nearest": nearest}

    def values(self, expr_or_id: str, *, universe: str, date: str | None = None, top: int = 10) -> dict:
        f = self.reg.factor(expr_or_id) if not any(c in expr_or_id for c in "(,") else None
        expr = f["expr"] if f else expr_or_id
        end = date or str(self.db.grid().end)
        dates, labels, res = self.db.evaluate([expr], universe, end, end)
        r = res[0]
        if r.values is None:
            return {"error": r.error}
        row = r.values[-1]
        ok = np.flatnonzero(np.isfinite(row))
        order = ok[np.argsort(row[ok])]
        pick = lambda ix: [{"symbol": labels[i], "value": _f(row[i], 6)} for i in ix]
        return {"date": str(dates[-1]), "n": int(len(ok)), "top": pick(order[::-1][:top]), "bottom": pick(order[:top]),
                "mean": _f(np.nanmean(row)) if len(ok) else None}

    def budget(self, owner: str | None) -> dict:
        u = self.reg.usage_today(owner)
        return {**u, "limit_seconds": self.daily_budget_s,
                "left_seconds": max(0.0, self.daily_budget_s - u["seconds"])}

    @staticmethod
    def operators() -> dict:
        out = []
        for spec in operators.all_specs():
            s = spec.schema or {}
            out.append({"name": spec.name, "signature": s.get("signature", spec.name), "category": s.get("category"),
                        "args": [spec.min_args, spec.max_args], "description": s.get("description", "")[:160]})
        return {"count": len(out), "operators": sorted(out, key=lambda o: (o["category"] or "", o["name"])),
                "fields": ["open", "high", "low", "close", "volume", "transactions", "dvol", "ret"],
                "notes": ["time-series and element-wise parts are computed once and shared across agents",
                          "cross-sectional operators see only the universe's members on each day"]}


def _brief(s: dict) -> dict:
    return {k: s.get(k) for k in ("rank_ic", "rank_icir", "ic", "turnover", "coverage", "long_short", "decay_halflife")}


def _ops(node) -> set[str]:
    from assay.engine.ast import iter_ops
    return iter_ops(node)
