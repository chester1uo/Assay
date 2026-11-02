"""Compute grid nodes in parallel, one slice of securities per worker.

Every node in a batch is column-separable, so the value of column *j* depends only on
column *j* of the inputs. The parent allocates each node's year files; each worker
reads the input fields for its columns from the QFinVerse panel (memory-mapped year
blocks, adjusted onto the grid's last day), evaluates **all** nodes of the batch for
those columns with one shared CSE memo, and writes its column slice straight into the
nodes' memory-mapped files. Nothing large crosses process boundaries.

Appends reuse the same path with a row window: only rows from ``row_start`` on are
written, and inputs start ``lookback`` rows earlier.

The column slice per task is sized to fit ``ASSAY_FACTORDB_WORKER_MB`` (default 1024)
per worker (:func:`column_chunk`). A worker that dies (e.g. OOM-killed) fails the batch instead of hanging it.
"""

from __future__ import annotations

import logging
import multiprocessing as mp
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor

import numpy as np

from assay.engine import FactorEngine
from assay.engine.ast import OpNode, hash_tree, iter_fields
from assay.factordb.store import Grid, NodeStore

log = logging.getLogger("assay.factordb")

__all__ = ["materialize", "default_workers", "column_chunk", "peak_live"]

# set in the parent before forking; workers inherit them (fork) or rebuild them (spawn)
_JOB: dict = {}


def default_workers() -> int:
    return max(1, min(32, (os.cpu_count() or 2) - 1))


def peak_live(nodes: list) -> int:
    """Most subtree matrices alive at once while ``evaluate_iter`` runs the batch.

    Replays the engine's walk (children first, a matrix freed after its last use) on
    hashes only, so it costs nothing and matches the engine's actual peak."""
    from assay.engine.engine import FactorEngine as _FE

    hashes: dict[int, str] = {}
    for n in nodes:
        hash_tree(n, hashes)
    refs = _FE._use_counts(nodes, hashes)
    live: set[str] = set()
    peak = 0

    def walk(n):
        nonlocal peak
        if not isinstance(n, OpNode):
            return
        h = hashes[id(n)]
        if h in live:
            return
        for c in n.children:
            walk(c)
        live.add(h)
        peak = max(peak, len(live) + 1)          # +1: the operator's own output buffer
        for c in n.children:
            if isinstance(c, OpNode):
                ch = hashes[id(c)]
                refs[ch] -= 1
                if refs[ch] <= 0:
                    live.discard(ch)

    for n in nodes:
        walk(n)
        if isinstance(n, OpNode):
            h = hashes[id(n)]
            refs[h] -= 1
            if refs[h] <= 0:
                live.discard(h)
    return peak


def column_chunk(nodes: list, rows: int, mem_mb: float | None = None, cap: int = 512) -> int:
    """Columns per task so that one worker's peak memory fits in ``mem_mb``.

    The engine frees a subtree after its last use and the window kernels work in
    bounded blocks (``ASSAY_WINDOW_BLOCK_MB``), so the peak is the live frontier of
    the batch DAG (:func:`peak_live`) plus the input fields, a few temporaries each."""
    mem_mb = mem_mb or float(os.environ.get("ASSAY_FACTORDB_WORKER_MB") or 1024)
    from assay.engine.operators._base import WINDOW_BLOCK_MB

    fields = len(set().union(*(iter_fields(n) for n in nodes))) if nodes else 0
    per_col = rows * 8 * (2 * peak_live(nodes) + fields + 4)
    budget = (mem_mb - 4 * WINDOW_BLOCK_MB) * 2**20
    return int(max(8, min(cap, budget // per_col)))


def _panel():
    p = _JOB.get("panel")
    if p is None:
        from assay.data.qfv_panel import DailyPanel
        p = _JOB["panel"] = DailyPanel(_JOB["panel_root"])
    return p


def _work(task) -> tuple[int, float]:
    """Evaluate every node of the batch for columns [c0, c1) and rows [row_start, end)."""
    c0, c1 = task
    t0 = time.perf_counter()
    grid: Grid = _JOB["grid"]
    store: NodeStore = _JOB["store"]
    nodes, hashes = _JOB["nodes"], _JOB["hashes"]
    row_start, in_start = _JOB["row_start"], _JOB["in_start"]
    fields = sorted(set().union(*(iter_fields(n) for n in nodes)))
    sids = grid.sids[c0:c1]
    m = _panel().matrix(fields, sids, str(grid.dates[in_start]), str(grid.end), adjust=grid.adjust)
    if len(m.dates) != len(grid.dates) - in_start:     # calendar mismatch: never write misaligned rows
        raise RuntimeError(f"panel rows {len(m.dates)} != grid rows {len(grid.dates) - in_start}")
    eng = FactorEngine.from_matrices(m.dates, sids, m.data)
    skip = row_start - in_start
    years = grid.year_slices()
    for h, res in zip(hashes, eng.evaluate_iter(nodes)):   # one node in memory at a time
        vals = res.values[skip:].astype(np.float32)
        for y, (y0, y1) in years.items():
            a, b = max(row_start, y0), y1
            if a >= b:
                continue
            mm = np.load(store.node_dir(grid.grid_id, h) / f"{y}.npy", mmap_mode="r+")
            mm[c0:c1, a - y0:b - y0] = vals[a - row_start:b - row_start].T   # security-major
            mm.flush()
            del mm
    return c1 - c0, time.perf_counter() - t0


def materialize(store: NodeStore, grid: Grid, panel_root: str, nodes: list, hashes: list[str], *,
                row_start: int = 0, lookback: int = 0, workers: int | None = None,
                chunk: int | None = None, panel=None) -> dict:
    """Compute ``nodes`` (column-separable, canonical) into the store for rows ≥ ``row_start``.

    ``lookback`` extra rows before ``row_start`` are read as inputs (for an append).
    Returns timing stats. Node year files must already be allocated."""
    if not nodes:
        return {"nodes": 0, "seconds": 0.0}
    t0 = time.perf_counter()
    in_start = max(0, row_start - lookback)
    _JOB.clear()
    _JOB.update(grid=grid, store=store, nodes=nodes, hashes=hashes, row_start=row_start,
                in_start=in_start, panel_root=str(panel_root), panel=panel)
    n = len(grid.sids)
    chunk = chunk or column_chunk(nodes, len(grid.dates) - in_start)
    tasks = [(c, min(c + chunk, n)) for c in range(0, n, chunk)]
    workers = workers or default_workers()
    if workers <= 1 or len(tasks) == 1:
        done = [_work(t) for t in tasks]
    else:
        method = "fork" if sys.platform.startswith("linux") else "spawn"
        if method == "spawn":
            _JOB["panel"] = None  # not picklable; workers reopen it
        ctx = mp.get_context(method)
        with ProcessPoolExecutor(min(workers, len(tasks)), mp_context=ctx, initializer=_init,
                                 initargs=(dict(_JOB),)) as pool:
            done = list(pool.map(_work, tasks))
    secs = time.perf_counter() - t0
    cpu = sum(d[1] for d in done)
    return {"nodes": len(nodes), "columns": n, "chunk": chunk, "rows": len(grid.dates) - row_start, "tasks": len(tasks),
            "workers": min(workers, len(tasks)), "seconds": round(secs, 3), "cpu_seconds": round(cpu, 3)}


def _init(job: dict) -> None:
    _JOB.clear()
    _JOB.update(job)
