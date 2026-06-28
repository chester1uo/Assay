"""Local reader for MASSIVE corporate actions (splits & dividends).

Reads the per-ticker JSONL files from the locally-downloaded MASSIVE mirror
instead of paginating the REST API. On-disk layout::

    {splits_dir}/{TICKER}.jsonl       # one JSON split record per line
    {dividends_dir}/{TICKER}.jsonl    # one JSON dividend record per line

Split records carry ``execution_date``, ``split_from``/``split_to`` and an
``id``; dividend records carry ``ex_dividend_date`` and ``cash_amount``. The
local dump has no declaration date, so the dividend knowledge-time falls back to
the ex-dividend date — handled by the ingester.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from assay.config import MassiveConfig

log = logging.getLogger(__name__)


class LocalCorpActions:
    """Reader over the locally-downloaded MASSIVE corporate-action JSONL files."""

    def __init__(self, config: MassiveConfig):
        self.config = config

    # -- low-level ------------------------------------------------------------
    @staticmethod
    def _read_jsonl(path: Path) -> Iterator[dict[str, Any]]:
        if not path.is_file():
            return
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    log.warning("skipping malformed JSON line in %s", path)

    # -- typed readers --------------------------------------------------------
    def iter_splits(self, ticker: str) -> Iterator[dict[str, Any]]:
        """Yield every split record for ``ticker`` (unfiltered)."""
        yield from self._read_jsonl(self.config.splits_dir / f"{ticker}.jsonl")

    def iter_dividends(self, ticker: str) -> Iterator[dict[str, Any]]:
        """Yield every dividend record for ``ticker`` (unfiltered)."""
        yield from self._read_jsonl(self.config.dividends_dir / f"{ticker}.jsonl")

    # -- bulk helpers (date strings are ISO 'YYYY-MM-DD', compared lexically) --
    def splits_for_tickers(
        self, tickers: list[str], start: str, end: str
    ) -> Iterator[dict[str, Any]]:
        """Yield all splits for ``tickers`` with execution_date in ``[start, end]``."""
        for ticker in sorted(set(tickers)):
            for rec in self.iter_splits(ticker):
                d = rec.get("execution_date")
                if d is not None and start <= str(d) <= end:
                    yield rec

    def dividends_for_tickers(
        self, tickers: list[str], start: str, end: str
    ) -> Iterator[dict[str, Any]]:
        """Yield all dividends for ``tickers`` with ex_dividend_date in ``[start, end]``."""
        for ticker in sorted(set(tickers)):
            for rec in self.iter_dividends(ticker):
                d = rec.get("ex_dividend_date")
                if d is not None and start <= str(d) <= end:
                    yield rec
