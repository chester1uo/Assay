"""MASSIVE REST client for corporate actions (splits & dividends).

Endpoints (Polygon-compatible)::

    GET {base}/stocks/v1/splits
    GET {base}/stocks/v1/dividends

Auth is an ``Authorization: Bearer <api_key>`` header (keeps the key out of
URLs/logs). Results are cursor-paginated via ``next_url``; this client follows
``next_url`` transparently and yields one result dict at a time.
"""

from __future__ import annotations

import datetime as dt
import email.utils
import logging
import time
from collections.abc import Iterator
from typing import Any

import requests

from assay.config import MassiveConfig

log = logging.getLogger(__name__)


def _parse_retry_after(value: str | None, fallback: float) -> float:
    """Interpret a ``Retry-After`` header (delta-seconds or HTTP-date)."""
    if not value:
        return fallback
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        pass
    try:
        when = email.utils.parsedate_to_datetime(value)
        if when is not None:
            if when.tzinfo is None:
                when = when.replace(tzinfo=dt.timezone.utc)
            return max(0.0, (when - dt.datetime.now(dt.timezone.utc)).total_seconds())
    except (TypeError, ValueError):
        pass
    return fallback

# splits accept up to ~1000/page in practice; dividends document a 5000 max.
_SPLITS_PAGE_LIMIT = 1000
_DIVIDENDS_PAGE_LIMIT = 1000
# how many tickers to bundle into one `ticker.any_of` request
_TICKER_BATCH = 50


class RestClient:
    """Paginating client for the MASSIVE corporate-actions endpoints."""

    def __init__(self, config: MassiveConfig, *, timeout: float = 30.0, max_retries: int = 5):
        self.config = config
        self.timeout = timeout
        self.max_retries = max_retries
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {config.api_key}",
                "Accept": "application/json",
            }
        )

    # -- low-level pagination -------------------------------------------------
    def _get(self, url: str, params: dict[str, Any] | None) -> dict[str, Any]:
        backoff = 1.0
        for attempt in range(1, self.max_retries + 1):
            try:
                resp = self._session.get(url, params=params, timeout=self.timeout)
            except requests.exceptions.RequestException as exc:
                # transient network error (timeout / connection reset): retry so a
                # single blip doesn't abort a multi-year backfill.
                if attempt == self.max_retries:
                    raise
                log.warning("MASSIVE %s network error (%s); retry %d/%d in %.1fs",
                            url, exc, attempt, self.max_retries, backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt == self.max_retries:
                    resp.raise_for_status()
                wait = _parse_retry_after(resp.headers.get("Retry-After"), backoff)
                log.warning("MASSIVE %s -> %s; retry %d/%d in %.1fs",
                            url, resp.status_code, attempt, self.max_retries, wait)
                time.sleep(wait)
                backoff = min(backoff * 2, 30.0)
                continue
            resp.raise_for_status()
            return resp.json()
        raise RuntimeError("unreachable")  # pragma: no cover

    def _paginate(self, path: str, params: dict[str, Any]) -> Iterator[dict[str, Any]]:
        url: str | None = f"{self.config.rest_base_url}{path}"
        # `next_url` is a fully-formed URL carrying the cursor; send params only
        # on the first request.
        first_params: dict[str, Any] | None = params
        while url:
            payload = self._get(url, first_params)
            yield from payload.get("results", [])
            url = payload.get("next_url")
            first_params = None

    # -- typed endpoints ------------------------------------------------------
    def iter_splits(self, **filters: Any) -> Iterator[dict[str, Any]]:
        """Yield split result dicts. Pass filters like ``ticker='AAPL'`` or
        ``**{'execution_date.gte': '2020-01-01'}``."""
        params = {"limit": _SPLITS_PAGE_LIMIT, "sort": "execution_date.asc", **filters}
        yield from self._paginate("/stocks/v1/splits", params)

    def iter_dividends(self, **filters: Any) -> Iterator[dict[str, Any]]:
        """Yield dividend result dicts. Pass filters like ``ticker='AAPL'`` or
        ``**{'ex_dividend_date.gte': '2020-01-01'}``."""
        params = {"limit": _DIVIDENDS_PAGE_LIMIT, "sort": "ex_dividend_date.asc", **filters}
        yield from self._paginate("/stocks/v1/dividends", params)

    # -- bulk helpers ---------------------------------------------------------
    def splits_for_tickers(
        self, tickers: list[str], start: str, end: str
    ) -> Iterator[dict[str, Any]]:
        """Yield all splits for ``tickers`` with execution_date in ``[start, end]``."""
        for batch in _batched(sorted(set(tickers)), _TICKER_BATCH):
            yield from self.iter_splits(
                **{
                    "ticker.any_of": ",".join(batch),
                    "execution_date.gte": start,
                    "execution_date.lte": end,
                }
            )

    def dividends_for_tickers(
        self, tickers: list[str], start: str, end: str
    ) -> Iterator[dict[str, Any]]:
        """Yield all dividends for ``tickers`` with ex_dividend_date in ``[start, end]``."""
        for batch in _batched(sorted(set(tickers)), _TICKER_BATCH):
            yield from self.iter_dividends(
                **{
                    "ticker.any_of": ",".join(batch),
                    "ex_dividend_date.gte": start,
                    "ex_dividend_date.lte": end,
                }
            )


def _batched(items: list[str], n: int) -> Iterator[list[str]]:
    for i in range(0, len(items), n):
        yield items[i : i + n]
