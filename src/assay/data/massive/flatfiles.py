"""MASSIVE flat-files (S3) client for US-stock daily aggregates.

Object layout (verified against the live bucket)::

    s3://flatfiles/us_stocks_sip/day_aggs_v1/{YYYY}/{MM}/{YYYY-MM-DD}.csv.gz

Each gzipped CSV has the columns::

    ticker,volume,open,close,high,low,window_start,transactions

``window_start`` is the Unix-nanosecond timestamp of the start of the trading
day window (midnight US/Eastern), so the trading date is recovered by converting
to ``America/New_York``.
"""

from __future__ import annotations

import datetime as dt
import gzip
import io
import logging
from dataclasses import dataclass

import boto3
import polars as pl
from botocore.config import Config
from botocore.exceptions import ClientError

from assay.config import MassiveConfig
from assay.data.schemas import DAY_AGG_CSV_COLUMNS

log = logging.getLogger(__name__)


class FlatFilesForbidden(Exception):
    """Raised when an object is listable but GetObject returns 403.

    MASSIVE entitles flat-file *downloads* on a rolling window (the bucket can be
    *listed* further back than it can be *fetched*), so a 403 means the date is
    outside the current subscription rather than a credential failure.
    """

    def __init__(self, key: str):
        super().__init__(f"403 Forbidden (outside subscription window): {key}")
        self.key = key


@dataclass(frozen=True)
class DayAggFile:
    """A located day-aggregate object: its trading date and S3 key."""

    date: dt.date
    key: str


class FlatFilesClient:
    """Thin wrapper over an S3-compatible client pointed at MASSIVE flat files."""

    def __init__(self, config: MassiveConfig):
        self.config = config
        self._s3 = boto3.client(
            "s3",
            endpoint_url=config.s3_endpoint,
            aws_access_key_id=config.s3_access_key_id,
            aws_secret_access_key=config.s3_secret_access_key,
            region_name="us-east-1",
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": "path"},
                retries={"max_attempts": 5, "mode": "standard"},
            ),
        )

    # -- key helpers ----------------------------------------------------------
    def day_agg_key(self, date: dt.date) -> str:
        return f"{self.config.day_aggs_prefix}/{date:%Y}/{date:%m}/{date:%Y-%m-%d}.csv.gz"

    def _month_prefix(self, year: int, month: int) -> str:
        return f"{self.config.day_aggs_prefix}/{year:04d}/{month:02d}/"

    # -- discovery ------------------------------------------------------------
    def list_top_level_prefixes(self) -> list[str]:
        """List the top-level dataset prefixes in the bucket (sanity/discovery)."""
        resp = self._s3.list_objects_v2(
            Bucket=self.config.s3_bucket, Delimiter="/", MaxKeys=100
        )
        return [cp["Prefix"] for cp in resp.get("CommonPrefixes", [])]

    def list_day_aggs(self, start: dt.date, end: dt.date) -> list[DayAggFile]:
        """List day-aggregate objects whose trading date falls in ``[start, end]``.

        Listing the actual objects (rather than probing each calendar date) means
        holidays/non-trading days simply don't appear — no guessing required.
        """
        found: list[DayAggFile] = []
        year, month = start.year, start.month
        while (year, month) <= (end.year, end.month):
            token = None
            prefix = self._month_prefix(year, month)
            while True:
                kwargs = {"Bucket": self.config.s3_bucket, "Prefix": prefix, "MaxKeys": 1000}
                if token:
                    kwargs["ContinuationToken"] = token
                resp = self._s3.list_objects_v2(**kwargs)
                for obj in resp.get("Contents", []):
                    key = obj["Key"]
                    date = self._date_from_key(key)
                    if date is not None and start <= date <= end:
                        found.append(DayAggFile(date=date, key=key))
                if resp.get("IsTruncated"):
                    token = resp.get("NextContinuationToken")
                else:
                    break
            month += 1
            if month > 12:
                month, year = 1, year + 1
        found.sort(key=lambda f: f.date)
        return found

    @staticmethod
    def _date_from_key(key: str) -> dt.date | None:
        name = key.rsplit("/", 1)[-1]
        if not name.endswith(".csv.gz"):
            return None
        stem = name[: -len(".csv.gz")]
        try:
            return dt.date.fromisoformat(stem)
        except ValueError:
            return None

    # -- download -------------------------------------------------------------
    def download_raw(self, key: str) -> bytes | None:
        """Return decompressed CSV bytes for an object key, or ``None`` if absent."""
        try:
            obj = self._s3.get_object(Bucket=self.config.s3_bucket, Key=key)
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if code in ("NoSuchKey", "404", "NoSuchBucket") or status == 404:
                log.warning("flat file not found: %s", key)
                return None
            if code in ("403", "AccessDenied", "Forbidden") or status == 403:
                raise FlatFilesForbidden(key) from exc
            raise
        return gzip.decompress(obj["Body"].read())

    def is_accessible(self, date: dt.date) -> bool:
        """True if the day-aggregate for ``date`` can be downloaded (entitled)."""
        try:
            self._s3.head_object(Bucket=self.config.s3_bucket, Key=self.day_agg_key(date))
            return True
        except ClientError:
            return False

    def earliest_accessible_date(self, listed: list[DayAggFile]) -> dt.date | None:
        """Binary-search the entitlement boundary over a sorted list of files.

        Assumes accessibility is monotonic in time (a rolling window), which
        matches observed MASSIVE behaviour. Returns the earliest downloadable
        date, or ``None`` if none of ``listed`` are accessible.
        """
        if not listed:
            return None
        lo, hi = 0, len(listed) - 1
        if self.is_accessible(listed[hi].date) is False:
            return None
        result = listed[hi].date
        while lo <= hi:
            mid = (lo + hi) // 2
            if self.is_accessible(listed[mid].date):
                result = listed[mid].date
                hi = mid - 1
            else:
                lo = mid + 1
        return result

    def read_day_agg(
        self, date: dt.date, symbols: set[str] | None = None
    ) -> pl.DataFrame | None:
        """Download and parse one day-aggregate file into a typed polars frame.

        Returns ``None`` when no file exists for ``date`` (e.g. a holiday). When
        ``symbols`` is given, rows are filtered to that set. The returned frame
        keeps the raw provider columns plus a derived ``date`` (ET trading date);
        normalization to the ``price_raw`` schema happens in the ingester.
        """
        raw = self.download_raw(self.day_agg_key(date))
        if raw is None:
            return None

        df = pl.read_csv(
            io.BytesIO(raw),
            columns=list(DAY_AGG_CSV_COLUMNS),
            schema_overrides={
                "ticker": pl.Utf8,
                "volume": pl.Float64,
                "open": pl.Float64,
                "close": pl.Float64,
                "high": pl.Float64,
                "low": pl.Float64,
                "window_start": pl.Int64,
                "transactions": pl.Int64,
            },
        )
        if symbols is not None:
            df = df.filter(pl.col("ticker").is_in(list(symbols)))

        # Derive the ET trading date from the nanosecond window_start.
        df = df.with_columns(
            pl.from_epoch("window_start", time_unit="ns")
            .dt.replace_time_zone("UTC")
            .dt.convert_time_zone("America/New_York")
            .dt.date()
            .alias("date")
        )
        return df
