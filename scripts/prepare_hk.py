#!/usr/bin/env python3
"""Ingest the raw Tushare HK mirror into Assay's canonical stores (market=HK).

Reads ``$TUSHARE_DATA_DIR`` (default /data/tushare_data; produced by
scripts/download_tushare.py --markets hk) and writes ``price_raw`` /
``adj_events`` / ``universe_snapshots`` partitions for ``market=HK`` under
``$ASSAY_DATA_DIR_HK`` (default ./data_hk), ready for the FactorEngine /
portfolio backtester.

Usage:
    python scripts/prepare_hk.py 2015-01-01 2026-07-27

Run from source with PYTHONPATH=src, or after `pip install -e .`.
"""

from __future__ import annotations

import datetime as dt
import logging
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from assay import config_store  # noqa: E402
from assay.config import AssayConfig  # noqa: E402
from assay.data.tushare.ingest import HK_MARKET, prepare_hk  # noqa: E402


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    start = dt.date.fromisoformat(sys.argv[1])
    end = dt.date.fromisoformat(sys.argv[2])
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S"
    )
    # Never fall back to cfg.data_dir — that is the US store, and landing
    # market=HK partitions inside it is silent cross-market contamination.
    cfg = AssayConfig.from_env()
    data_dir = cfg.market_dirs.get(HK_MARKET) or config_store.assay_dir(HK_MARKET)
    if not data_dir:
        raise SystemExit("no HK output dir: set ASSAY_DATA_DIR_HK or dirs.assay_hk in the config")
    report = prepare_hk(data_dir, start=start, end=end)
    print(__import__("json").dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
