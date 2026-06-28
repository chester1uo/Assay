"""Corporate-action ingester: local MASSIVE splits & dividends -> ``adj_events``.

Each split/dividend becomes one ``adj_events`` row recording the *primitive*
event (forward split ratio or raw cash dividend). The cumulative point-in-time
adjustment factor is derived at read time (see :mod:`assay.data.store.adjust`),
never copied from the provider's as-of-today ``historical_adjustment_factor``
(which is retained only for cross-checking).

Knowledge-time (``as_of_date``) conventions:

* dividends -> ``declaration_date`` (the announcement), falling back to the
  ex-dividend date when no declaration date is available (the local dump carries
  no declaration date, so the ex-date is used);
* splits    -> ``execution_date`` (the splits records expose no announcement
  date, so the effective date is the conservative knowable date).
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

import polars as pl

from assay.config import AssayConfig
from assay.data.io_utils import upsert_parquet
from assay.data.massive.corpactions import LocalCorpActions
from assay.data.schemas import ADJ_EVENTS_SCHEMA, adj_events_path

log = logging.getLogger(__name__)


def _date(value: Any) -> dt.date | None:
    if not value:
        return None
    try:
        return dt.date.fromisoformat(str(value))
    except ValueError:
        return None


def _split_row(rec: dict[str, Any]) -> dict[str, Any] | None:
    tkr = rec.get("ticker")
    ex_date = _date(rec.get("execution_date"))
    sfrom = rec.get("split_from")
    sto = rec.get("split_to")
    if not tkr or ex_date is None or not sfrom or not sto:
        return None
    # Fall back to a deterministic natural key when the provider omits `id`, so
    # distinct events never collapse onto a shared blank dedup key.
    event_id = str(rec.get("id") or f"massive:splits:{tkr}:{ex_date}:{sfrom}-{sto}")
    return {
        "symbol": tkr,
        "ex_date": ex_date,
        "as_of_date": ex_date,
        "event_type": str(rec.get("adjustment_type", "split")).upper(),
        "split_ratio": float(sto) / float(sfrom),  # forward ratio (e.g. 4.0 for 1:4)
        "dividend_cash": 0.0,
        "provider_adj_factor": float(rec.get("historical_adjustment_factor") or 0.0),
        "event_id": event_id,
        "source": "massive:splits",
    }


def _dividend_row(rec: dict[str, Any]) -> dict[str, Any] | None:
    tkr = rec.get("ticker")
    ex_date = _date(rec.get("ex_dividend_date"))
    cash = rec.get("cash_amount")
    if not tkr or ex_date is None or cash is None:
        return None
    as_of = _date(rec.get("declaration_date")) or ex_date
    event_id = str(rec.get("id") or f"massive:dividends:{tkr}:{ex_date}:{cash}")
    return {
        "symbol": tkr,
        "ex_date": ex_date,
        "as_of_date": as_of,
        "event_type": "DIVIDEND",
        "split_ratio": 1.0,
        "dividend_cash": float(cash),
        "provider_adj_factor": float(rec.get("historical_adjustment_factor") or 0.0),
        "event_id": event_id,
        "source": "massive:dividends",
    }


class CorpActionIngester:
    def __init__(self, config: AssayConfig, client: LocalCorpActions | None = None):
        self.config = config
        self.client = client or LocalCorpActions(config.massive)

    def run(self, tickers, start: dt.date, end: dt.date) -> dict:
        tickers = sorted(set(tickers))
        start_s, end_s = start.isoformat(), end.isoformat()
        rows: list[dict[str, Any]] = []

        n_splits = 0
        for rec in self.client.splits_for_tickers(tickers, start_s, end_s):
            row = _split_row(rec)
            if row:
                rows.append(row)
                n_splits += 1

        n_divs = 0
        for rec in self.client.dividends_for_tickers(tickers, start_s, end_s):
            row = _dividend_row(rec)
            if row:
                rows.append(row)
                n_divs += 1

        log.info("corp actions: %d splits, %d dividends for %d tickers",
                 n_splits, n_divs, len(tickers))

        stats = {"splits": n_splits, "dividends": n_divs, "rows": 0}
        if not rows:
            return stats

        df = pl.DataFrame(rows, schema=ADJ_EVENTS_SCHEMA)
        path = adj_events_path(self.config.data_dir, self.config.market)
        stats["rows"] = upsert_parquet(
            path, df, keys=["event_id"], sort_by=["symbol", "ex_date"]
        )
        log.info("wrote %s (%d total event rows)", path, stats["rows"])
        return stats
