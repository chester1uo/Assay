"""Agent-native factor research on FactorDB: batch jobs, dedup, novelty, budgets.

Every call returns a small JSON-safe dict with stable keys. Large inputs (hundreds of
expressions) go through :meth:`FactorService.submit`, which answers at once with what it
already knows (rejects with hints, duplicates, factors already scored on this spec) and
queues the rest as a job. Jobs from different owners are interleaved chunk by chunk, so
one agent's 10,000-factor sweep does not block another's 20 factors.

With an ADES task queue (``ades=``) the service is only the controller: slices go to
the queue, remote workers lease and score them (:mod:`assay.ades`), and
:meth:`FactorService.apply_results` records what they send back. Without it, a thread
in this process computes the slices on FactorDB.
"""

from __future__ import annotations

import logging
import json
import queue
import threading
import time
import uuid
from collections import OrderedDict, deque

import numpy as np

from assay import evaluator
from assay.evaluator.metrics import rank_cache, row_orders
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


class SubmissionConflict(ValueError):
    """A client reused an idempotency key with different submission inputs."""


def _f(x, nd=5):
    return None if x is None or not np.isfinite(x) else round(float(x), nd)


def score(values: np.ndarray, fwd: dict[int, np.ndarray]) -> dict:
    """Compact metrics for one factor (the fields agents sort and filter on)."""
    order = row_orders(values)                 # sorted once: every horizon and the lag
    cache = rank_cache(values)                 # ranked once per date's names: likewise
    m = evaluator.evaluate_ic(values, fwd, order=order, xcache=cache)
    h0 = min(fwd)
    groups = evaluator.group_returns(values, fwd[h0], order=order)
    q = groups.get("quintile_returns") if isinstance(groups, dict) else None
    long_short = (q[-1] - q[0]) if q and len(q) >= 2 and q[-1] is not None and q[0] is not None else None
    coverage = float(np.isfinite(values).mean()) if values.size else 0.0
    return {
        "rank_ic": _f(m.get("rank_ic")), "rank_icir": _f(m.get("rank_icir")),
        "ic": _f(m.get("ic")), "icir": _f(m.get("icir")),
        "ic_by_horizon": {int(h): _f(v) for h, v in (m.get("ic_by_horizon") or {}).items()},
        "decay_halflife": _f(evaluator.decay_halflife(m.get("ic_by_horizon") or {}), 2),
        "turnover": _f(evaluator.turnover(values, order=order, cache=cache)), "coverage": _f(coverage, 4),
        "long_short": _f(long_short),
    }


