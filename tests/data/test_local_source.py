"""Local-source readers + ingesters: a tiny on-disk MASSIVE mirror -> Assay stores.

Builds a minimal local mirror (one day-aggregate parquet, one splits JSONL, one
dividends JSONL) in a tmp dir, points :class:`MassiveConfig` at it, and verifies
the readers and ingesters transform it into the ``price_raw`` / ``adj_events``
parquet stores with the right schema, values, and point-in-time conventions.
"""

from __future__ import annotations

import datetime as dt
import json

import polars as pl

from assay.config import AssayConfig, MassiveConfig
from assay.data.ingest import CorpActionIngester, PriceIngester
from assay.data.massive import LocalCorpActions, LocalFlatFiles
from assay.data.schemas import (
    DAY_AGG_CSV_COLUMNS,
    PRICE_RAW_SCHEMA,
    adj_events_path,
    price_partition_path,
)

# 2024-01-02 00:00 America/New_York == 2024-01-02 05:00 UTC == this many ns.
_WINDOW_START_2024_01_02 = 1704171600000000000


def _build_mirror(source_dir):
    """Write a minimal MASSIVE-format local mirror under ``source_dir``."""
    # one day-aggregate parquet at us_stocks_sip/day_aggs_v1/2024/01/2024-01-02.parquet
    day_dir = source_dir / "us_stocks_sip" / "day_aggs_v1" / "2024" / "01"
    day_dir.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(
        {
            "ticker": ["AAPL", "MSFT", "ZZZZ"],
            "volume": [1000, 2000, 30],
            "open": [185.0, 370.0, 1.0],
            "close": [186.0, 372.0, 1.1],
            "high": [187.0, 373.0, 1.2],
            "low": [184.0, 369.0, 0.9],
            "window_start": [_WINDOW_START_2024_01_02] * 3,
            "transactions": [50, 60, 5],
        },
        # match the on-disk MASSIVE day-aggregate dtypes
        schema={
            "ticker": pl.Utf8,
            "volume": pl.Int64,
            "open": pl.Float64,
            "close": pl.Float64,
            "high": pl.Float64,
            "low": pl.Float64,
            "window_start": pl.Int64,
            "transactions": pl.Int64,
        },
    ).write_parquet(day_dir / "2024-01-02.parquet")

    # per-ticker corporate-action JSONL
    splits_dir = source_dir / "corporate_actions" / "splits"
    divs_dir = source_dir / "corporate_actions" / "dividends"
    splits_dir.mkdir(parents=True, exist_ok=True)
    divs_dir.mkdir(parents=True, exist_ok=True)
    (splits_dir / "AAPL.jsonl").write_text(
        json.dumps(
            {
                "id": "split-aapl-1",
                "execution_date": "2024-01-02",
                "split_from": 1.0,
                "split_to": 4.0,
                "ticker": "AAPL",
                "adjustment_type": "forward_split",
                "historical_adjustment_factor": 0.25,
            }
        )
        + "\n"
    )
    (divs_dir / "MSFT.jsonl").write_text(
        json.dumps(
            {
                "id": "div-msft-1",
                "ticker": "MSFT",
                "record_date": "2024-01-05",
                "pay_date": "2024-01-10",
                "ex_dividend_date": "2024-01-02",
                "frequency": 4,
                "cash_amount": 0.75,
                "currency": "USD",
                "distribution_type": "recurring",
            }
        )
        + "\n"
    )


def _config(tmp_path):
    source_dir = tmp_path / "massive_data"
    source_dir.mkdir()
    _build_mirror(source_dir)
    cfg = AssayConfig(
        massive=MassiveConfig(source_dir=source_dir),
        data_dir=tmp_path / "out",
        market="US",
    )
    return cfg


def test_local_flatfiles_list_and_read(tmp_path):
    cfg = _config(tmp_path)
    client = LocalFlatFiles(cfg.massive)

    files = client.list_day_aggs(dt.date(2024, 1, 1), dt.date(2024, 1, 31))
    assert [f.date for f in files] == [dt.date(2024, 1, 2)]
    assert files[0].key == "us_stocks_sip/day_aggs_v1/2024/01/2024-01-02.parquet"

    df = client.read_day_agg(dt.date(2024, 1, 2), symbols={"AAPL", "MSFT"})
    assert df is not None and df.height == 2
    assert set(DAY_AGG_CSV_COLUMNS).issubset(set(df.columns))
    assert "date" in df.columns
    # ET trading date derived from window_start
    assert df["date"].unique().to_list() == [dt.date(2024, 1, 2)]
    # holiday / missing date -> None
    assert client.read_day_agg(dt.date(2024, 1, 3)) is None


def test_price_ingester_writes_price_raw(tmp_path):
    cfg = _config(tmp_path)
    stats = PriceIngester(cfg).run(
        dt.date(2024, 1, 1), dt.date(2024, 1, 31), symbols={"AAPL", "MSFT"}
    )
    assert stats["files_loaded"] == 1
    assert stats["rows"] == 2
    assert stats["partitions"] == 1

    path = price_partition_path(cfg.data_dir, "US", 2024, 1)
    out = pl.read_parquet(path)
    assert list(out.columns) == list(PRICE_RAW_SCHEMA.keys())
    assert sorted(out["symbol"].to_list()) == ["AAPL", "MSFT"]
    aapl = out.filter(pl.col("symbol") == "AAPL")
    assert aapl["close"].item() == 186.0
    assert aapl["as_of_date"].item() == dt.date(2024, 1, 2)  # EOD bar knowable same day


def test_local_corp_actions_reader_filters_by_date(tmp_path):
    cfg = _config(tmp_path)
    client = LocalCorpActions(cfg.massive)

    splits = list(client.splits_for_tickers(["AAPL", "MSFT"], "2024-01-01", "2024-01-31"))
    assert len(splits) == 1 and splits[0]["ticker"] == "AAPL"
    # out-of-range -> nothing
    assert not list(client.splits_for_tickers(["AAPL"], "2025-01-01", "2025-12-31"))

    divs = list(client.dividends_for_tickers(["AAPL", "MSFT"], "2024-01-01", "2024-01-31"))
    assert len(divs) == 1 and divs[0]["ticker"] == "MSFT"


def test_corp_action_ingester_writes_adj_events(tmp_path):
    cfg = _config(tmp_path)
    stats = CorpActionIngester(cfg).run(["AAPL", "MSFT"], dt.date(2024, 1, 1), dt.date(2024, 1, 31))
    assert stats["splits"] == 1
    assert stats["dividends"] == 1

    out = pl.read_parquet(adj_events_path(cfg.data_dir, "US"))
    aapl = out.filter(pl.col("symbol") == "AAPL")
    assert aapl["event_type"].item() == "FORWARD_SPLIT"
    assert aapl["split_ratio"].item() == 4.0
    msft = out.filter(pl.col("symbol") == "MSFT")
    assert msft["event_type"].item() == "DIVIDEND"
    assert msft["dividend_cash"].item() == 0.75
    # no declaration date in the local dump -> as_of falls back to the ex-date
    assert msft["as_of_date"].item() == dt.date(2024, 1, 2)
