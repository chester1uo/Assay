"""MASSIVE flat-files S3 downloader (the in-repo Stage-1 for US raw data).

Pulls daily aggregate flat files from the MASSIVE S3-compatible endpoint into the
local RAW mirror, mirroring the bucket layout and converting each ``.csv.gz`` to the
``.parquet`` the ingester (:class:`assay.data.massive.flatfiles.LocalFlatFiles`)
reads::

    {raw_dir}/us_stocks_sip/day_aggs_v1/{YYYY}/{MM}/{YYYY-MM-DD}.parquet

Credentials come from :mod:`assay.config_store` (``massive_s3``). Notes mirroring the
working external downloader: sign with ``s3v4``; the token may not ``HeadObject``, so
we stream with ``get_object`` (never ``download_file``); list per-year prefixes and
filter by the date embedded in the key so weekends/holidays are never requested.

``boto3`` is an optional dependency — imported lazily so the rest of the app (and
tests) keep working without it.
"""

from __future__ import annotations

import datetime as dt
import gzip
import io
import re
from pathlib import Path
from typing import Any, Callable

from assay import config_store

DEFAULT_INDEX = "us_stocks_sip/day_aggs_v1"
_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})\.csv\.gz$")


def _client(s3: dict[str, str]):
    """Build a boto3 S3 client for the MASSIVE endpoint (lazy import of boto3)."""
    try:
        import boto3
        from botocore.config import Config
    except ModuleNotFoundError as exc:  # pragma: no cover - optional dep
        raise RuntimeError("boto3 is required for MASSIVE S3 download — pip install boto3") from exc

    cfg = Config(signature_version="s3v4", retries={"max_attempts": 5, "mode": "standard"},
                 max_pool_connections=16)
    return boto3.client(
        "s3",
        endpoint_url=s3.get("endpoint") or "https://files.massive.com",
        aws_access_key_id=s3.get("access_key_id") or "",
        aws_secret_access_key=s3.get("secret_access_key") or "",
        config=cfg,
    )


def discover_files(client, bucket: str, index: str, start: dt.date, end: dt.date) -> list[tuple[str, int, str]]:
    """Return ``[(key, size, 'YYYY-MM-DD')]`` for ``index`` files within ``[start, end]``.

    Lists per-year prefixes to keep each listing small and filters on the date in the
    key (so non-trading days are never requested).
    """
    found: list[tuple[str, int, str]] = []
    pag = client.get_paginator("list_objects_v2")
    for year in range(start.year, end.year + 1):
        prefix = f"{index}/{year}/"
        for page in pag.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                m = _DATE_RE.search(obj["Key"])
                if not m:
                    continue
                d = dt.date.fromisoformat(m.group(1))
                if start <= d <= end:
                    found.append((obj["Key"], int(obj.get("Size", 0)), m.group(1)))
    found.sort(key=lambda t: t[2])
    return found


def _convert_to_parquet(gz_bytes: bytes, target: Path) -> None:
    """Gzipped CSV bytes -> parquet at ``target`` (column headers preserved)."""
    import polars as pl

    data = gzip.decompress(gz_bytes)
    df = pl.read_csv(io.BytesIO(data))
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".part")
    df.write_parquet(tmp, compression="zstd")
    tmp.replace(target)


def download_index(
    raw_dir: str | Path,
    index: str = DEFAULT_INDEX,
    *,
    start: dt.date,
    end: dt.date,
    s3: dict[str, str] | None = None,
    force: bool = False,
    progress: Callable[[float, str], None] | None = None,
) -> dict[str, Any]:
    """Download + convert all ``index`` flat files in ``[start, end]`` to the raw mirror.

    Skips files whose target parquet already exists (unless ``force``). ``progress`` is
    called with ``(fraction, message)`` as files complete. Returns a small summary.
    """
    s3 = s3 or config_store.massive_s3()
    bucket = s3.get("bucket") or "flatfiles"
    raw = Path(raw_dir).expanduser()

    def _say(frac: float, msg: str) -> None:
        if progress:
            progress(frac, msg)

    _say(0.0, f"listing {index} {start}..{end} on s3://{bucket}")
    client = _client(s3)
    files = discover_files(client, bucket, index, start, end)
    total = len(files)
    if total == 0:
        _say(1.0, "no files found in range")
        return {"index": index, "found": 0, "downloaded": 0, "skipped": 0, "bytes": 0}

    downloaded = skipped = nbytes = 0
    for i, (key, size, datestr) in enumerate(files, 1):
        target = raw / key[: -len(".csv.gz")]  # .../date.csv.gz -> .../date
        target = target.with_suffix(".parquet")
        if not force and target.exists() and target.stat().st_size > 0:
            skipped += 1
        else:
            resp = client.get_object(Bucket=bucket, Key=key)
            body = resp["Body"]
            try:
                gz = body.read()
            finally:
                body.close()
            _convert_to_parquet(gz, target)
            downloaded += 1
            nbytes += len(gz)
        if i % 5 == 0 or i == total:
            _say(i / total, f"{index}: {i}/{total} ({datestr}) — {downloaded} new, {skipped} cached")
    return {"index": index, "found": total, "downloaded": downloaded, "skipped": skipped, "bytes": nbytes}
