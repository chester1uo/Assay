"""Tushare data source for Assay — Chinese A-share and Hong Kong markets.

This package downloads *raw* provider data (prices, dividends, split/merge
adjustment factors, index-membership history) into a local mirror, mirroring the
role the ``massive`` package plays for US equities. The bulk downloader talks to
the Tushare Pro HTTP API (https://api.tushare.pro) — the same token/backend as
the Tushare MCP server, but the plain HTTP surface supports offset/limit
pagination and is the right tool for a resumable multi-year backfill.

See :mod:`assay.data.tushare.download` for the orchestration and
``scripts/download_tushare.py`` for the CLI entry point.
"""

from __future__ import annotations

from .client import TushareClient, TushareError
from .constituents import (
    CN_INDEX_CODES,
    HK_INDEX_CODES,
    hk_constituents,
    hk_universe,
)
from .ingest import CN_INDICES, CN_MARKET, prepare_cn

__all__ = [
    "TushareClient",
    "TushareError",
    "CN_INDEX_CODES",
    "HK_INDEX_CODES",
    "hk_constituents",
    "hk_universe",
    "prepare_cn",
    "CN_INDICES",
    "CN_MARKET",
]
