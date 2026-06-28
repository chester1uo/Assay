#!/usr/bin/env python3
"""Ingest the raw Tushare CN mirror into Assay's canonical stores (market=CN).

Reads ``$TUSHARE_DATA_DIR`` (default /data/tushare_data; produced by
scripts/download_tushare.py) and writes ``price_raw`` / ``adj_events`` /
``universe_snapshots`` / ``trade_status`` partitions for ``market=CN`` under
``$ASSAY_DATA_DIR`` (default ./data), ready for the FactorEngine / portfolio
backtester.

Usage:
    python scripts/prepare_cn.py 2015-01-01 2025-12-31

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

from assay.config import AssayConfig  # noqa: E402
from assay.data.tushare.ingest import prepare_cn  # noqa: E402


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    start = dt.date.fromisoformat(sys.argv[1])
    end = dt.date.fromisoformat(sys.argv[2])
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S"
    )
    data_dir = AssayConfig.from_env().data_dir
    report = prepare_cn(data_dir, start=start, end=end)
    print(__import__("json").dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
