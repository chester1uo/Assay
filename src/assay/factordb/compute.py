"""Compute grid nodes in parallel, one slice of securities per worker.

Every node in a batch is column-separable, so the value of column *j* depends only on
column *j* of the inputs. The parent allocates each node's year files; each worker
reads the input fields for its columns from the QFinVerse panel (memory-mapped year
blocks, adjusted onto the grid's last day), evaluates **all** nodes of the batch for
those columns with one shared CSE memo, and writes its column slice straight into the
nodes' memory-mapped files. Nothing large crosses process boundaries.

Appends reuse the same path with a row window: only rows from ``row_start`` on are
written, and inputs start ``lookback`` rows earlier.
"""

from __future__ import annotations

import logging
import multiprocessing as mp
import os
import sys
import time

import numpy as np

from assay.engine import FactorEngine
from assay.engine.ast import iter_fields
from assay.factordb.store import Grid, NodeStore

log = logging.getLogger("assay.factordb")

__all__ = ["materialize", "default_workers"]

# set in the parent before forking; workers inherit them (fork) or rebuild them (spawn)
_JOB: dict = {}


def default_workers() -> int:
    return max(1, min(32, (os.cpu_count() or 2) - 1))


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
    results = eng.evaluate_many(nodes)
    skip = row_start - in_start
    years = grid.year_slices()
    for h, res in zip(hashes, results):
        vals = res.values[skip:].astype(np.float32)
        for y, (y0, y1) in years.items():
            a, b = max(row_start, y0), y1
            if a >= b:
                continue
            mm = np.load(store.node_dir(grid.grid_id, h) / f"{y}.npy", mmap_mode="r+")
            mm[a - y0:b - y0, c0:c1] = vals[a - row_start:b - row_start]
            mm.flush()
            del mm
    return c1 - c0, time.perf_counter() - t0


def materialize(store: NodeStore, grid: Grid, panel_root: str, nodes: list, hashes: list[str], *,
                row_start: int = 0, lookback: int = 0, workers: int | None = None,
                chunk: int = 256, panel=None) -> dict:
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
    tasks = [(c, min(c + chunk, n)) for c in range(0, n, chunk)]
    workers = workers or default_workers()
    if workers <= 1 or len(tasks) == 1:
        done = [_work(t) for t in tasks]
    else:
        method = "fork" if sys.platform.startswith("linux") else "spawn"
        if method == "spawn":
            _JOB["panel"] = None  # not picklable; workers reopen it
        ctx = mp.get_context(method)
        with ctx.Pool(min(workers, len(tasks)), initializer=_init, initargs=(dict(_JOB),)) as pool:
            done = pool.map(_work, tasks, chunksize=1)
    secs = time.perf_counter() - t0
    cpu = sum(d[1] for d in done)
    return {"nodes": len(nodes), "columns": n, "rows": len(grid.dates) - row_start, "tasks": len(tasks),
            "workers": min(workers, len(tasks)), "seconds": round(secs, 3), "cpu_seconds": round(cpu, 3)}


def _init(job: dict) -> None:
    _JOB.clear()
    _JOB.update(job)
