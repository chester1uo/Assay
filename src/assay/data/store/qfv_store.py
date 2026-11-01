"""US daily data from the QFinVerse daily panel store.

QFinVerse builds the store from MASSIVE day bars and corporate actions (format
``qfv-daily-panel/1``, reader vendored in :mod:`assay.data.qfv_panel`). This class
gives the engine the same interface as :class:`~assay.data.store.DataStore`, plus
two things the parquet store cannot do:

* :meth:`get_matrices`: fields as ``(dates x symbols)`` matrices, without the long
  frame the engine would pivot back;
* point-in-time membership (:meth:`universe_span`, :meth:`get_membership`): every
  security that was a member at some point in the period, and on which dates, so
  evaluation is not limited to the members on the last day.

Universes: ``TOP500`` / ``TOP1000`` / ``TOP3000`` (QFinVerse liquidity universes,
monthly, no survivorship bias) and the index histories Assay ships (``SP500``,
``NASDAQ100``), whose tickers are resolved to QFinVerse securities on each date.

Symbols are QFinVerse labels: the ticker, or ``TICKER~YYYY-MM`` for an earlier
company that used a ticker someone else holds now.
"""

from __future__ import annotations

import datetime as dt
import logging
import threading
from pathlib import Path

import numpy as np
import polars as pl

from assay.data.qfv_panel import DailyPanel
from assay.data.schemas import TRADE_STATUS_SCHEMA

log = logging.getLogger(__name__)

QFV_UNIVERSES = {"TOP500": "top500", "TOP1000": "top1000", "TOP3000": "top3000"}
INDEX_UNIVERSES = ("SP500", "NASDAQ100")
_FIELDS = ("open", "high", "low", "close", "volume", "transactions", "dvol", "ret")


def _iso(value) -> str:
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()[:10]
    return str(value)[:10]


def _index_module(index_id: str):
    if index_id == "SP500":
        from assay.data.universe import sp500 as mod
    elif index_id == "NASDAQ100":
        from assay.data.universe import nasdaq100 as mod
    else:
        raise KeyError(index_id)
    return mod


