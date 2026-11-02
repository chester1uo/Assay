"""FactorDB: factor values and scores served from a global grid of stored nodes.

    fdb = FactorDB(root="/var/lib/assay/factordb", panel_root="/srv/qfinverse/data/daily_panel")
    out = fdb.evaluate(["cs_rank(ts_corr(close, volume, 20))", ...], "TOP1000", "2016-01-01", "2026-10-30")

A request is answered in three steps:

1. **plan** — parse and canonicalise every expression; collect the frontier nodes
   (maximal column-separable subtrees) of the whole batch, shared ones once;
2. **ensure** — compute the frontier nodes that are missing or behind the panel, in
   parallel over security slices (:mod:`.compute`), and keep them;
3. **finish** — read the frontier nodes for the universe's securities and the period,
   put them on the period end's adjustment basis, then run the cross-sectional part of
   each expression on the universe's members of each day, and score.

Cross-sectional operators see only the securities that were universe members on that
day (non-members are masked before the operator runs).
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from assay.engine import operators
from assay.engine.ast import FieldNode, LitNode, OpNode, hash_tree, iter_fields
from assay.engine.engine import EvalContext, EvaluationError, FactorEngine
from assay.engine.parsing import parse
from assay.factordb import canonical as cn
from assay.factordb.compute import default_workers, materialize
from assay.factordb.store import Grid, NodeStore

log = logging.getLogger("assay.factordb")

__all__ = ["FactorDB", "Evaluated"]

DOMAIN = ("TOP3000", "SP500", "NASDAQ100")


@dataclass
class Evaluated:
    """One expression's result on a request (``values`` is ``None`` when it failed)."""

    expr: str
    factor_id: str | None
    values: np.ndarray | None = None
    error: dict | None = None
    from_store: bool = False


@dataclass
class _Plan:
    canon: list = field(default_factory=list)       # canonical trees (None if parse failed)
    errors: dict = field(default_factory=dict)      # index -> error dict
    frontier: dict = field(default_factory=dict)    # hash -> canonical subtree


