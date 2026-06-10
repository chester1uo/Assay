"""L2 factor-result cache: complete ``(T, N)`` factor matrices on disk.

Engineering-docs section 5.4 ("L2 Factor String Cache"). L2 caches the *complete*
result of a factor expression keyed by everything that makes the result unique —
the canonicalised expression plus the universe, date range, adjustment mode and
market. Because the key folds in ``adj`` (the adjustment version), a split/dividend
restatement that bumps the adjustment changes the key and so transparently
invalidates stale entries (engineering-docs §5.5).

It is a *cross-session* cache: an expression evaluated in a previous session on the
same universe/period/adj is served from disk without recomputation.

Implementation choices (constrained to numpy / polars / stdlib):

* **Key** — a hex digest. The cache key is the BLAKE2b hash of a canonical,
  delimiter-joined tuple of the inputs, so it is stable across processes and has no
  ordering ambiguity. Returned as ``<digest>`` (32 hex chars); the on-disk layout
  shards by the first two characters (``<digest[:2]>/<digest>.npy``) as in the doc's
  ``factor_cache/<hash[:2]>/<hash>`` scheme — this keeps any single directory from
  growing unbounded.
* **Storage** — ``numpy.save`` / ``numpy.load`` (``.npy``). The docs reference
  Parquet+zstd; that would force a polars round-trip and lose the ``(T, N)`` matrix
  shape. ``.npy`` stores the float64 matrix verbatim with O(1) load and no schema
  juggling, which is the correctness-first choice for this layer.
* **Robustness** — a corrupt / truncated / partially written file is treated as a
  *miss*, never an error: ``get`` returns ``None`` and unlinks the bad file so the
  next ``put`` can heal it. Writes are atomic (temp file + ``os.replace``) so a
  crash mid-write cannot leave a reader-visible corrupt entry.

The clock is never read at import or in the key — keys are pure functions of their
inputs.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

# Unit/record separators: bytes that cannot appear in any textual key component,
# so the joined pre-image is unambiguous (no escaping needed).
_SEP = "\x1f"  # ASCII unit separator between fields
_DIGEST_BYTES = 16  # 128-bit digest -> 32 hex chars (matches SHA-256[:16]-style ids)


@dataclass
class L2FactorCache:
    """On-disk content-addressed cache of ``(T, N)`` factor-result matrices.

    Parameters
    ----------
    cache_dir:
        Root directory for the cache. Created (with parents) if missing.

    The cache is keyed by :meth:`key`; values are float64 ``(T, N)`` numpy arrays.
    All operations are best-effort and never raise on a corrupt store — a bad
    entry simply reads as a miss.
    """

    cache_dir: Path
    _hits: int = field(default=0, init=False)
    _misses: int = field(default=0, init=False)
    _writes: int = field(default=0, init=False)
    _corrupt: int = field(default=0, init=False)

    def __init__(self, cache_dir: Path | str):
        self.cache_dir = Path(cache_dir).expanduser()
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._hits = 0
        self._misses = 0
        self._writes = 0
        self._corrupt = 0

    # -- key derivation -------------------------------------------------------
    def key(
        self,
        expr_canonical: str,
        universe: str,
        period: tuple[str, str],
        adj: str,
        market: str,
    ) -> str:
        """Derive the content key for a factor result.

        ``period`` is an inclusive ``(start, end)`` date-string pair. ``adj`` doubles
        as the adjustment version (engineering-docs §5.4/5.5): bumping it on a split
        or restatement changes the key and so invalidates stale results. The result
        is a 32-char hex digest, stable across processes and Python runs.
        """
        start, end = period
        preimage = _SEP.join(
            (
                "assay-l2-v1",  # namespace tag: lets the key format evolve safely
                str(expr_canonical),
                str(universe),
                str(start),
                str(end),
                str(adj),
                str(market),
            )
        )
        return hashlib.blake2b(preimage.encode("utf-8"), digest_size=_DIGEST_BYTES).hexdigest()

    def _path_for(self, key: str) -> Path:
        """Sharded on-disk path: ``<cache_dir>/<key[:2]>/<key>.npy``."""
        return self.cache_dir / key[:2] / f"{key}.npy"

    # -- get / put ------------------------------------------------------------
    def get(self, key: str) -> np.ndarray | None:
        """Return the cached matrix for ``key``, or ``None`` on miss/corruption.

        A truncated or otherwise unreadable file is counted as corrupt, unlinked,
        and reported as a miss so the next :meth:`put` can repair the entry.
        """
        path = self._path_for(key)
        if not path.is_file():
            self._misses += 1
            return None
        try:
            arr = np.load(path, allow_pickle=False)
        except Exception:  # truncated / non-npy / unreadable -> treat as a miss
            self._corrupt += 1
            self._misses += 1
            self._unlink_quiet(path)
            return None
        self._hits += 1
        return np.asarray(arr, dtype=np.float64)

    def put(self, key: str, arr: np.ndarray) -> None:
        """Store the ``(T, N)`` matrix ``arr`` under ``key`` (atomic write).

        The array is materialised as float64 and written to a temp file in the
        shard directory, then atomically renamed into place so a concurrent or
        crashing writer can never expose a partial file to a reader.
        """
        matrix = np.ascontiguousarray(np.asarray(arr, dtype=np.float64))
        path = self._path_for(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Temp file in the same directory guarantees os.replace is atomic (same FS).
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            with open(tmp, "wb") as fh:
                np.save(fh, matrix, allow_pickle=False)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        except Exception:
            self._unlink_quiet(tmp)
            raise
        self._writes += 1

    # -- maintenance / introspection -----------------------------------------
    def stats(self) -> dict:
        """Counters + on-disk footprint for ``assay cache stats`` (engineering §8)."""
        n_files = 0
        n_bytes = 0
        for path in self.cache_dir.rglob("*.npy"):
            try:
                n_bytes += path.stat().st_size
                n_files += 1
            except OSError:  # raced unlink — ignore
                continue
        lookups = self._hits + self._misses
        return {
            "cache_dir": str(self.cache_dir),
            "entries": n_files,
            "bytes": n_bytes,
            "hits": self._hits,
            "misses": self._misses,
            "writes": self._writes,
            "corrupt": self._corrupt,
            "hit_rate": (self._hits / lookups) if lookups else 0.0,
        }

    def clear(self) -> int:
        """Delete every cached entry. Returns the number of files removed."""
        removed = 0
        for path in self.cache_dir.rglob("*.npy"):
            if self._unlink_quiet(path):
                removed += 1
        return removed

    @staticmethod
    def _unlink_quiet(path: Path) -> bool:
        """Best-effort unlink; never raises. ``True`` if a file was removed."""
        try:
            path.unlink()
            return True
        except OSError:
            return False
