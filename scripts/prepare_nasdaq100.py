#!/usr/bin/env python3
"""Convenience wrapper: prepare the NASDAQ-100 dataset for a date range.

Usage:
    python scripts/prepare_nasdaq100.py 2023-01-01 2023-12-31

Requires MASSIVE_* credentials in the environment (see .env.example). Run from
source with PYTHONPATH=src, or after `pip install -e .`.
"""

from __future__ import annotations

import datetime as dt
import sys

from assay.config import AssayConfig
from assay.data.pipeline import prepare_nasdaq100


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    start = dt.date.fromisoformat(sys.argv[1])
    end = dt.date.fromisoformat(sys.argv[2])
    import logging

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    report = prepare_nasdaq100(AssayConfig.from_env(), start, end)
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