class FactorDB:
    """A factor store on one QFinVerse daily panel."""

    def __init__(self, root: str | Path, panel_root: str | Path, *, start: str = "2010-01-01",
                 domain: tuple[str, ...] = DOMAIN, adjust: str = "total", workers: int | None = None,
                 store=None):
        from assay.data.store.qfv_store import QFVPanelStore

        self.root = Path(root).expanduser()
        self.panel_root = Path(panel_root)
        self.qfv = store or QFVPanelStore(self.panel_root)
        self.nodes = NodeStore(self.root)
        self.start, self.domain, self.adjust = start, tuple(domain), adjust
        self.workers = workers or default_workers()
        self._grid: Grid | None = None
        self._lock = threading.RLock()
        self._building: dict[str, threading.Event] = {}   # node hash -> done (in-flight dedup)

    @property
    def panel(self):
        return self.qfv.panel

    # ── grid ─────────────────────────────────────────────────────────────────
    def grid(self) -> Grid:
        """The global grid on the current panel (built once; grows with the calendar)."""
        with self._lock:
            self.qfv.refresh()
            cal = self.panel.calendar
            cal = cal[cal >= np.datetime64(self.start, "D")]
            if self._grid is not None and self._grid.dates[-1] == cal[-1]:
                return self._grid
            end = str(cal[-1])
            sids = set()
            for u in self.domain:
                labels = self.qfv.universe_span(u, self.start, end)
                s = self.panel.sids(labels, on=end)
                sids.update(int(x) for x in s[s >= 0])
            sids = np.array(sorted(sids), dtype=np.int32)
            if self._grid is not None and np.isin(sids, self._grid.sids).all():
                sids = self._grid.sids      # securities only join a grid when it is rebuilt
            gid = Grid.make_id(sids, self.start, self.adjust)
            old = self.nodes.grid_sids(gid)
            if old is not None:
                sids = old
            self._grid = Grid(gid, cal, sids, self.adjust)
            self.nodes.save_grid(self._grid, {"domain": list(self.domain), "panel": self.qfv.snapshot_id})
            return self._grid

    # ── plan ─────────────────────────────────────────────────────────────────
    def plan(self, exprs) -> _Plan:
        p = _Plan()
        for i, e in enumerate(exprs):
            try:
                node = parse(e) if isinstance(e, str) else e
                if not iter_fields(node):
                    raise ValueError("the expression references no data field")
                unknown = [op for op in _ops(node) if not operators.is_registered(op)]
                if unknown:
                    raise ValueError(f"unknown operators: {unknown}")
                c = cn.canonicalize(node)
            except Exception as exc:  # noqa: BLE001 — a bad expression never sinks the batch
                p.canon.append(None)
                p.errors[i] = {"code": "PARSE_ERROR", "message": str(exc)}
                continue
            p.canon.append(c)
            hashes = hash_tree(c)
            for f in cn.frontier(c):
                p.frontier.setdefault(hashes[id(f)], f)
        return p

    # ── ensure ───────────────────────────────────────────────────────────────
    def ensure(self, frontier: dict) -> dict:
        """Compute missing / stale frontier nodes; returns ``{"computed": n, ...}``."""
        grid = self.grid()
        end = str(grid.end)
        fresh, stale, waits = [], [], []
        with self._lock:
            for h, node in frontier.items():
                m = self.nodes.meta(grid.grid_id, h)
                if m is not None and m.get("valid_through") == end:
                    continue
                ev = self._building.get(h)
                if ev is not None:
                    waits.append(ev)
                    continue
                self._building[h] = threading.Event()
                (stale if m is not None else fresh).append((h, node, m))
        stats = {"computed": 0, "appended": 0, "reused": len(frontier) - len(fresh) - len(stale) - len(waits)}
        try:
            if fresh:
                stats["compute"] = self._compute([(h, n) for h, n, _ in fresh], grid, row_start=0)
                stats["computed"] = len(fresh)
            for h, node, m in stale:          # appends: few rows, grouped by look-back
                self._append(h, node, m, grid)
                stats["appended"] += 1
        finally:
            with self._lock:
                for h, _, _ in fresh + stale:
                    self._building.pop(h).set()
        for ev in waits:
            ev.wait()
        return stats

    def _compute(self, items, grid: Grid, row_start: int, lookback: int = 0, years=None) -> dict:
        t0 = time.time()
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(16) as pool:      # file creation is I/O-bound
            list(pool.map(lambda it: self.nodes.allocate(grid, it[0], years), items))
        out = materialize(self.nodes, grid, str(self.panel_root), [n for _, n in items], [h for h, _ in items],
                          row_start=row_start, lookback=lookback, workers=self.workers, panel=self.panel)
        per = (time.time() - t0) / max(len(items), 1)
        for h, node in items:
            self.nodes.write_meta(grid.grid_id, h, {
                "hash": h, "expr": str(node), "grid": grid.grid_id, "valid_through": str(grid.end),
                "panel": self.qfv.snapshot_id, "degree": cn.degree(node), "lookback": cn.lookback(node),
                "cost_s": round(per, 3), "created": time.time(), "hits": 0,
                "bytes": int(len(grid.dates) * len(grid.sids) * 4)})
        return out

    def _append(self, h: str, node, meta: dict, grid: Grid) -> None:
        """Bring a node from ``meta['valid_through']`` to the grid's end."""
        old_end = np.datetime64(meta["valid_through"], "D")
        lb = cn.lookback(node)
        changed = self._event_sids(old_end, grid.end)
        if lb is None or changed is None:
            self._compute([(h, node)], grid, row_start=0)
            return
        row_start = int(np.searchsorted(grid.dates, old_end, side="right"))
        years = sorted({int(str(d)[:4]) for d in grid.dates[row_start:]})
        self._compute([(h, node)], grid, row_start=row_start, lookback=lb, years=years)
        if len(changed):   # a new split or dividend restates those securities' adjusted history
            cols = grid.cols(changed)
            cols = cols[cols >= 0]
            if len(cols):
                self._recompute_columns(h, node, grid, cols)

    def _recompute_columns(self, h: str, node, grid: Grid, cols: np.ndarray) -> None:
        from assay.engine import FactorEngine as _FE
        sids = grid.sids[cols]
        m = self.panel.matrix(sorted(iter_fields(node)), sids, str(grid.dates[0]), str(grid.end), adjust=grid.adjust)
        vals = _FE.from_matrices(m.dates, sids, m.data).evaluate_many([node])[0].values.astype(np.float32)
        for y, (y0, y1) in grid.year_slices().items():
            mm = np.load(self.nodes.node_dir(grid.grid_id, h) / f"{y}.npy", mmap_mode="r+")
            mm[:, cols] = vals[y0:y1]
            mm.flush()
            del mm

    def _event_sids(self, after, through) -> np.ndarray | None:
        ev = self.panel.events()
        kinds = ("split",) if self.adjust == "split" else ("split", "dividend")
        m = ev["applied"] & np.isin(ev["kind"], kinds) & (ev["ex_date"] > after) & (ev["ex_date"] <= through)
        return np.unique(ev["sid"][m]).astype(np.int32)

    # ── finish ───────────────────────────────────────────────────────────────
    def evaluate(self, exprs, universe: str, start: str, end: str | None = None, *,
                 with_stats: bool = False):
        """Factor values for ``exprs`` on ``universe`` over [start, end].

        Returns ``(dates, labels, [Evaluated])`` (plus a stats dict with ``with_stats``)."""
        exprs = list(exprs)
        t0 = time.perf_counter()
        plan = self.plan(exprs)
        stats = self.ensure(plan.frontier)
        grid = self.grid()
        end = min(np.datetime64(end or str(grid.end), "D"), grid.end)
        r0, r1 = grid.rows(start, end)
        if r1 <= r0:
            raise ValueError(f"no trading days in [{start}, {end}]")
        dates = grid.dates[r0:r1]
        labels = sorted(self.qfv.universe_span(universe, str(dates[0]), str(dates[-1])))
        sids = self.panel.sids(labels, on=str(dates[-1]))
        keep = sids >= 0
        labels = [l for l, k in zip(labels, keep) if k]
        sids = sids[keep]
        order = np.argsort(sids, kind="stable")             # engine axes: sorted sids
        sids, labels = sids[order], [labels[i] for i in order]
        member = self.qfv.get_membership(universe, dates, labels)
        cols = grid.cols(sids)
        scale = self._rescale(sids, dates[-1], grid) if dates[-1] < grid.end else None
        ctx = _Finish(self, grid, r0, r1, dates, sids, cols, member, scale)
        out = []
        for i, (e, c) in enumerate(zip(exprs, plan.canon)):
            text = e if isinstance(e, str) else str(e)
            if c is None:
                out.append(Evaluated(text, None, error=plan.errors[i]))
                continue
            fid = hash_tree(c)[id(c)]
            try:
                vals = ctx.run(c)
                out.append(Evaluated(text, fid, np.where(member, vals, np.nan), from_store=True))
            except EvaluationError as exc:
                out.append(Evaluated(text, fid, error={"code": "RUNTIME_ERROR", "operator": exc.op,
                                                       "message": exc.message}))
            except Exception as exc:  # noqa: BLE001
                out.append(Evaluated(text, fid, error={"code": "RUNTIME_ERROR", "message": str(exc)}))
        stats.update(seconds=round(time.perf_counter() - t0, 3), rows=int(r1 - r0), columns=int(len(sids)),
                     frontier=len(plan.frontier))
        return (dates, labels, out, stats) if with_stats else (dates, labels, out)

    def _rescale(self, sids: np.ndarray, end, grid: Grid) -> tuple[np.ndarray, np.ndarray]:
        """(P, V): per-security price / volume factors of the corporate actions in (end, grid end]."""
        kinds = ("split",) if self.adjust == "split" else ("split", "dividend")
        pf, vf = self.panel._back_factors(np.array([end, grid.end], dtype="datetime64[D]"), sids, kinds)
        return pf[0], vf[0]


