"""Saved-combination persistence — reloadable combination "jobs".

A combination run (``AssayService.combine_factors``) already returns a JSON-safe
dict carrying the fitted **model**: the scheme, the per-factor weights/importances,
their orientation and train IC, the validation selection scores, and the train/val/
test scorecard. This store persists that dict so a run can be reloaded and viewed
later without recomputation — mirroring :class:`assay.library.store.FactorLibrary`,
but simpler because the payload is already a plain dict.

Layout: one ``<id>.json`` per saved run under ``<path>/``. Each file is a record
``{id, name, saved_at, result}``. Two save modes:

* **explicit** — a named, permanent record with a content-derived id.
* **last run** — a rolling record under the reserved id ``_last`` (overwritten each
  run) so the most recent result survives a page reload even if never saved.

A small in-memory summary index backs :meth:`list` so the picker never reads every
full record. The directory is the source of truth (rebuilt on construction).
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

__all__ = ["CombinationStore", "LAST_RUN_ID"]

LAST_RUN_ID = "_last"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _num(d: dict | None, key: str) -> float | None:
    v = (d or {}).get(key)
    try:
        f = float(v)
        return f if f == f else None  # drop NaN
    except (TypeError, ValueError):
        return None


class CombinationStore:
    """JSON-file store of saved combination results, indexed by a summary row."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.mkdir(parents=True, exist_ok=True)
        self._index: dict[str, dict[str, Any]] = {}
        self._reindex()

    # -- internals ---------------------------------------------------------
    def _file(self, cid: str) -> Path:
        # ids are our own (hash / '_last'); still guard against path escapes.
        safe = "".join(c for c in str(cid) if c.isalnum() or c in "._-")
        return self.path / f"{safe}.json"

    @staticmethod
    def _make_id(result: dict, name: str | None, saved_at: str) -> str:
        """Content-derived 12-hex id (stable per run; unique across saves via time)."""
        basis = json.dumps({
            "f": result.get("resolved_factors") or result.get("factor_names"),
            "s": result.get("splits"),
            "m": result.get("method"),
            "u": result.get("universe"),
            "n": name or "",
            "t": saved_at,
        }, sort_keys=True, default=str)
        return "cmb_" + hashlib.sha1(basis.encode("utf-8")).hexdigest()[:12]

    @staticmethod
    def _summarize(rec: dict) -> dict[str, Any]:
        r = rec.get("result", {}) or {}
        test = r.get("test", {}) or {}
        return {
            "id": rec.get("id"),
            "name": rec.get("name") or "",
            "saved_at": rec.get("saved_at"),
            "is_last": rec.get("id") == LAST_RUN_ID,
            "method": r.get("method"),
            "weight_kind": r.get("weight_kind"),
            "universe": r.get("universe"),
            "horizon": r.get("horizon"),
            "n_factors": len(r.get("factor_names") or []),
            "test_icir": _num(test, "icir"),
            "test_rank_icir": _num(test, "rank_icir"),
            "factor_names": list(r.get("factor_names") or []),
        }

    def _reindex(self) -> None:
        self._index.clear()
        for fp in sorted(self.path.glob("*.json")):
            try:
                rec = json.loads(fp.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            cid = rec.get("id") or fp.stem
            self._index[cid] = self._summarize(rec)

    # -- write -------------------------------------------------------------
    def save(self, result: dict, *, name: str | None = None, record_id: str | None = None) -> dict[str, Any]:
        """Persist a combination ``result``; return the summary of the saved record.

        ``record_id`` forces the id (e.g. :data:`LAST_RUN_ID` for the rolling last
        run); otherwise a content+time-derived id is minted so each explicit save is
        its own reloadable record.
        """
        saved_at = _now_iso()
        cid = record_id or self._make_id(result, name, saved_at)
        rec = {"id": cid, "name": name or "", "saved_at": saved_at, "result": result}
        self._file(cid).write_text(json.dumps(rec, default=str))
        self._index[cid] = self._summarize(rec)
        return self._index[cid]

    # -- read --------------------------------------------------------------
    def get(self, cid: str) -> dict[str, Any] | None:
        """Load the full record ``{id, name, saved_at, result}`` or ``None``."""
        fp = self._file(cid)
        if not fp.exists():
            return None
        try:
            return json.loads(fp.read_text())
        except (json.JSONDecodeError, OSError):
            return None

    def list(self, *, include_last: bool = True, limit: int = 200) -> list[dict[str, Any]]:
        """Saved-run summaries, newest first (the rolling last run first when kept)."""
        rows = [dict(s) for s in self._index.values()]
        if not include_last:
            rows = [r for r in rows if not r.get("is_last")]
        # last run pinned first, then by saved_at descending.
        rows.sort(key=lambda r: (not r.get("is_last"), _rev_iso(r.get("saved_at"))))
        return rows[: int(limit)] if limit and limit >= 0 else rows

    # -- delete ------------------------------------------------------------
    def delete(self, ids: list[str] | str) -> int:
        if isinstance(ids, str):
            ids = [ids]
        n = 0
        for cid in ids:
            fp = self._file(cid)
            existed = self._index.pop(cid, None) is not None
            if fp.exists():
                try:
                    fp.unlink()
                    existed = True
                except OSError:
                    pass
            if existed:
                n += 1
        return n


def _rev_iso(s: str | None) -> str:
    """Sort key that puts newer ISO timestamps first (descending)."""
    return "" if not s else "".join(chr(255 - ord(c)) if ord(c) < 255 else c for c in s)
