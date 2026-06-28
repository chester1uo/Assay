#!/usr/bin/env python3
"""Download raw Tushare data (A-share + Hong Kong) into a local mirror.

Covers CSI300 / CSI500 / CSI1000 constituent history + prices/adjustment/
dividends/valuation, and HSI / Hang Seng TECH index series + a current-snapshot
HK constituent universe. See :mod:`assay.data.tushare.download` for the layout.

Examples
--------
    # Full backfill since 2010 into /data/tushare_data (token from env):
    TUSHARE_TOKEN=xxxx python scripts/download_tushare.py

    # Just the A-share universe + prices for a smoke test (5 symbols):
    python scripts/download_tushare.py --steps cn_universe,cn_prices --limit-symbols 5

    # Re-download HK only, overwriting existing files:
    python scripts/download_tushare.py --markets hk --force

Run from source with PYTHONPATH=src, or after `pip install -e .`. The token is
read from --token or the TUSHARE_TOKEN env var (never commit it).
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# Allow running straight from a checkout without installing the package.
_SRC = Path(__file__).resolve().parents[1] / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from assay.data.tushare.download import (  # noqa: E402
    ALL_STEPS,
    DEFAULT_DATA_DIR,
    DEFAULT_START,
    TushareDownloadConfig,
    run_download,
    today_yyyymmdd,
)

_CN_STEPS = ("cn_universe", "cn_prices", "cn_adj", "cn_basic", "cn_dividend", "cn_limit")
_HK_STEPS = ("hk_index", "hk_prices")


def _resolve_steps(args) -> set[str]:
    if args.steps:
        chosen = {s.strip() for s in args.steps.split(",") if s.strip()}
        unknown = chosen - set(ALL_STEPS)
        if unknown:
            raise SystemExit(f"unknown steps: {sorted(unknown)}; valid: {list(ALL_STEPS)}")
        return chosen
    markets = {m.strip().lower() for m in args.markets.split(",") if m.strip()}
    steps: set[str] = {"meta"}
    if "cn" in markets:
        steps |= set(_CN_STEPS)
    if "hk" in markets:
        steps |= set(_HK_STEPS)
    return steps


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--token", default=None, help="Tushare token (default: $TUSHARE_TOKEN)")
    ap.add_argument("--data-dir", default=None, help=f"output root (default: $TUSHARE_DATA_DIR or {DEFAULT_DATA_DIR})")
    ap.add_argument("--start", dest="start_date", default=None, help=f"YYYYMMDD (default: {DEFAULT_START})")
    ap.add_argument("--end", dest="end_date", default=None, help="YYYYMMDD (default: today)")
    ap.add_argument("--markets", default="cn,hk", help="comma list: cn,hk (ignored if --steps given)")
    ap.add_argument("--steps", default=None, help=f"explicit comma list from {list(ALL_STEPS)}")
    ap.add_argument("--rate", dest="calls_per_min", type=int, default=380, help="API calls/min budget")
    ap.add_argument("--workers", dest="max_workers", type=int, default=8, help="concurrent symbol workers")
    ap.add_argument("--limit-symbols", type=int, default=0, help="cap symbols per market (smoke test)")
    ap.add_argument("--force", action="store_true", help="re-download existing files")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    cfg = TushareDownloadConfig.from_env(
        token=args.token,
        data_dir=args.data_dir,
        start_date=args.start_date,
        end_date=args.end_date,
        calls_per_min=args.calls_per_min,
        max_workers=args.max_workers,
        limit_symbols=args.limit_symbols,
        force=args.force,
    )
    if not cfg.token:
        raise SystemExit("no token: pass --token or set TUSHARE_TOKEN")

    steps = _resolve_steps(args)
    logging.getLogger("assay.tushare").info(
        "downloading %s  range=%s..%s  -> %s", sorted(steps), cfg.start_date, cfg.end_date, cfg.data_dir
    )
    manifest = run_download(cfg, steps)
    print(__import__("json").dumps(manifest["results"], indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