def _sample(values: np.ndarray) -> np.ndarray:
    """The fingerprint days: ``FINGERPRINT_DAYS`` evenly spaced rows."""
    return values[np.linspace(0, len(values) - 1, min(FINGERPRINT_DAYS, len(values))).astype(int)]


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
    def __init__(self, db: FactorDB, registry: Registry, *, daily_budget_s: float = 0.0, ades=None, stores=None,
                 tenant_owner=None, owner_router=None, owner_dispatch_recovery=None, start_background=True):
        from assay.data.markets import MarketStores

        self.db, self.reg, self.ades = db, registry, ades
        self.stores = stores or MarketStores({"US": db.qfv})
        self.daily_budget_s = daily_budget_s
        self.tenant_owner, self.owner_router = tenant_owner, owner_router
        self.owner_dispatch_recovery = owner_dispatch_recovery
        self._queues: "OrderedDict[str, deque]" = OrderedDict()   # owner -> deque of (job_id, exprs)
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._fwd: dict = {}                                      # spec -> (dates, labels, fwd, close)
        self._prints: dict = {}                                   # spec -> {factor_id: ranks on sample days}
        self._spec_data = None                                    # ADES mode: one loaded spec for reads
        self._thread = None
        if start_background:
            self._thread = threading.Thread(target=self._reap if ades is not None else self._run,
                                            name="factordb-jobs", daemon=True)
            self._thread.start()

    def close(self):
        """Stop background maintenance when the controller is replaced."""
        self._stop.set()
        self._wake.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=2)

    # ── submit ───────────────────────────────────────────────────────────────
    def submit(self, owner: str | None, exprs: list[str], *, universe: str, start: str, end: str | None = None,
               horizons=DEFAULT_HORIZONS, execution: str = "next_open", tags=(), parents=(), label: str = "",
               request_id: str | None = None) -> dict:
        if self.tenant_owner is not None:
            owner = self.tenant_owner
        if request_id is not None:
            return self._submit_idempotent(owner, exprs, universe=universe, start=start, end=end,
                                           horizons=horizons, execution=execution, tags=tags,
                                           parents=parents, label=label, request_id=request_id)
        universe = universe.upper()
        store = self.stores.for_universe(universe)
        end = self.stores.end(universe, end)
        from datetime import date

        if date.fromisoformat(start) > date.fromisoformat(end):
            raise ValueError("start must not be later than end")
        if not store.get_universe(universe, end, end):
            raise ValueError(f"no {universe} members available on {end}")
        horizons = sorted({int(h) for h in horizons})
        spec = spec_key(universe, start, end, horizons, execution)
        self.reg.add_spec(spec, universe, start, end, horizons, execution)
        version = store.snapshot_id
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
            job_id = "job_" + uuid.uuid4().hex
            self.reg.create_job(job_id, owner, spec, [e for _, e, _ in accepted], label, version=version)
            self.reg.update_job(job_id, factor_ids=[f for f, _, _ in accepted])
            if self.ades is not None:
                # Private jobs and non-default labels require upgraded workers
                # with lease fencing. Shared next_open keeps legacy compatibility.
                self.ades.enqueue(job_id, owner or "anonymous", spec, version,
                                  [[fid, e] for fid, e, _ in accepted], CHUNK,
                                  idempotent=self.tenant_owner is not None or execution in {'next_close','close'},
                                  stable=True)
            else:
                with self._lock:
                    q = self._queues.setdefault(owner or "anonymous", deque())
                    for i in range(0, len(accepted), CHUNK):
                        q.append((job_id, spec, universe, start, end, horizons, execution, accepted[i:i + CHUNK]))
                self._wake.set()
        return {"job_id": job_id, "spec": spec, "queued": len(accepted), "already_scored": known[:50],
                "n_already_scored": len(known), "duplicates": duplicates[:50], "rejected": rejected[:50],
                "next": "poll fdb_job with job_id" if job_id else "nothing to compute",
                "universe": universe, "data_version": version}

    def _submit_idempotent(self, owner, exprs, *, universe, start, end, horizons, execution, tags, parents,
                           label, request_id):
        """Atomically save a client request and its job before durable dispatch."""
        if self.ades is None:
            raise ValueError('request_id for FactorDB submissions requires ADES')
        if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 128:
            raise ValueError('request_id must be a nonempty string of at most 128 characters')
        owner = owner or 'anonymous'
        universe = str(universe).upper()
        horizons = sorted({int(h) for h in horizons})
        inputs = json.dumps(dict(exprs=list(exprs), universe=universe, start=str(start), end=end,
                                 horizons=horizons, execution=execution, tags=list(tags),
                                 parents=list(parents), label=label), sort_keys=True, allow_nan=False)

        def replay(saved):
            if saved['inputs'] != inputs:
                raise SubmissionConflict('request_id already belongs to different submission inputs')
            self.recover_dispatches()
            return saved['receipt']

        prior = self.reg.generic_submission(owner, request_id)
        if prior is not None:
            return replay(prior)
        store = self.stores.for_universe(universe)
        resolved_end = self.stores.end(universe, end)
        from datetime import date
        if date.fromisoformat(start) > date.fromisoformat(resolved_end):
            raise ValueError('start must not be later than end')
        if not store.get_universe(universe, resolved_end, resolved_end):
            raise ValueError(f'no {universe} members available on {resolved_end}')
        spec = spec_key(universe, start, resolved_end, horizons, execution)
        version = store.snapshot_id
        with self.reg._lock, self.reg._conn() as c:
            c.execute('BEGIN IMMEDIATE')
            row = c.execute('SELECT inputs,receipt FROM generic_submissions WHERE owner=? AND request_id=?',
                            (owner, request_id)).fetchone()
            if row is not None:
                saved = {'inputs': row['inputs'], 'receipt': json.loads(row['receipt'])}
            else:
                c.execute('INSERT OR IGNORE INTO specs VALUES (?,?,?,?,?,?)',
                          (spec, universe, str(start), str(resolved_end), json.dumps(horizons), execution))
                accepted, rejected, known, duplicates, seen = [], [], [], [], {}
                for expr in exprs:
                    try:
                        node = parse(expr)
                        bad = sorted(op for op in _ops(node) if not operators.is_registered(op))
                        if bad:
                            raise ValueError(f'unknown operators {bad}; see fdb_operators')
                        canon = cn.canonicalize(node)
                        fid = cn.factor_id(canon)
                    except Exception as exc:
                        rejected.append({'expr':expr, 'code':'PARSE_ERROR', 'message':str(exc)[:300],
                                         'hint':'check operator names and arity with fdb_operators'})
                        continue
                    if fid in seen:
                        duplicates.append({'expr':expr, 'factor_id':fid, 'same_as':seen[fid]})
                        continue
                    seen[fid] = expr
                    updated = c.execute('UPDATE factors SET submits=submits+1 WHERE id=?', (fid,)).rowcount
                    if not updated:
                        c.execute('INSERT INTO factors (id,canonical,expr,owner,created,tags,parents) VALUES (?,?,?,?,?,?,?)',
                                  (fid,str(canon),expr,owner,time.time(),json.dumps(list(tags)),json.dumps(list(parents))))
                    score_row = c.execute('SELECT * FROM scores WHERE factor_id=? AND spec=? AND version=?',
                                          (fid,spec,version)).fetchone()
                    if score_row is not None:
                        known.append({'factor_id':fid,'expr':expr,'new':False,**_brief(self.reg._score_row(score_row))})
                    else:
                        accepted.append((fid,expr))
                job_id = 'job_' + uuid.uuid4().hex if accepted else None
                if job_id:
                    c.execute("INSERT INTO jobs (id,owner,spec,status,total,created,exprs,label,version,factor_ids) "
                              "VALUES (?,?,?,'queued',?,?,?,?,?,?)",
                              (job_id,owner,spec,len(accepted),time.time(),json.dumps([e for _,e in accepted]),
                               str(label)[:120],version,json.dumps([f for f,_ in accepted])))
                receipt = {'job_id':job_id,'spec':spec,'queued':len(accepted),'already_scored':known[:50],
                           'n_already_scored':len(known),'duplicates':duplicates[:50],'rejected':rejected[:50],
                           'next':'poll fdb_job with job_id' if job_id else 'nothing to compute',
                           'universe':universe,'data_version':version,'request_id':request_id}
                c.execute('INSERT INTO generic_submissions VALUES (?,?,?,?)',
                          (owner,request_id,inputs,json.dumps(receipt,allow_nan=False)))
                saved = {'inputs':inputs,'receipt':receipt}
        return replay(saved)

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
        while not self._stop.is_set():
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
                from assay.data.markets import market_for
                if market_for(universe) != "US":
                    from assay.ades import slice as sl

                    data = sl.load_spec(self.stores.for_universe(universe), self.reg.spec(spec))
                    if data.version != job.get("version"):
                        raise ValueError("data snapshot retired; resubmit against current data")
                    self.apply_results({"job_id": job_id, "spec": spec, "version": data.version, "owner": owner},
                                       sl.score_items(data, [[f, e] for f, e, _ in items]), time.time() - t0)
                    continue
                fwd = self._forward(spec, universe, start, end, horizons, execution)
                _, labels, res, _ = self.db.evaluate_iter([e for _, e, _ in items], universe, start, end)
                order = np.argsort(np.asarray(labels, dtype=object), kind="stable")  # prints on sorted labels
                version = self.db.qfv.snapshot_id
                if job.get("version") and version != job["version"]:
                    raise ValueError("data snapshot retired; resubmit against current data")
                for (fid, _, _), r in zip(items, res):
                    if r.values is None:
                        failed += 1
                        self.reg.put_score(fid, spec, version, owner, {"error": r.error})
                        continue
                    self.reg.put_score(fid, spec, version, owner, score(r.values, fwd[2]))
                    self._remember((spec, version), fid, _ranks(_sample(r.values))[:, order].astype(np.float32))
                    done += 1
            except Exception as exc:  # noqa: BLE001 — a failing slice fails its job, not the worker
                log.exception("factordb job %s failed", job_id)
                self.reg.update_job(job_id, status="failed", error=str(exc)[:500], finished=time.time())
                continue
            self._progress(job_id, owner, done, failed, time.time() - t0)

    def _progress(self, job_id: str, owner: str | None, done: int, failed: int, seconds: float) -> None:
        self.reg.add_usage(owner, seconds, done)
        job = self.reg.job(job_id)
        if job is None:
            return
        fields = {"done": job["done"] + done, "failed": job["failed"] + failed}
        if job["status"] == "queued":
            fields.update(status="running", started=time.time())
        if fields["done"] + fields["failed"] >= job["total"]:
            fields.update(status="done", finished=time.time())
        self.reg.update_job(job_id, **fields)

    # ── ADES controller ──────────────────────────────────────────────────────
    def apply_results(self, task: dict, results: list[dict], seconds: float) -> dict:
        """Record a worker's scores for a completed task (see :mod:`assay.ades`)."""
        from assay.ades.slice import decode_print

        if self.owner_router is not None:
            target = self.owner_router(task['owner'])
            if target is not self:
                return target.apply_results(task, results, seconds)
        if self.tenant_owner is not None and task['owner'] != self.tenant_owner:
            raise ValueError('task belongs to another account')

        applied = self.reg.apply_task_results(task, results, seconds)
        if applied:
            for r in results:
                if r.get("metrics") is not None and r.get("print"):
                    self._remember((task["spec"],task["version"]), r["factor_id"], decode_print(r["print"]))
        return {"done":sum(r.get("metrics") is not None for r in results),
                "failed":sum(r.get("metrics") is None for r in results), "applied":applied}

    def recover_results(self, job_id=None, *, best_effort=False):
        if self.ades is not None:
            for task, results, seconds in self.ades.result_receipts(job_id, owner=self.tenant_owner):
                if self.tenant_owner is not None and task['owner'] != self.tenant_owner:
                    continue
                try:
                    self.apply_results(task, results, seconds)
                except Exception:
                    if not best_effort:
                        raise
                    log.exception('Could not recover ADES result for task %s', task['id'])
            for receipt in self.ades.cancellation_receipts(job_id):
                try:
                    if self.tenant_owner is not None:
                        if self.reg.job(receipt['job_id']) is not None:
                            self.reg.apply_cancellation(receipt)
                    elif self.owner_router is not None:
                        owner = self.ades.job_owner(receipt['job_id'])
                        target = self.owner_router(owner) if owner else self
                        target.reg.apply_cancellation(receipt)
                    else:
                        self.reg.apply_cancellation(receipt)
                except Exception:
                    if not best_effort:
                        raise
                    log.exception('Could not recover ADES cancellation for job %s', receipt['job_id'])

    def lease(self, worker: str, panel: str | list[str], info: dict | None = None) -> dict | None:
        task = self.ades.lease(worker, panel, info, supports_research_leases=(info is None or info.get("lease_token_supported") is True))
        if task is not None:
            try:
                target = self.owner_router(task['owner']) if self.owner_router is not None else self
                spec = target.reg.spec(task["spec"])
                if spec is None:
                    raise ValueError('task registry or specification is unavailable')
                task["spec"] = spec
                job = target.reg.job(task["job_id"])
                if job is not None and job["status"] == "queued":
                    target.reg.update_job(task["job_id"], status="running", started=time.time())
            except Exception:
                self.ades.release(task['task_id'], worker, task.get('lease_token'))
                raise
        return task

    def recover_dispatches(self):
        """Idempotently dispatch a generic job interrupted before queue commit."""
        if self.ades is None:
            return
        for job in self.reg.pending_dispatches():
            if self.ades.job_owner(job['id']) is not None:
                continue
            try:
                spec = self.reg.spec(job['spec'])
                if spec is None or not job['version']:
                    raise ValueError('job specification or data version missing')
                exprs = job['exprs']
                ids = job['factor_ids']
                if len(ids) != len(exprs):
                    ids = [cn.factor_id(cn.canonicalize(parse(expr))) for expr in exprs]
                    self.reg.update_job(job['id'], factor_ids=ids)
                if len(ids) != job['total']:
                    raise ValueError('job factor count is inconsistent')
                self.ades.enqueue(job['id'], job['owner'] or 'anonymous', job['spec'], job['version'],
                                  list(map(list, zip(ids, exprs))), CHUNK,
                                  idempotent=(self.tenant_owner is not None or self.reg.is_idempotent_job(job['id']) or
                                              spec['execution'] in {'next_close', 'close'}),
                                  stable=True)
            except Exception:  # one bad outbox entry must not block other jobs
                log.exception('Could not recover ADES dispatch for job %s', job['id'])

    def _reap(self) -> None:
        while not self._stop.wait(10):
            for operation in (lambda: self.recover_results(best_effort=True), self.recover_dispatches,
                              self.owner_dispatch_recovery or (lambda: None), self.ades.reap,
                              lambda: self.ades.retire_versions(list(self.stores.versions().values())),
                              lambda: self.recover_results(best_effort=True)):
                try:
                    operation()
                except Exception:  # noqa: BLE001
                    log.exception("ades reaper")

    def _forward(self, spec, universe, start, end, horizons, execution):
        key = (spec, self.stores.for_universe(universe).snapshot_id)
        hit = self._fwd.get(key)
        if hit is not None:
            return hit
        dates, labels, _ = self.db.evaluate(["close"], universe, start, end)   # the request's axes
        sids = self.db.panel.label_sids(labels, on=str(dates[-1]))
        m = self.db.panel.matrix(["close", "open"], sids, str(dates[0]), str(dates[-1]), adjust=self.db.adjust)
        # unmasked prices: a security's return after it leaves the universe still counts
        fwd = evaluator.forward_returns(np.asarray(m.data["close"], float), np.asarray(m.data["open"], float),
                                        list(horizons), execution)
        out = (dates, labels, fwd)
        self._fwd[key] = out
        return out

    def _remember(self, spec: str, fid: str, ranks: np.ndarray) -> None:
        """Keep a factor's fingerprint: ranks on the sample days, columns in sorted-label order."""
        self._prints.setdefault(spec, {})[fid] = ranks

    # ── read side ────────────────────────────────────────────────────────────
    def job(self, job_id: str, top: int = 10) -> dict:
        j = self.reg.job(job_id)
        if j is None:
            return {"error": {"code": "NOT_FOUND", "message": f"no job {job_id}"}}
        rows = []
        info = self.reg.spec(j["spec"])
        version = j.get("version") or self.stores.for_universe(info["universe"]).snapshot_id
        for fid, e in zip(j["factor_ids"], j["exprs"]):
            s = self.reg.score(fid, j["spec"], version)
            if s is not None and s.get("rank_icir") is not None:
                rows.append({"factor_id": fid, "expr": e, **_brief(s)})
        rows.sort(key=lambda r: abs(r["rank_icir"] or 0), reverse=True)
        elapsed = (j["finished"] or time.time()) - (j["started"] or j["created"])
        rate = (j["done"] + j["failed"]) / elapsed if elapsed > 0 else 0
        left = j["total"] - j["done"] - j["failed"]
        return {"job_id": job_id, "status": j["status"], "total": j["total"], "done": j["done"], "failed": j["failed"],
                "eta_s": round(left / rate, 1) if rate and left else 0, "error": j["error"], "spec": j["spec"],
                "top": rows[:top], "data_version": version, "universe": info["universe"]}

    def query(self, **kw) -> dict:
        rows = self.reg.query(**kw)
        return {"count": len(rows), "factors": rows}

    def factor(self, factor_id: str) -> dict:
        f = self.reg.factor(factor_id)
        return f or {"error": {"code": "NOT_FOUND", "message": f"no factor {factor_id}"}}

    def novelty(self, expr_or_id: str, *, universe: str, start: str, end: str | None = None,
                horizons=DEFAULT_HORIZONS, execution: str = "next_open", top: int = 5) -> dict:
        universe = universe.upper()
        end = self.stores.end(universe, end)
        spec = spec_key(universe, start, end, sorted({int(h) for h in horizons}), execution)
        version = self.stores.for_universe(universe).snapshot_id
        if self.ades is not None:
            # Worker receipts retain fingerprints durably; rebuild this account's
            # in-memory index after process restart or facade eviction.
            from assay.ades.slice import decode_print
            for task, results, _ in self.ades.result_receipts(owner=self.tenant_owner, spec=spec, version=version):
                if self.owner_router is not None:
                    try:
                        if self.owner_router(task['owner']) is not self:
                            continue
                    except RuntimeError:
                        continue
                for result in results:
                    if (result.get('metrics') is not None and result.get('print') and
                            result['factor_id'] not in self._prints.get((spec, version), {})):
                        self._remember((spec, version), result['factor_id'], decode_print(result['print']))
        prints = dict(self._prints.get((spec, version), {}))
        if not prints:
            return {"spec": spec, "library": 0, "max_abs_corr": None, "nearest": [],
                    "note": "no novelty fingerprints available for this spec and data version"}
        expr = expr_or_id
        f = self.reg.factor(expr_or_id) if not any(c in expr_or_id for c in "(,") else None
        if f:
            expr = f["expr"]
        v, labels, err = self._values_one(expr, spec, universe, start, end)
        if err is not None:
            return {"error": err}
        mine = _ranks(_sample(v))[:, np.argsort(np.asarray(labels, dtype=object), kind="stable")]
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

    def _values_one(self, expr: str, spec: str, universe: str, start: str, end: str):
        """(values, labels, error) of one expression on a spec. In ADES mode the controller
        does not build FactorDB nodes; it evaluates on the classic engine, like a worker."""
        from assay.data.markets import market_for

        if self.ades is None and market_for(universe) == "US":
            _, labels, res = self.db.evaluate([expr], universe, start, end)
            return res[0].values, labels, res[0].error
        from assay.ades import slice as sl

        info = self.reg.spec(spec)
        store = self.stores.for_universe(universe)
        with self._lock:
            if (self._spec_data is None or self._spec_data.key != spec
                    or self._spec_data.version != store.snapshot_id):
                self._spec_data = None
                self._spec_data = sl.load_spec(store, info)
            data = self._spec_data
        try:
            v = data.engine.evaluate(expr).values
        except Exception as exc:  # noqa: BLE001
            return None, None, {"code": "RUNTIME_ERROR", "message": str(exc)[:300]}
        return v, list(data.engine.symbols), None

    def values(self, expr_or_id: str, *, universe: str, date: str | None = None, top: int = 10) -> dict:
        f = self.reg.factor(expr_or_id) if not any(c in expr_or_id for c in "(,") else None
        expr = f["expr"] if f else expr_or_id
        universe = universe.upper()
        end = self.stores.end(universe, date)
        from assay.data.markets import market_for

        if self.ades is not None or market_for(universe) != "US":
            from assay.engine.engine import FactorEngine

            store = self.stores.for_universe(universe)
            span = store.price_span()
            if market_for(universe) == "US":
                calendar = store.panel.calendar
                calendar = calendar[calendar <= np.datetime64(end, "D")]
            else:
                calendar = store._dates(span["first_date"], end, end)
            if not len(calendar):
                raise ValueError(f"no {universe} prices through {end}")
            lookback = cn.lookback(parse(expr))
            start = str(calendar[0 if lookback is None else max(0, len(calendar) - lookback - 2)])
            eng = FactorEngine.from_store(store, universe, (start, end), end, adj="total")
            result = eng.evaluate(expr)
            return self._value_row(eng.dates, list(eng.symbols), result.values, top)
        dates, labels, res = self.db.evaluate([expr], universe, end, end)
        r = res[0]
        if r.values is None:
            return {"error": r.error}
        return self._value_row(dates, labels, r.values, top)

    def markets(self) -> dict:
        versions = self.stores.versions()
        return {"markets": [{"market": market, "data_version": version,
                             "universes": self.stores.stores[market].universes(),
                             "first_date": self.stores.stores[market].price_span()["first_date"],
                             "last_date": self.stores.stores[market].price_span()["last_date"],
                             "node_store": market == "US"}
                            for market, version in versions.items()], "distributed": self.ades is not None}


    @staticmethod
    def _value_row(dates, labels, values, top):
        row = values[-1]
        ok = np.flatnonzero(np.isfinite(row))
        order = ok[np.argsort(row[ok])]
        pick = lambda ix: [{"symbol": labels[i], "value": _f(row[i], 6)} for i in ix]
        return {"date": str(dates[-1]), "n": int(len(ok)), "top": pick(order[::-1][:top]), "bottom": pick(order[:top]),
                "mean": _f(np.nanmean(row)) if len(ok) else None}


    def budget(self, owner: str | None) -> dict:
        """Compute used today; ``limit_seconds`` / ``left_seconds`` are ``None`` when uncapped."""
        if self.tenant_owner is not None:
            owner = self.tenant_owner
        u = self.reg.usage_today(owner)
        if not self.daily_budget_s or self.daily_budget_s <= 0:
            return {**u, "limit_seconds": None, "left_seconds": None}
        return {**u, "limit_seconds": self.daily_budget_s,
                "left_seconds": max(0.0, self.daily_budget_s - u["seconds"])}

    def over_budget(self, owner: str | None) -> bool:
        left = self.budget(owner)["left_seconds"]
        return left is not None and left <= 0

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
