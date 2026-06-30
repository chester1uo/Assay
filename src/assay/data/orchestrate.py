"""Data pipeline orchestration — status + init/update runs (RAW -> ASSAY).

Ties the building blocks together for the WebUI data manager:

* :func:`status` — per market, the latest date present in the RAW mirror vs the
  latest date ingested into the ASSAY store, so the operator sees how far behind
  each market is and whether they're in sync.
* :func:`run` — one market's pipeline end to end: Stage-1 download (US: in-repo
  boto3 S3 flat-files; CN: Tushare API) then Stage-2 ingest (US:
  ``prepare_nasdaq100``; CN: ``prepare_cn``). Driven by a :class:`assay.data.jobs.Job`
  so progress streams to the UI. ``mode`` is ``"init"`` (full history) or
  ``"update"`` (incremental from the last ingested date).

Dirs + credentials are read from :mod:`assay.config_store`. The serving DataStore
reads parquet live per request, so freshly-ingested data is visible immediately —
no restart needed.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

from assay import config_store

_MARKETS = ("US", "CN")
_INIT_START = {"US": dt.date(2016, 1, 1), "CN": dt.date(2015, 1, 1)}


# --------------------------------------------------------------------------- status
def _assay_latest(assay_dir: str, market: str) -> tuple[dt.date | None, int]:
    """(latest ingested date, distinct trading days) in the ASSAY price_raw store."""
    try:
        import polars as pl

        from assay.data.schemas import price_root

        proot = price_root(Path(assay_dir), market)
        files = sorted(proot.glob("**/price_raw.parquet")) if proot.exists() else []
        if not files:
            return None, 0
        df = pl.read_parquet([str(f) for f in files], columns=["date"])
        if df.is_empty():
            return None, 0
        return df["date"].max(), int(df["date"].n_unique())
    except Exception:
        return None, 0


def _us_raw_latest(raw_massive: str) -> dt.date | None:
    """Latest day-agg date in the MASSIVE mirror (parsed from parquet filenames)."""
    root = Path(raw_massive) / "us_stocks_sip" / "day_aggs_v1"
    if not root.exists():
        return None
    latest: dt.date | None = None
    for p in root.glob("**/*.parquet"):
        try:
            d = dt.date.fromisoformat(p.stem)
        except ValueError:
            continue
        if latest is None or d > latest:
            latest = d
    return latest


def _parse_loose_date(s: str) -> dt.date | None:
    s = str(s or "").strip()
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return dt.datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _cn_raw_latest(raw_tushare: str) -> dt.date | None:
    """Latest date downloaded into the Tushare mirror (from its run manifest)."""
    man = Path(raw_tushare) / "_manifest.json"
    if not man.exists():
        return None
    try:
        m = json.loads(man.read_text(encoding="utf-8"))
        rng = m.get("date_range") or []
        if len(rng) == 2:
            return _parse_loose_date(rng[1])
    except (json.JSONDecodeError, OSError):
        pass
    return None


def status() -> dict[str, Any]:
    """Per-market RAW-vs-ASSAY sync snapshot for the data manager."""
    today = dt.date.today()
    out = []
    for mk in _MARKETS:
        raw_dir = config_store.raw_dir(mk)
        assay_d = config_store.assay_dir(mk)
        raw_latest = _us_raw_latest(raw_dir) if mk == "US" else _cn_raw_latest(raw_dir)
        assay_latest, days = _assay_latest(assay_d, mk)
        behind = None
        if raw_latest and assay_latest:
            behind = (raw_latest - assay_latest).days
        out.append({
            "market": mk,
            "raw_dir": raw_dir,
            "assay_dir": assay_d,
            "raw_latest": raw_latest.isoformat() if raw_latest else None,
            "assay_latest": assay_latest.isoformat() if assay_latest else None,
            "trading_days": days,
            "behind_days": behind,
            "in_sync": bool(assay_latest and raw_latest and assay_latest >= raw_latest),
            "initialized": assay_latest is not None,
        })
    return {"today": today.isoformat(), "markets": out}


def default_range(market: str, mode: str) -> tuple[dt.date, dt.date]:
    """Sensible (start, end) for a wizard run: init = full history, update = incremental."""
    market = market.upper()
    end = dt.date.today()
    if mode == "init":
        return _INIT_START.get(market, dt.date(2015, 1, 1)), end
    # update: resume the day after the last ingested ASSAY date
    assay_latest, _ = _assay_latest(config_store.assay_dir(market), market)
    start = (assay_latest + dt.timedelta(days=1)) if assay_latest else _INIT_START.get(market, dt.date(2015, 1, 1))
    if start > end:
        start = end
    return start, end


# --------------------------------------------------------------------------- run
def run(market: str, mode: str, start: dt.date, end: dt.date, job) -> dict[str, Any]:
    """Run one market's full pipeline (download -> ingest), reporting via ``job``."""
    market = market.upper()
    job.log(f"{market} {mode}: {start.isoformat()} .. {end.isoformat()}")
    if market == "CN":
        return _run_cn(start, end, job)
    if market == "US":
        return _run_us(start, end, job)
    raise ValueError(f"unsupported market {market!r}")


def _run_us(start: dt.date, end: dt.date, job) -> dict[str, Any]:
    raw = config_store.raw_dir("US")
    assay = config_store.assay_dir("US")
    s3 = config_store.massive_s3()
    if not (s3.get("access_key_id") and s3.get("secret_access_key")):
        raise RuntimeError("MASSIVE S3 credentials not configured (set them in the data manager)")

    from assay.data.massive import s3 as s3dl

    job.progress_to(0.03, "downloading MASSIVE day-aggregates from S3 …")
    dl = s3dl.download_index(
        raw, start=start, end=end, s3=s3,
        progress=lambda f, m: job.progress_to(0.03 + 0.50 * f, m),
    )
    job.log(f"download: {dl}")

    job.progress_to(0.55, "ingesting US → ASSAY (NASDAQ100 + SP500: universe, corp-actions, prices) …")
    from assay.config import AssayConfig, MassiveConfig
    from assay.data.pipeline import prepare_us

    cfg = AssayConfig(massive=MassiveConfig(source_dir=Path(raw)), data_dir=Path(assay), market="US")
    rep = prepare_us(cfg, start, end, index_ids=("NASDAQ100", "SP500"))
    job.progress_to(1.0, "US update complete")
    return {"download": dl, "ingest": rep}


def _run_cn(start: dt.date, end: dt.date, job) -> dict[str, Any]:
    raw = config_store.raw_dir("CN")
    assay = config_store.assay_dir("CN")
    token = config_store.tushare_token()
    if not token:
        raise RuntimeError("Tushare token not configured (set it in the data manager)")

    from assay.data.tushare.download import TushareDownloadConfig, run_download

    job.progress_to(0.03, "downloading Tushare raw data …")
    dlcfg = TushareDownloadConfig(
        token=token, data_dir=Path(raw),
        start_date=start.strftime("%Y%m%d"), end_date=end.strftime("%Y%m%d"),
    )
    manifest = run_download(dlcfg)
    steps = sorted((manifest.get("results") or {}).keys())
    job.log(f"download steps: {steps}")

    job.progress_to(0.55, "ingesting CN → ASSAY (universe, prices, adj, limits) …")
    from assay.data.tushare.ingest import prepare_cn

    rep = prepare_cn(assay, start=start, end=end, tushare_data_dir=raw)
    job.progress_to(1.0, "CN update complete")
    return {"download_steps": steps, "ingest": rep}
