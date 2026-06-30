"""On-disk precompute store for common sub-expressions (engineering-docs §4.3/§5.4).

The companion to :mod:`assay.engine.cse`: once the corpus's hottest sub-expressions
are known, :class:`PrecomputeStore` **materialises each one for every asset** and
keeps the ``(T, N)`` matrix on disk, content-addressed by the subtree's structural
hash *and the panel's fingerprint*. A subsequent batch evaluation then short-circuits
those subtrees — loading the result instead of recomputing it across thousands of
factors.

**Auto-update by history.** The on-disk key folds in
:meth:`assay.engine.FactorEngine.panel_fingerprint`, which changes whenever the
panel's dates / symbols / fields change. So when new history is ingested the old
entries simply stop matching (a miss) and the store is rebuilt for the new panel —
no manual invalidation. Within one panel the entries are stable and shared across
processes.

Storage mirrors the L2 factor cache: sharded ``<key[:2]>/<key>.npy`` files written
atomically (temp file + ``os.replace``), float64 verbatim, best-effort (a corrupt
file reads as a miss). Building is itself CSE-accelerated (it evaluates the winners
through :meth:`FactorEngine.evaluate_many`, so nested shared subtrees compute once).
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from assay.engine.cse import common_subexpressions

__all__ = ["PrecomputeStore", "BoundPrecompute"]

_NAMESPACE = "assay-cse-v1"
_SEP = "\x1f"


class PrecomputeStore:
    """Content-addressed disk store of precomputed sub-expression matrices.

    Parameters
    ----------
    cache_dir:
        Root directory (created if missing). Safe to share across processes and to
        point at a fresh/empty dir.
    """

    def __init__(self, cache_dir: Path | str) -> None:
        self.cache_dir = Path(cache_dir).expanduser()
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    # -- keys / paths ---------------------------------------------------------
    def _key(self, struct_hash: str, fingerprint: str) -> str:
        preimage = _SEP.join((_NAMESPACE, str(struct_hash), str(fingerprint)))
        return hashlib.blake2b(preimage.encode("utf-8"), digest_size=16).hexdigest()

    def _path(self, key: str) -> Path:
        return self.cache_dir / key[:2] / f"{key}.npy"

    # -- get / put ------------------------------------------------------------
    def get(self, struct_hash: str, fingerprint: str) -> np.ndarray | None:
        """Load the precomputed matrix for ``struct_hash`` on ``fingerprint``, or ``None``."""
        path = self._path(self._key(struct_hash, fingerprint))
        if not path.is_file():
            return None
        try:
            return np.asarray(np.load(path, allow_pickle=False), dtype=np.float64)
        except Exception:  # noqa: BLE001 — corrupt/truncated -> treat as a miss
            return None

    def has(self, struct_hash: str, fingerprint: str) -> bool:
        return self._path(self._key(struct_hash, fingerprint)).is_file()

    def put(self, struct_hash: str, fingerprint: str, arr: np.ndarray) -> None:
        """Atomically store the ``(T, N)`` matrix for ``struct_hash`` on ``fingerprint``."""
        matrix = np.ascontiguousarray(np.asarray(arr, dtype=np.float64))
        path = self._path(self._key(struct_hash, fingerprint))
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            with open(tmp, "wb") as fh:
                np.save(fh, matrix, allow_pickle=False)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        except Exception:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise

    def bind(self, fingerprint: str) -> "BoundPrecompute":
        """A panel-bound view with the ``get(h)`` / ``put(h, arr)`` the engine consumes."""
        return BoundPrecompute(self, fingerprint)

    # -- build ----------------------------------------------------------------
    def build(
        self,
        engine,
        exprs,
        *,
        top_k: int = 256,
        min_count: int = 3,
        min_nodes: int = 2,
        fingerprint: str | None = None,
    ) -> dict:
        """Precompute the corpus's hottest sub-expressions for the engine's panel.

        Ranks ``exprs`` by :func:`assay.engine.cse.common_subexpressions`, evaluates
        the top ``top_k`` winners through :meth:`FactorEngine.evaluate_many` (so nested
        shared subtrees compute once), and stores each ``(T, N)`` matrix under its
        structural hash + the panel ``fingerprint`` (default: the engine's own).
        Returns a summary ``{built, skipped, fingerprint, top, est_evals_saved}``.
        """
        fp = fingerprint or engine.panel_fingerprint()
        commons = common_subexpressions(
            exprs, min_count=min_count, min_nodes=min_nodes, top_k=top_k
        )
        if not commons:
            return {"built": 0, "skipped": 0, "fingerprint": fp, "top": [], "est_evals_saved": 0}

        # Fast path: one CSE pass over all winners. If any single sub-expression
        # fails to evaluate on this panel (e.g. a field absent for this universe),
        # fall back to a per-expression pass that skips only the offenders.
        try:
            pairs = list(zip(commons, engine.evaluate_many([c.expr for c in commons])))
        except Exception:  # noqa: BLE001
            pairs = []
            for c in commons:
                try:
                    pairs.append((c, engine.evaluate_many([c.expr])[0]))
                except Exception:  # noqa: BLE001 — skip the un-evaluable subtree
                    continue
        built = skipped = 0
        entries: list[dict] = []
        for c, res in pairs:
            try:
                vals = np.asarray(res.values, dtype=np.float64)
                self.put(c.struct_hash, fp, vals)
                built += 1
                with np.errstate(invalid="ignore"):
                    coverage = float(np.isfinite(vals).mean()) if vals.size else 0.0
                entries.append({
                    "struct_hash": c.struct_hash, "expr": c.expr,
                    "count": c.count, "n_factors": c.n_factors,
                    "n_nodes": c.n_nodes, "score": c.score,
                    "shape": list(vals.shape), "bytes": int(vals.nbytes),
                    "coverage": round(coverage, 4),
                })
            except Exception:  # noqa: BLE001 — a write hiccup must not abort the build
                skipped += 1
        return {
            "built": built, "skipped": skipped, "fingerprint": fp,
            "est_evals_saved": int(sum(c.score for c in commons)),
            "entries": entries,                       # full per-subexpression detail
            "top": [c.to_dict() for c in commons[:20]],
        }

    # -- build manifests (data-validity alignment) ---------------------------
    # A small JSON record per built scope (keyed by ``scope``, e.g. a universe), so
    # the admin surface can show *what the hot cache is valid for* and whether the
    # data has moved on since it was built — without loading any panel.
    def _manifest_dir(self) -> Path:
        d = self.cache_dir / "_manifests"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def record_manifest(self, scope: str, meta: dict) -> None:
        """Persist the build metadata for ``scope`` (overwriting the prior record)."""
        safe = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in str(scope))
        path = self._manifest_dir() / f"{safe}.json"
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps({"scope": scope, **meta}, separators=(",", ":")))
        os.replace(tmp, path)

    def manifest(self, scope: str) -> dict | None:
        safe = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in str(scope))
        path = self._manifest_dir() / f"{safe}.json"
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return None

    def record_entries(self, scope: str, entries: list[dict]) -> None:
        """Persist the full per-sub-expression detail for ``scope`` (a sidecar file).

        Kept separate from the lean :meth:`record_manifest` so listing every scope's
        status stays cheap; the detail is loaded only when someone opens one scope's
        cache contents (:meth:`entries`).
        """
        safe = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in str(scope))
        path = self._manifest_dir() / f"{safe}.entries.json"
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(list(entries or []), separators=(",", ":")))
        os.replace(tmp, path)

    def entries(self, scope: str) -> list[dict]:
        """The recorded per-sub-expression detail for ``scope`` (``[]`` if none)."""
        safe = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in str(scope))
        path = self._manifest_dir() / f"{safe}.entries.json"
        if not path.is_file():
            return []
        try:
            return list(json.loads(path.read_text()))
        except (json.JSONDecodeError, OSError):
            return []

    def manifests(self) -> list[dict]:
        """All recorded build manifests (newest-built first)."""
        out: list[dict] = []
        for p in sorted(self._manifest_dir().glob("*.json")):
            if p.name.endswith(".entries.json"):
                continue  # detail sidecars are lists, not manifests
            try:
                m = json.loads(p.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            if isinstance(m, dict):
                out.append(m)
        out.sort(key=lambda m: m.get("built_at", ""), reverse=True)
        return out

    # -- introspection --------------------------------------------------------
    def stats(self) -> dict:
        """``{entries, bytes}`` footprint of the store on disk."""
        n = nbytes = 0
        for p in self.cache_dir.rglob("*.npy"):
            try:
                nbytes += p.stat().st_size
                n += 1
            except OSError:
                pass
        return {"entries": n, "bytes": nbytes}


@dataclass
class BoundPrecompute:
    """A :class:`PrecomputeStore` bound to one panel fingerprint (engine consumer).

    Exposes the ``get(struct_hash) -> matrix | None`` the engine's CSE evaluator
    calls, with the fingerprint baked in, and counts hits/misses so callers can
    report the acceleration (``hit_rate``).
    """

    store: PrecomputeStore
    fingerprint: str
    hits: int = field(default=0)
    misses: int = field(default=0)

    def get(self, struct_hash: str) -> np.ndarray | None:
        arr = self.store.get(struct_hash, self.fingerprint)
        if arr is None:
            self.misses += 1
        else:
            self.hits += 1
        return arr

    def put(self, struct_hash: str, arr: np.ndarray) -> None:
        self.store.put(struct_hash, self.fingerprint, arr)

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return (self.hits / total) if total else 0.0
