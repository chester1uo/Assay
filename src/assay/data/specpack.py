"""Spec packs: the inputs of one factor-mining spec, kept on disk and memory-mapped.

Scoring factors on a spec (universe, period) needs the same inputs for every factor:
the OHLCV matrices of every security that was a member during the period, aligned on
trading days and adjusted onto the period's last day; the membership mask; and the
forward returns for each horizon. Gathering them from the daily panel (float32 year
blocks, corporate-action factors) takes ~6 s and ~2 GB at peak for TOP1000 2016+,
in every worker process and again after every restart.

A pack stores those inputs once, in the layout the kernels read (C-contiguous
float64 ``.npy``), and opens them with ``mmap``: loading takes milliseconds, and the
processes on a machine share one copy of the pages instead of holding their own.

::

    <root>/<panel snapshot>/<universe>_<start>_<end>_<adj>/
      meta.json                format, snapshot, shape, fields, build time
      dates.npy symbols.npy    rows (datetime64[D]) and columns (sorted labels)
      members.npy              bool (T, N): in the universe on that date
      open.npy ... volume.npy  float64 (T, N), NaN where there is no bar
      fwd-<execution>-<h>.npy  forward returns, written the first time a horizon is used

A pack belongs to one panel snapshot, so a published panel update is never mixed
with an older pack; building a pack removes the packs of older snapshots. A pack
directory and each forward-return file appear by rename, so readers see a complete
pack or none, and two processes building the same pack both end with the same files.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import time
import uuid
from pathlib import Path

import numpy as np

from assay import evaluator
from assay.engine.engine import FactorEngine

log = logging.getLogger(__name__)

__all__ = ["SpecPack", "pack_root", "open_or_build"]

FORMAT = "assay-spec-pack/1"


def pack_root() -> Path | None:
    """``ASSAY_PACK_DIR``, or ``None`` when packs are switched off."""
    root = os.environ.get("ASSAY_PACK_DIR")
    return Path(root) if root else None


def _safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", str(s))


def _map(path: Path) -> np.ndarray:
    """``path`` mapped read-only, as a plain ndarray view: a ``np.memmap`` would come back
    from ``np.asarray`` / ``np.ascontiguousarray`` as a new view on every call, and the
    evaluator keys its per-array caches (forward-return orders and ranks) by identity."""
    return np.asarray(np.load(path, mmap_mode="r"))


def _save(path: Path, arr: np.ndarray) -> None:
    """Write ``arr`` to ``path`` by rename (no reader sees a partial file)."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}")
    with open(tmp, "wb") as f:
        np.save(f, np.ascontiguousarray(arr))
    os.replace(tmp, path)


class SpecPack:
    """An open pack: field and membership matrices mapped read-only."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.meta = json.loads((self.path / "meta.json").read_text())
        if self.meta.get("format") != FORMAT:
            raise ValueError(f"{self.path}: format {self.meta.get('format')!r}, expected {FORMAT!r}")
        self.dates = np.load(self.path / "dates.npy")
        self.symbols = np.load(self.path / "symbols.npy").astype(object)
        self.members = _map(self.path / "members.npy")
        self.fields = {f: _map(self.path / f"{f}.npy") for f in self.meta["fields"]}

    def engine(self) -> FactorEngine:
        """A matrix engine over the mapped fields, masked to the universe's member dates."""
        eng = FactorEngine.from_matrices(self.dates, self.symbols, self.fields)
        eng.membership = self.members
        eng.snapshot = self.meta["snapshot"]
        return eng

    def fwd(self, horizons, execution: str) -> dict[int, np.ndarray]:
        """Forward returns per horizon (unmasked, as the evaluator expects), computing and
        storing the horizons this pack does not have yet."""
        horizons = sorted({int(h) for h in horizons})
        files = {h: self.path / f"fwd-{_safe(execution)}-{h}.npy" for h in horizons}
        missing = [h for h in horizons if not files[h].exists()]
        if missing:
            open_ = self.fields.get("open")
            new = evaluator.forward_returns(self.fields["close"], open_, missing, execution)
            for h in missing:
                _save(files[h], new[h])
        return {h: _map(files[h]) for h in horizons}


def _build(store, universe: str, start: str, end: str, adj: str, fields: list[str], final: Path) -> None:
    t0 = time.perf_counter()
    eng = FactorEngine.from_store(store, universe, (start, end), end, fields=fields, adj=adj)
    tmp = final.with_name(f".{final.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}")
    tmp.mkdir(parents=True)
    try:
        np.save(tmp / "dates.npy", np.asarray(eng.dates, dtype="datetime64[D]"))
        np.save(tmp / "symbols.npy", np.asarray(eng.symbols, dtype=str))
        members = eng.membership if eng.membership is not None else np.ones(eng._shape, dtype=bool)
        np.save(tmp / "members.npy", np.ascontiguousarray(members, dtype=bool))
        for f in fields:
            np.save(tmp / f"{f}.npy", np.ascontiguousarray(eng._matrix(f), dtype=np.float64))
        (tmp / "meta.json").write_text(json.dumps({
            "format": FORMAT, "snapshot": eng.snapshot, "universe": universe, "start": start, "end": end,
            "adj": adj, "fields": list(fields), "shape": list(eng._shape),
            "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "build_s": round(time.perf_counter() - t0, 2)}))
        try:
            os.rename(tmp, final)
        except OSError:                      # another process published it first
            if not (final / "meta.json").exists():
                raise
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    log.info("spec pack %s built in %.1fs", final, time.perf_counter() - t0)


def _prune(snapshot_dir: Path) -> None:
    """Remove the packs of the same store's older snapshots (open maps stay valid)."""
    store = snapshot_dir.name.rsplit("_", 1)[0]
    for d in snapshot_dir.parent.iterdir():
        if d.is_dir() and d.name.rsplit("_", 1)[0] == store and d.name < snapshot_dir.name:
            shutil.rmtree(d, ignore_errors=True)
            log.info("spec pack: removed %s", d)


def open_or_build(store, universe: str, start: str, end: str, *, fields: list[str], adj: str = "total",
                  root: Path | None = None) -> SpecPack:
    """The pack of this spec on the store's current snapshot, built on first use."""
    root = Path(root) if root is not None else pack_root()
    if root is None:
        raise ValueError("no pack directory (set ASSAY_PACK_DIR)")
    snap = root / _safe(store.snapshot_id)
    final = snap / _safe(f"{universe}_{start}_{end}_{adj}")
    if (final / "meta.json").exists():
        pack = SpecPack(final)
        if set(fields) <= set(pack.meta["fields"]):
            return pack
        shutil.rmtree(final, ignore_errors=True)       # built for fewer fields: rebuild
    snap.mkdir(parents=True, exist_ok=True)
    _build(store, universe, start, end, adj, list(fields), final)
    _prune(snap)
    return SpecPack(final)