class QFVPanelStore:
    market = "US"

    def __init__(self, root: str | Path, config=None):
        self.root = Path(root)
        self.config = config
        self.panel = DailyPanel(self.root)
        self._lock = threading.Lock()
        self._checked = 0.0

    # ── identity ────────────────────────────────────────────────────────
    @property
    def snapshot_id(self) -> str:
        return f"qfv-daily-panel:{self.panel.version.name}"

    def refresh(self, min_interval_s: float = 30.0) -> bool:
        """Switch to a newer published version (checked at most every ``min_interval_s``).

        The old reader object is replaced, not changed, so a request already holding it
        finishes on the version it started with."""
        import time

        now = time.monotonic()
        if now - self._checked < min_interval_s:
            return False
        with self._lock:
            self._checked = now
            from assay.data.qfv_panel.layout import read_current

            if read_current(self.root) == self.panel.version:
                return False
            self.panel = DailyPanel(self.root)
            log.info("qfv panel: now on %s", self.panel.version.name)
            return True

    # ── universes ───────────────────────────────────────────────────────
    def universes(self) -> list[str]:
        return list(QFV_UNIVERSES) + list(INDEX_UNIVERSES)

    def _index_sids(self, index_id: str, on: str) -> np.ndarray:
        tickers = sorted(_index_module(index_id).members_on(dt.date.fromisoformat(on)))
        sids = self.panel.sids(tickers, on=on)
        return np.unique(sids[sids >= 0])

    def _members(self, index_id: str, on: str) -> np.ndarray:
        uid = index_id.upper()
        if uid in QFV_UNIVERSES:
            return self.panel.universe(QFV_UNIVERSES[uid], on)
        if uid in INDEX_UNIVERSES:
            return self._index_sids(uid, on)
        raise ValueError(f"unknown universe {index_id!r}; available: {self.universes()}")

    def get_universe(self, index_id: str, date, as_of_date) -> list[str]:
        """Members on ``min(date, as_of_date)``."""
        on = min(_iso(date), _iso(as_of_date))
        return self.panel.labels(self._members(index_id, on))

    def _change_dates(self, index_id: str, start: str, end: str) -> list[str]:
        uid = index_id.upper()
        if uid in QFV_UNIVERSES:
            dates, _ = self.panel._universe_table()[QFV_UNIVERSES[uid]]
            days = [str(d) for d in dates]
        else:
            snaps = _index_module(uid).membership_snapshots(dt.date.fromisoformat(start), dt.date.fromisoformat(end))
            days = [d.isoformat() for d, _ in snaps]
        before = [d for d in days if d <= start]
        return ([before[-1]] if before else []) + [d for d in days if start < d <= end] or [start]

    def universe_span(self, index_id: str, start, end, as_of_date=None) -> list[str]:
        """Every member at some point in [start, min(end, as_of)]: the cross-section to evaluate on."""
        start, end = _iso(start), min(_iso(end), _iso(as_of_date or end))
        sids = [self._members(index_id, max(d, start)) for d in self._change_dates(index_id, start, end)]
        allsids = np.unique(np.concatenate(sids)) if sids else np.array([], dtype=np.int32)
        return sorted(self.panel.labels(allsids))

    def get_membership(self, index_id: str, dates, symbols, as_of_date=None) -> np.ndarray:
        """Bool (dates x symbols): member on that date."""
        dates = np.asarray(dates).astype("datetime64[D]")
        symbols = list(symbols)
        if not len(dates) or not symbols:
            return np.zeros((len(dates), len(symbols)), dtype=bool)
        sids = self.panel.sids(symbols, on=str(dates[-1]))
        uid = index_id.upper()
        if uid in QFV_UNIVERSES:
            return self.panel.membership(QFV_UNIVERSES[uid], dates, sids)
        out = np.zeros((len(dates), len(symbols)), dtype=bool)
        change = [np.datetime64(d) for d in self._change_dates(uid, str(dates[0]), str(dates[-1]))]
        for i, d in enumerate(change):
            rows = (dates >= d) & (dates < change[i + 1] if i + 1 < len(change) else True)
            if rows.any():
                out[np.ix_(rows, np.isin(sids, self._members(uid, str(max(d, dates[0])))))] = True
        return out

    # ── prices ──────────────────────────────────────────────────────────
    def _check_fields(self, fields) -> None:
        unknown = [f for f in fields if f not in _FIELDS]
        if unknown:
            raise ValueError(f"unsupported field(s) {unknown}; the QFinVerse panel provides {list(_FIELDS)} "
                             "(vwap is not in MASSIVE day bars)")

    def get_matrices(self, fields, symbols, start_date, end_date, as_of_date, adj: str = "split"):
        """(dates datetime64[D], symbols sorted, {field: (T x N) float64}) over trading days in
        [start, min(end, as_of)], adjusted onto that last day's basis."""
        if as_of_date is None:
            raise ValueError("as_of_date is required (point-in-time correctness).")
        self._check_fields(fields)
        adj = "total" if adj == "forward" else adj
        end = min(_iso(end_date), _iso(as_of_date))
        p = self.panel
        symbols = sorted(set(symbols))
        sids = p.sids(symbols, on=end)
        keep = sids >= 0
        if not keep.all():
            log.info("qfv panel: %d unknown symbols dropped", int((~keep).sum()))
        symbols = [s for s, k in zip(symbols, keep) if k]
        m = p.matrix(list(fields), sids[keep], _iso(start_date), end, adjust=adj)
        return m.dates, np.array(symbols, dtype=object), m.data

    def get_panel(self, fields, symbols, start_date, end_date, as_of_date, adj: str = "split") -> pl.DataFrame:
        """Long frame ``date, symbol, *fields`` (rows only where the security has a bar)."""
        dates, syms, data = self.get_matrices(fields, symbols if symbols is not None else [], start_date,
                                              end_date, as_of_date, adj)
        if not len(dates) or not len(syms):
            return pl.DataFrame(schema={"date": pl.Date, "symbol": pl.Utf8, **{f: pl.Float64 for f in fields}})
        have = np.zeros((len(dates), len(syms)), dtype=bool)
        for f in fields:
            have |= np.isfinite(data[f])
        r, c = np.nonzero(have)
        cols = {"date": pl.Series(dates[r]).cast(pl.Date), "symbol": pl.Series(syms[c].astype(str))}
        for f in fields:
            cols[f] = pl.Series(data[f][r, c])
        return pl.DataFrame(cols).sort(["date", "symbol"])

    def get_trade_status(self, symbols, start_date, end_date, as_of_date) -> pl.DataFrame:
        return pl.DataFrame(schema=TRADE_STATUS_SCHEMA)          # no price limits in the US

    def get_groups(self, symbols, as_of_date) -> dict[str, str]:
        return {}                                                # no PIT industry labels yet

    # ── store facts (status pages) ──────────────────────────────────────
    def price_span(self) -> dict:
        m = self.panel.manifest
        return {"first_date": m["first_date"], "last_date": m["last_date"], "trading_days": m["dates"],
                "symbols_in_store": m["securities"], "last_sync": m.get("built_at"), "ingested": True,
                "source": "QFinVerse daily panel", "version": self.panel.version.name}

    def coverage(self, year: int | None = None) -> list[dict]:
        """Securities with a bar per trading day."""
        out = []
        for b in self.panel.blocks.blocks:
            if year is not None and int(str(b.dates[0])[:4]) != int(year):
                continue
            counts = np.isfinite(b.field("close")).sum(axis=1)
            out.extend({"date": str(d), "n_symbols": int(n)} for d, n in zip(b.dates, counts))
        peak = max((r["n_symbols"] for r in out), default=1) or 1
        for r in out:
            r["coverage_pct"] = round(r["n_symbols"] / peak, 4)
        return out

    def last_rebalance(self, index_id: str, as_of=None) -> str | None:
        uid = index_id.upper()
        on = _iso(as_of or self.panel.last_date)
        try:
            days = self._change_dates(uid, "1900-01-01", on)
        except (KeyError, ValueError):
            return None
        return days[-1] if days else None