class _Finish:
    """Runs the cross-sectional part of an expression on one request's slice."""

    def __init__(self, db: FactorDB, grid: Grid, r0, r1, dates, sids, cols, member, scale):
        self.db, self.grid, self.r0, self.r1 = db, grid, r0, r1
        self.dates, self.sids, self.cols, self.member, self.scale = dates, sids, cols, member, scale
        self.cache: dict[str, np.ndarray] = {}
        self.fields: dict[str, np.ndarray] = {}
        self.ctx = EvalContext(dates.tolist(), sids.tolist(), {}, {})

    def field(self, name: str) -> np.ndarray:
        if name not in self.fields:
            m = self.db.panel.matrix([name], self.sids, str(self.dates[0]), str(self.dates[-1]), adjust=self.grid.adjust)
            self.fields[name] = np.asarray(m.data[name], dtype=np.float64)
        return self.fields[name]

    def node(self, h: str, node) -> np.ndarray:
        if h in self.cache:
            return self.cache[h]
        deg = cn.degree(node)
        if self.scale is not None and deg is None:
            # not homogeneous: compute on the request window directly (the classic path)
            mats = {f: self.field(f) for f in iter_fields(node)}
            vals = FactorEngine.from_matrices(self.dates, self.sids, mats).evaluate_many([node])[0].values
        else:
            vals = self.db.nodes.read(self.grid, h, self.r0, self.r1, self.cols)
            if self.scale is not None and deg != (0, 0):
                p, v = self.scale
                vals = vals / (p ** deg[0] * v ** deg[1])
        self.cache[h] = vals
        return vals

    def run(self, root) -> np.ndarray:
        hashes = hash_tree(root)
        front = {hashes[id(f)] for f in cn.frontier(root)}

        def ev(n):
            if isinstance(n, LitNode):
                return n.value
            if isinstance(n, FieldNode):
                return self.field(n.name)
            h = hashes[id(n)]
            if h in front:
                return self.node(h, n)
            if h in self.cache:
                return self.cache[h]
            spec = operators.get(n.op)
            args = [ev(c) for c in n.children]
            if cn._row_coupled(n.op):   # the cross-section is the day's members only
                args = [np.where(self.member, a, np.nan) if isinstance(a, np.ndarray) else a for a in args]
            try:
                val = spec.fn(*args, ctx=self.ctx) if spec.needs_ctx else spec.fn(*args)
            except Exception as exc:
                raise EvaluationError(str(exc), op=n.op, cause=type(exc).__name__) from exc
            val = np.full(self.member.shape, float(val)) if np.ndim(val) == 0 else np.asarray(val, dtype=np.float64)
            self.cache[h] = val
            return val

        out = ev(root)
        return np.full(self.member.shape, float(out)) if np.ndim(out) == 0 else out


def _ops(node) -> set[str]:
    from assay.engine.ast import iter_ops
    return iter_ops(node)
