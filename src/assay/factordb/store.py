"""The global grid and the node store.

A :class:`Grid` is every trading day of the store (``start`` to the panel's last day)
× a fixed, sorted set of securities (QFinVerse sids). A node is the float32 value of a
column-separable subtree on that grid, kept as one ``.npy`` file per calendar year so
that appending a day rewrites one small file and a read maps only the years it needs.
Year files are security-major, shape ``(n_sids, days_in_year)``: workers compute
slices of securities, so each one writes contiguous ranges instead of touching every
page of a day-major file, and a universe read pulls one contiguous run per security::

    <root>/grids/<grid_id>/sids.npy, grid.json
    <root>/grids/<grid_id>/nodes/<hash[:2]>/<hash>/meta.json, 2016.npy, 2017.npy, ...

``meta.json`` records the canonical expression, the last day the node is valid for, the
panel version it was computed on, its degree and look-back, size and compute cost.
Writes go to temporary names and are renamed into place; a node is visible to readers
only once its ``meta.json`` exists.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

__all__ = ["Grid", "NodeStore", "FORMAT"]

FORMAT = 2   # 2: security-major year files (part of the grid id, so older stores are not reused)


def _years(dates: np.ndarray) -> np.ndarray:
    return dates.astype("datetime64[Y]").astype(int) + 1970


@dataclass
class Grid:
    """Rows: trading days ``dates``; columns: sorted security ids ``sids``."""

    grid_id: str
    dates: np.ndarray          # datetime64[D], ascending
    sids: np.ndarray           # int32, ascending
    adjust: str = "total"

    @property
    def end(self) -> np.datetime64:
        return self.dates[-1]

    def rows(self, start, end) -> tuple[int, int]:
        lo = int(np.searchsorted(self.dates, np.datetime64(start, "D"), side="left"))
        hi = int(np.searchsorted(self.dates, np.datetime64(end, "D"), side="right"))
        return lo, hi

    def cols(self, sids: np.ndarray) -> np.ndarray:
        """Grid column of each sid, -1 where the sid is not on the grid."""
        sids = np.asarray(sids, dtype=np.int32)
        pos = np.searchsorted(self.sids, sids)
        pos = np.minimum(pos, len(self.sids) - 1)
        return np.where(self.sids[pos] == sids, pos, -1)

    def year_slices(self) -> dict[int, tuple[int, int]]:
        """``{year: (first_row, stop_row)}``."""
        ys = _years(self.dates)
        out = {}
        for y in np.unique(ys):
            idx = np.flatnonzero(ys == y)
            out[int(y)] = (int(idx[0]), int(idx[-1]) + 1)
        return out

    @staticmethod
    def make_id(sids: np.ndarray, start, adjust: str, scope: str = "global") -> str:
        h = hashlib.blake2b(digest_size=8)
        h.update(f"{scope}|{np.datetime64(start, 'D')}|{adjust}|v{FORMAT}|".encode())
        h.update(np.asarray(sids, dtype=np.int32).tobytes())
        return h.hexdigest()


class NodeStore:
    """Grids and their nodes under one root directory."""

    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser()
        (self.root / "grids").mkdir(parents=True, exist_ok=True)

    # ── grids ──────────────────────────────────────────────────────────────
    def _gdir(self, grid_id: str) -> Path:
        return self.root / "grids" / grid_id

    def save_grid(self, grid: Grid, meta: dict) -> None:
        d = self._gdir(grid.grid_id)
        d.mkdir(parents=True, exist_ok=True)
        np.save(d / "sids.npy", grid.sids)
        info = {"grid_id": grid.grid_id, "start": str(grid.dates[0]), "end": str(grid.end),
                "n_sids": int(len(grid.sids)), "adjust": grid.adjust, "format": FORMAT, **meta}
        tmp = d / "grid.json.tmp"
        tmp.write_text(json.dumps(info, indent=1))
        os.replace(tmp, d / "grid.json")

    def grid_info(self, grid_id: str) -> dict | None:
        p = self._gdir(grid_id) / "grid.json"
        return json.loads(p.read_text()) if p.is_file() else None

    def grid_sids(self, grid_id: str) -> np.ndarray | None:
        p = self._gdir(grid_id) / "sids.npy"
        return np.load(p) if p.is_file() else None

    # ── nodes ──────────────────────────────────────────────────────────────
    def node_dir(self, grid_id: str, h: str) -> Path:
        return self._gdir(grid_id) / "nodes" / h[:2] / h

    def meta(self, grid_id: str, h: str) -> dict | None:
        p = self.node_dir(grid_id, h) / "meta.json"
        try:
            return json.loads(p.read_text())
        except (OSError, ValueError):
            return None

    def write_meta(self, grid_id: str, h: str, meta: dict) -> None:
        d = self.node_dir(grid_id, h)
        tmp = d / "meta.json.tmp"
        tmp.write_text(json.dumps(meta))
        os.replace(tmp, d / "meta.json")

    def allocate(self, grid: Grid, h: str, years: list[int] | None = None) -> dict[int, Path]:
        """Create float32 ``(n_sids, days)`` year files for a node; returns their paths.

        Existing files of the node for other years are kept; ``years`` limits which
        years are (re)created (default: all)."""
        d = self.node_dir(grid.grid_id, h)
        d.mkdir(parents=True, exist_ok=True)
        out = {}
        for y, (r0, r1) in grid.year_slices().items():
            if years is not None and y not in years:
                continue
            p = d / f"{y}.npy"
            old = p if p.is_file() else None
            # a fresh node is invisible until its meta.json exists, so it is created in place
            tmp = d / f".{y}.{os.getpid()}.npy" if old is not None else p
            # a fresh node is written in full by the workers, so its file is left sparse
            mm = np.lib.format.open_memmap(tmp, mode="w+", dtype=np.float32, shape=(len(grid.sids), r1 - r0))
            if old is not None:  # keep the days already computed (an append grows the year)
                prev = np.load(old, mmap_mode="r")
                n = min(prev.shape[1], mm.shape[1])
                mm[: prev.shape[0], :n] = prev[:, :n]
                mm[:, n:] = np.nan
            mm.flush()
            del mm
            if tmp != p:
                os.replace(tmp, p)
            out[y] = p
        return out

    def open_years(self, grid: Grid, h: str, mode: str = "r") -> dict[int, np.memmap]:
        d = self.node_dir(grid.grid_id, h)
        return {y: np.load(d / f"{y}.npy", mmap_mode=mode) for y in grid.year_slices() if (d / f"{y}.npy").is_file()}

    def read(self, grid: Grid, h: str, r0: int, r1: int, cols: np.ndarray) -> np.ndarray:
        """float64 ``(r1-r0, len(cols))`` slice of a node, day-major (NaN where a column is -1)."""
        out = np.full((r1 - r0, len(cols)), np.nan)
        ok = cols >= 0
        cidx = cols[ok]
        d = self.node_dir(grid.grid_id, h)
        for y, (y0, y1) in grid.year_slices().items():
            a, b = max(r0, y0), min(r1, y1)
            if a >= b:
                continue
            block = np.load(d / f"{y}.npy", mmap_mode="r")
            out[a - r0:b - r0, ok] = block[cidx, a - y0:b - y0].T
        return out

    def nodes(self, grid_id: str) -> list[dict]:
        base = self._gdir(grid_id) / "nodes"
        out = []
        for p in base.glob("*/*/meta.json") if base.is_dir() else []:
            try:
                out.append(json.loads(p.read_text()))
            except (OSError, ValueError):
                continue
        return out

    def drop(self, grid_id: str, h: str) -> None:
        shutil.rmtree(self.node_dir(grid_id, h), ignore_errors=True)

    def touch(self, grid_id: str, h: str) -> None:
        """Count a hit (best effort; used for eviction and popularity)."""
        m = self.meta(grid_id, h)
        if m is not None:
            m["hits"] = int(m.get("hits", 0)) + 1
            m["last_used"] = time.time()
            try:
                self.write_meta(grid_id, h, m)
            except OSError:
                pass
