"""Factor-library persistence — engineering-docs section 7.2 (FactorReport store)
and the ``assay library list/prune`` CLI surface (section, redundancy management).

:class:`FactorLibrary` is the append-only-by-id store of evaluated factors. Each
:class:`~assay.library.report.FactorReport` is serialised to one JSON file at
``<path>/<factor_id>.json`` (the canonical-expression hash is the id, so re-saving
the same factor *overwrites* in place rather than duplicating). A lightweight
in-memory index of :class:`~assay.library.report.FactorSummary` rows backs
``list()`` so leaderboard/triage queries never have to read every full report.

Design notes:
- The index is rebuilt by scanning ``*.json`` on construction; the directory *is*
  the source of truth, so the store is safe to point at a fresh/empty dir and is
  robust to files written by another process between calls.
- ``None`` metrics sort and filter as ``-inf`` (a never-evaluated or failed factor
  ranks last and is never admitted by a ``min_*`` floor).
- All numeric filters/sorts key off the summary projection; the full report is only
  loaded on ``get()`` / ``all_reports()``.
"""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path

from assay.library.report import FactorReport, FactorSummary
from assay.library._atomic import write_text

__all__ = ["FactorLibrary"]

_NEG_INF = float("-inf")


def _as_sort_key(x: float | int | None) -> float:
    """None / non-finite -> -inf so missing metrics rank last under descending sort."""
    if x is None:
        return _NEG_INF
    try:
        v = float(x)
    except (TypeError, ValueError):
        return _NEG_INF
    return v if v == v else _NEG_INF  # NaN (v != v) -> -inf


class FactorLibrary:
    """JSON-file store of :class:`FactorReport`, indexed by :class:`FactorSummary`.

    Parameters
    ----------
    path:
        Directory holding one ``<factor_id>.json`` per factor. Created (with
        parents) if it does not exist. Safe on an empty or fresh directory.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.mkdir(parents=True, exist_ok=True)
        # factor_id -> FactorSummary (the queryable index)
        self._index: dict[str, FactorSummary] = {}
        self._lock = threading.RLock()
        self._index_stamp = None
        self._reindex()

    # -- internals ---------------------------------------------------------
    def _file(self, factor_id: str) -> Path:
        if not isinstance(factor_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', factor_id):
            raise ValueError('invalid factor id')
        return self.path / f"{factor_id}.json"

    def _reindex(self) -> None:
        """Rebuild the in-memory summary index by scanning the directory."""
        stamp = self._signature()
        self._index.clear()
        for fp in sorted(self.path.glob("*.json")):
            try:
                d = json.loads(fp.read_text())
            except (json.JSONDecodeError, OSError):
                continue  # skip corrupt/partial files rather than fail the whole store
            report = FactorReport.from_dict(d)
            fid = report.factor_id or fp.stem
            self._index[fid] = FactorSummary.from_report(report)
        self._index_stamp = stamp

    def _signature(self):
        # Directory timestamps can have coarse granularity. Atomic replacements
        # give each version a new inode, even when writes share one timestamp.
        rows = []
        for path in self.path.glob('*.json'):
            try:
                stat = path.stat()
            except FileNotFoundError:
                continue
            rows.append((path.name, stat.st_ino, stat.st_mtime_ns, stat.st_size))
        return tuple(sorted(rows))

    def _refresh(self):
        if self._signature() != self._index_stamp:
            self._reindex()

    # -- write -------------------------------------------------------------
    def save(self, report: FactorReport) -> str:
        """Persist ``report`` (overwriting any prior file for the same id); return its id.

        The id is taken from ``report.factor_id``, falling back to the canonical-
        expression hash when absent so every persisted file is addressable.
        """
        fid = report.factor_id or FactorReport.compute_factor_id(report.expr_canonical)
        report.factor_id = fid
        with self._lock:
            write_text(self._file(fid), report.to_json())
            self._index[fid] = FactorSummary.from_report(report)
        return fid

    # -- read --------------------------------------------------------------
    def get(self, factor_id: str) -> FactorReport | None:
        """Load the full :class:`FactorReport` for ``factor_id``, or ``None`` if absent."""
        fp = self._file(factor_id)
        if not fp.exists():
            return None
        try:
            return FactorReport.from_dict(json.loads(fp.read_text()))
        except (json.JSONDecodeError, OSError):
            return None

    def all_reports(self) -> list[FactorReport]:
        """Load every stored report (full objects). Order matches the sorted index keys."""
        out: list[FactorReport] = []
        with self._lock:
            self._refresh()
            ids = list(self._index)
        for fid in ids:
            r = self.get(fid)
            if r is not None:
                out.append(r)
        return out

    def list(
        self,
        *,
        universe: str | None = None,
        min_rank_icir: float = 0.0,
        max_redundancy: float = 1.0,
        source: str | None = None,
        sort_by: str = "rank_icir",
        limit: int = 100,
        offset: int = 0,
    ) -> list[FactorSummary]:
        """Filtered, sorted, paged view of the library as :class:`FactorSummary` rows.

        Filters (all conjunctive):
        - ``universe`` / ``source``: exact match when provided.
        - ``min_rank_icir``: keep rows whose ``rank_icir`` (None -> -inf) is ``>=``.
        - ``max_redundancy``: keep rows whose ``redundancy_score`` (None -> 0.0) is ``<=``.

        Sorting is **descending** by ``sort_by`` (any numeric summary attribute;
        unknown attr -> all-equal, so insertion order is preserved), with ``None``/
        non-finite metrics treated as ``-inf``. ``factor_id`` breaks ties for
        determinism. ``limit``/``offset`` page the result (``limit < 0`` -> no cap).
        """
        with self._lock:
            self._refresh()
            rows = list(self._index.values())

        # --- filter ---
        def _keep(s: FactorSummary) -> bool:
            if universe is not None and s.universe_id != universe:
                return False
            if source is not None and s.source != source:
                return False
            if _as_sort_key(s.rank_icir) < float(min_rank_icir):
                return False
            red = s.redundancy_score if s.redundancy_score is not None else 0.0
            if float(red) > float(max_redundancy):
                return False
            return True

        rows = [s for s in rows if _keep(s)]

        # --- sort (descending by metric, factor_id as stable tiebreak) ---
        def _key(s: FactorSummary):
            metric = _as_sort_key(getattr(s, sort_by, None))
            return (metric, s.factor_id)

        rows.sort(key=lambda s: s.factor_id)  # stable secondary order
        rows.sort(key=lambda s: _as_sort_key(getattr(s, sort_by, None)), reverse=True)

        # --- page ---
        off = max(int(offset), 0)
        rows = rows[off:]
        if limit is not None and limit >= 0:
            rows = rows[: int(limit)]
        return rows

    # -- delete ------------------------------------------------------------
    def delete(self, ids: list[str] | str) -> int:
        """Remove the given factor(s) from disk and the index; return the count deleted."""
        if isinstance(ids, str):
            ids = [ids]
        n = 0
        with self._lock:
            for fid in ids:
                fp = self._file(fid)
                self._index.pop(fid, None)
                try:
                    fp.unlink()
                    n += 1
                except FileNotFoundError:
                    pass
        return n
