"""Thin, dependency-light client for the Tushare Pro HTTP API.

Why the HTTP API and not the MCP server: both share the same token and backend,
but the HTTP API (``https://api.tushare.pro``) returns clean ``{fields, items}``
JSON and supports ``offset``/``limit`` pagination — exactly what a bulk,
resumable, multi-year backfill needs. The MCP transport is for interactive LLM
tool calls, not for pulling millions of rows.

Stdlib only (``urllib``) so it runs in the project venv without ``requests``.

Behaviour worth knowing:
  * A global :class:`RateLimiter` spaces calls evenly (thread-safe), so a thread
    pool can share one client without tripping per-minute quotas.
  * Per-minute quota errors ("每分钟最多访问") are retried with backoff.
  * Permission errors (code 40203, "没有接口...访问权限") are *not* retried — they
    are a property of the token, not a transient failure — and raise
    :class:`TusharePermissionError` so callers can skip an unavailable endpoint.
  * :meth:`call_paged` transparently walks ``offset``/``limit`` so a query that
    exceeds the server's per-call row cap is never silently truncated.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request

import polars as pl

API_URL = "https://api.tushare.pro"

# Per-call row cap. Observed empirically: index_weight caps at 7000, daily/
# adj_factor around 5000-6000. 5000 is a safe page size under all of them.
DEFAULT_PAGE_LIMIT = 5000

# Tushare overloads code 40203 for several conditions, distinguished by message:
#   * per-minute throttle ("...次/分钟") -> transient, retry with short backoff
#   * no interface permission -> token property, skip the endpoint
#   * per-hour/day quota ("频率超限 ... 5次/天") -> a hard cap; in a bulk job it is
#     not worth blocking an hour per call, so treat as non-retryable.
# Order matters: a per-minute message ("频率超限(200次/分钟)") also contains 超限, so
# the minute check (any mention of 分钟) must run before the quota check.
_MINUTE_MARKERS = ("分钟", "every minute", "per minute")
_PERMISSION_MARKERS = ("没有接口", "访问权限", "没有访问")
_QUOTA_MARKERS = ("次/小时", "次/天", "每天", "每日", "小时", "超限")


class TushareError(RuntimeError):
    """A non-zero ``code`` returned by the Tushare API."""

    def __init__(self, code: int, msg: str, api: str) -> None:
        super().__init__(f"tushare api={api} code={code}: {msg}")
        self.code = code
        self.msg = msg
        self.api = api


class TusharePermissionError(TushareError):
    """The token has no permission for ``api``. Not retryable — skip the endpoint."""


class TushareQuotaError(TushareError):
    """A per-hour/day quota for ``api`` is exhausted. Not retryable in a bulk run."""


class RateLimiter:
    """Reserve evenly-spaced call slots so N threads never exceed a rate.

    Each :meth:`acquire` reserves the next slot under a lock (advancing the
    cursor) and then sleeps *outside* the lock until that slot's time, so the
    lock is never held across a sleep.
    """

    def __init__(self, calls_per_min: int) -> None:
        self._min_interval = 60.0 / max(1, calls_per_min)
        self._lock = threading.Lock()
        self._next = 0.0

    def acquire(self) -> None:
        with self._lock:
            start = max(time.monotonic(), self._next)
            self._next = start + self._min_interval
        wait = start - time.monotonic()
        if wait > 0:
            time.sleep(wait)


class TushareClient:
    """Rate-limited, retrying Tushare Pro HTTP client returning polars frames."""

    def __init__(
        self,
        token: str,
        *,
        calls_per_min: int = 380,
        timeout: float = 60.0,
        max_retries: int = 6,
    ) -> None:
        if not token:
            raise ValueError("TushareClient requires a non-empty token")
        self._token = token
        self._timeout = timeout
        self._max_retries = max_retries
        self._limiter = RateLimiter(calls_per_min)

    # -- low-level request -------------------------------------------------

    def _post(self, api: str, params: dict, fields: str) -> dict:
        body = json.dumps(
            {"api_name": api, "token": self._token, "params": params, "fields": fields}
        ).encode("utf-8")
        req = urllib.request.Request(
            API_URL, data=body, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=self._timeout) as resp:
            return json.loads(resp.read())

    def call(
        self,
        api: str,
        params: dict | None = None,
        fields: str | list[str] | None = None,
    ) -> pl.DataFrame:
        """One API call → polars DataFrame. Retries throttles; raises on error."""
        params = dict(params or {})
        if isinstance(fields, (list, tuple)):
            fields = ",".join(fields)
        fields = fields or ""

        attempt = 0
        while True:
            self._limiter.acquire()
            try:
                payload = self._post(api, params, fields)
            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                attempt += 1
                if attempt > self._max_retries:
                    raise TushareError(-1, f"network error: {exc}", api) from exc
                time.sleep(min(2 ** attempt, 30))
                continue

            code = payload.get("code", 0)
            if code == 0:
                return self._to_frame(payload.get("data") or {})

            msg = payload.get("msg") or ""
            # Per-minute throttle: transient — back off and retry. Checked first
            # because its message can also mention 权限/接口.
            if any(m in msg for m in _MINUTE_MARKERS):
                attempt += 1
                if attempt > self._max_retries:
                    raise TushareError(code, msg, api)
                time.sleep(min(15 * attempt, 60))
                continue
            if any(m in msg for m in _PERMISSION_MARKERS):
                raise TusharePermissionError(code, msg, api)
            if any(m in msg for m in _QUOTA_MARKERS):
                raise TushareQuotaError(code, msg, api)
            # Other server-side errors: a couple of retries, then give up.
            attempt += 1
            if attempt > self._max_retries:
                raise TushareError(code, msg, api)
            time.sleep(min(2 ** attempt, 20))

    def call_paged(
        self,
        api: str,
        params: dict | None = None,
        fields: str | list[str] | None = None,
        *,
        page_limit: int = DEFAULT_PAGE_LIMIT,
        max_pages: int = 50,
    ) -> pl.DataFrame:
        """Walk ``offset``/``limit`` until a short page, concatenating results.

        Guards against silent truncation at the server's per-call row cap. A
        short (or empty) page ends the walk; ``max_pages`` is a runaway backstop.
        """
        frames: list[pl.DataFrame] = []
        offset = 0
        for _ in range(max_pages):
            page = self.call(
                api,
                {**(params or {}), "offset": offset, "limit": page_limit},
                fields,
            )
            if page.height:
                frames.append(page)
            if page.height < page_limit:
                break
            offset += page_limit
        if not frames:
            # Preserve column names even on an empty result.
            return self.call(api, {**(params or {}), "limit": 1}, fields).clear()
        return pl.concat(frames, how="vertical_relaxed")

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _to_frame(data: dict) -> pl.DataFrame:
        cols = list(data.get("fields") or [])
        items = data.get("items") or []
        if not items:
            return pl.DataFrame(schema={c: pl.Utf8 for c in cols})
        # infer_schema_length=None scans *all* rows so a column that is null for
        # the first 100 rows (e.g. delist_date) and a string later doesn't break
        # type inference. Fall back to all-Utf8 if a column is genuinely mixed.
        try:
            return pl.DataFrame(items, schema=cols, orient="row", infer_schema_length=None)
        except pl.exceptions.ComputeError:
            return pl.DataFrame(
                items, schema={c: pl.Utf8 for c in cols}, orient="row"
            )
