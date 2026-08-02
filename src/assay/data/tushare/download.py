"""Bulk, resumable Tushare backfill for Chinese A-share + Hong Kong markets.

Lands *raw* provider data under a local mirror (default ``/data/tushare_data``):

    {root}/
      _manifest.json                     run config, counts, failures
      meta/stock_basic.parquet           A-share listings (L + D + P)
      meta/hk_basic.parquet              HK listings
      meta/trade_cal.parquet             SSE trading calendar
      cn/index_weight/{INDEX}.parquet    monthly membership history (CSI300/500/1000)
      cn/universe.parquet                deduped union of all index members
      cn/daily/{ts_code}.parquet         raw OHLCV (unadjusted)
      cn/adj_factor/{ts_code}.parquet    split/merge adjustment factor
      cn/daily_basic/{ts_code}.parquet   valuation: PE/PB/turnover/total_mv/...
      cn/dividend/{ts_code}.parquet      cash + stock dividends / splits
      meta/hk_trade_cal.parquet          HKEX trading calendar
      hk/index_global/{HSI,HSTECH}.parquet   index value series
      hk/constituents.parquet            current-snapshot HSI/HSTECH membership
      hk/daily/{YYYY}/{YYYYMMDD}.parquet      raw HK OHLCV, whole market per date
      hk/adj_factor/{YYYY}/{YYYYMMDD}.parquet cumulative adjustment factor
      hk/daily_adj/{YYYY}/{YYYYMMDD}.parquet  market cap / shares / turnover

CN is mirrored **per symbol** (its universe is known up front from index
membership); HK is mirrored **per trade date**, because Tushare exposes no HK
membership API — the tradable universe has to be discovered from whole-market
daily responses, which also makes the HK price mirror survivorship-free.

Resume is by file existence: a per-symbol / per-date / per-index parquet that
already exists is skipped unless ``force=True``. So an interrupted run is
restarted simply by re-running the same command.

Known limitations (Tushare scope):
  * HK index membership is a **current snapshot only** (Tushare has no HK
    membership API) — see :mod:`assay.data.tushare.constituents`. Prefer the
    liquidity-ranked universe built by the ingest layer, which is point-in-time.
  * The HK price endpoints are not permissioned on a plain Tushare token; they
    require an entitled endpoint (see ``$TUSHARE_API_URL`` in
    :mod:`assay.data.tushare.client`).
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

import polars as pl

from ..io_utils import write_parquet_atomic
from .client import (
    TushareClient,
    TusharePermissionError,
    TushareQuotaError,
    default_base_url,
)
from .constituents import CN_INDEX_CODES, HK_INDEX_CODES, hk_constituents

log = logging.getLogger("assay.tushare.download")

DEFAULT_DATA_DIR = "/data/tushare_data"
DEFAULT_START = "20100101"

# All work steps, in dependency order. ``cn_universe`` must precede the per-symbol
# CN steps because it produces the symbol list they iterate.
ALL_STEPS = (
    "meta",
    "cn_universe",
    "cn_prices",
    "cn_adj",
    "cn_basic",
    "cn_dividend",
    "cn_limit",
    "hk_index",
    "hk_prices",
    "hk_adj",
    "hk_valuation",
)
CN_SYMBOL_STEPS = ("cn_prices", "cn_adj", "cn_basic", "cn_dividend", "cn_limit")
HK_BYDATE_STEPS = ("hk_prices", "hk_adj", "hk_valuation")


def today_yyyymmdd() -> str:
    return dt.date.today().strftime("%Y%m%d")


@dataclass
class TushareDownloadConfig:
    """Where to write, what date range, and how hard to push the API."""

    token: str
    api_url: str = ""
    data_dir: Path = Path(DEFAULT_DATA_DIR)
    start_date: str = DEFAULT_START
    end_date: str = field(default_factory=today_yyyymmdd)
    calls_per_min: int = 380
    max_workers: int = 8
    force: bool = False
    # Incremental update: fetch the date window BY trade_date (all symbols per call)
    # and append to the per-symbol files, instead of the per-symbol full-history pull.
    # ~n_dates × n_endpoints calls instead of n_symbols × n_endpoints — and it actually
    # extends existing files (the per-symbol path skips them by resume-on-existence).
    incremental: bool = False
    # Cap the per-market symbol universe for a quick smoke test (0 = no cap).
    limit_symbols: int = 0

    def __post_init__(self) -> None:
        self.data_dir = Path(self.data_dir).expanduser()

    @classmethod
    def from_env(cls, **overrides) -> "TushareDownloadConfig":
        token = overrides.pop("token", None) or os.environ.get("TUSHARE_TOKEN", "")
        api_url = overrides.pop("api_url", None) or default_base_url()
        data_dir = overrides.pop("data_dir", None) or os.environ.get(
            "TUSHARE_DATA_DIR", DEFAULT_DATA_DIR
        )
        start = overrides.pop("start_date", None) or os.environ.get(
            "TUSHARE_START", DEFAULT_START
        )
        end = overrides.pop("end_date", None) or os.environ.get(
            "TUSHARE_END", today_yyyymmdd()
        )
        return cls(
            token=token,
            api_url=api_url,
            data_dir=Path(data_dir),
            start_date=start,
            end_date=end,
            **overrides,
        )

    # -- path helpers ------------------------------------------------------

    def p(self, *parts: str) -> Path:
        return self.data_dir.joinpath(*parts)


# ---------------------------------------------------------------------------
# small utilities
# ---------------------------------------------------------------------------


def _month_ranges(start: str, end: str) -> list[tuple[str, str]]:
    """Yield (first_day, last_day) YYYYMMDD windows, one per calendar month."""
    s = dt.datetime.strptime(start, "%Y%m%d").date().replace(day=1)
    e = dt.datetime.strptime(end, "%Y%m%d").date()
    out: list[tuple[str, str]] = []
    cur = s
    while cur <= e:
        if cur.month == 12:
            nxt = cur.replace(year=cur.year + 1, month=1)
        else:
            nxt = cur.replace(month=cur.month + 1)
        last = nxt - dt.timedelta(days=1)
        out.append((cur.strftime("%Y%m%d"), min(last, e).strftime("%Y%m%d")))
        cur = nxt
    return out


def _year_ranges(start: str, end: str) -> list[tuple[str, str]]:
    s = dt.datetime.strptime(start, "%Y%m%d").date()
    e = dt.datetime.strptime(end, "%Y%m%d").date()
    out: list[tuple[str, str]] = []
    for y in range(s.year, e.year + 1):
        lo = max(s, dt.date(y, 1, 1)).strftime("%Y%m%d")
        hi = min(e, dt.date(y, 12, 31)).strftime("%Y%m%d")
        out.append((lo, hi))
    return out


def _exists_nonempty(path: Path) -> bool:
    return path.is_file() and path.stat().st_size > 0


def _concat_nonempty(frames: list[pl.DataFrame]) -> pl.DataFrame | None:
    """Concatenate the non-empty frames, or ``None`` if there are none.

    ``pl.concat([])`` raises, and a step that raises something other than a
    permission/quota error escapes ``guard`` and takes the whole run down with
    it — including a CN job that merely shares the ``meta`` step.
    """
    frames = [f for f in frames if f is not None and f.height]
    if not frames:
        return None
    return pl.concat(frames, how="vertical_relaxed")


# ---------------------------------------------------------------------------
# meta
# ---------------------------------------------------------------------------


def run_meta(client: TushareClient, cfg: TushareDownloadConfig) -> dict:
    """stock_basic (L/D/P), hk_basic, and the SSE trade calendar."""
    out: dict[str, int] = {}

    sb_path = cfg.p("meta", "stock_basic.parquet")
    if cfg.force or not _exists_nonempty(sb_path):
        fields = (
            "ts_code,symbol,name,area,industry,fullname,market,exchange,"
            "list_status,list_date,delist_date,is_hs"
        )
        frames = [
            client.call("stock_basic", {"list_status": s}, fields)
            for s in ("L", "D", "P")
        ]
        df = _concat_nonempty(frames)
        if df is not None:
            write_parquet_atomic(df, sb_path)
            out["stock_basic"] = df.height
            log.info("meta stock_basic: %d rows", df.height)

    cal_path = cfg.p("meta", "trade_cal.parquet")
    if cfg.force or not _exists_nonempty(cal_path):
        # Per-year to stay well under any per-call row cap (offset paging on
        # trade_cal is not reliable, and a year is < 400 rows).
        frames = [
            client.call("trade_cal", {"exchange": "SSE", "start_date": lo, "end_date": hi})
            for lo, hi in _year_ranges(cfg.start_date, cfg.end_date)
        ]
        df = _concat_nonempty(frames)
        if df is not None:
            write_parquet_atomic(df, cal_path)
            out["trade_cal"] = df.height
            log.info("meta trade_cal: %d rows", df.height)

    # HK last: this step is shared with CN runs, and an endpoint entitled for
    # A-shares but not HK must not cost the caller its SSE calendar.
    hk_path = cfg.p("meta", "hk_basic.parquet")
    if cfg.force or not _exists_nonempty(hk_path):
        # hk_basic defaults to list_status="L"; the delisted set must be asked for
        # explicitly or the listings table silently becomes survivorship-biased.
        try:
            frames = [client.call("hk_basic", {"list_status": s}) for s in ("L", "D")]
        except (TusharePermissionError, TushareQuotaError) as exc:
            log.warning("meta hk_basic unavailable: %s", exc.msg)
            frames = []
        df = _concat_nonempty(frames)
        if df is not None:
            write_parquet_atomic(df, hk_path)
            out["hk_basic"] = df.height
            log.info("meta hk_basic: %d rows", df.height)

    hk_cal_path = cfg.p("meta", "hk_trade_cal.parquet")
    if cfg.force or not _exists_nonempty(hk_cal_path):
        try:
            frames = [
                client.call("hk_tradecal", {"start_date": lo, "end_date": hi})
                for lo, hi in _year_ranges(cfg.start_date, cfg.end_date)
            ]
        except (TusharePermissionError, TushareQuotaError) as exc:
            log.warning("meta hk_trade_cal unavailable: %s", exc.msg)
            frames = []
        df = _concat_nonempty(frames)
        if df is not None:
            write_parquet_atomic(df, hk_cal_path)
            out["hk_trade_cal"] = df.height
            log.info("meta hk_trade_cal: %d rows", df.height)

    return out


# ---------------------------------------------------------------------------
# CN universe (index membership history + union)
# ---------------------------------------------------------------------------


def run_cn_universe(client: TushareClient, cfg: TushareDownloadConfig) -> list[str]:
    """Download monthly ``index_weight`` history per index; build the union.

    Returns the deduplicated, sorted list of A-share ts_codes that were a member
    of CSI300/500/1000 at any point in the range (survivorship-bias-free).
    """
    months = _month_ranges(cfg.start_date, cfg.end_date)
    for name, codes in CN_INDEX_CODES.items():
        path = cfg.p("cn", "index_weight", f"{name}.parquet")
        if not cfg.force and _exists_nonempty(path):
            continue
        primary = codes[0]
        frames: list[pl.DataFrame] = []
        # Single call() per month (no offset paging): a month of even daily-
        # granularity weights (~300 names x ~23 days < 7000) fits one call, and
        # index_weight offset paging is unreliable. Codes are queried in order so
        # the canonical (primary) code wins on any (con_code, trade_date) overlap.
        for code in codes:
            for lo, hi in months:
                page = client.call(
                    "index_weight", {"index_code": code, "start_date": lo, "end_date": hi}
                )
                if page.height:
                    frames.append(page)
        if frames:
            df = pl.concat(frames, how="vertical_relaxed")
            df = df.unique(subset=["con_code", "trade_date"], keep="first")
            # Normalize to the canonical code so a merged index reads as one series.
            df = df.with_columns(pl.lit(primary).alias("index_code"))
            df = df.sort(["trade_date", "con_code"])
        else:
            df = pl.DataFrame(schema={c: pl.Utf8 for c in ("index_code", "con_code", "trade_date", "weight")})
        write_parquet_atomic(df, path)
        log.info("cn_universe %s: %d weight rows (codes=%s)", name, df.height, ",".join(codes))

    # Build the union table from the three per-index files.
    members: dict[str, dict] = {}
    for name in CN_INDEX_CODES:
        path = cfg.p("cn", "index_weight", f"{name}.parquet")
        if not _exists_nonempty(path):
            continue
        df = pl.read_parquet(path)
        if not df.height:
            continue
        agg = df.group_by("con_code").agg(
            pl.col("trade_date").min().alias("first_date"),
            pl.col("trade_date").max().alias("last_date"),
            pl.len().alias("n_snapshots"),
        )
        for row in agg.iter_rows(named=True):
            code = row["con_code"]
            m = members.setdefault(
                code,
                {"ts_code": code, "indices": set(), "first_date": row["first_date"], "last_date": row["last_date"]},
            )
            m["indices"].add(name)
            m["first_date"] = min(m["first_date"], row["first_date"])
            m["last_date"] = max(m["last_date"], row["last_date"])

    union = sorted(members)
    if members:
        union_df = pl.DataFrame(
            [
                {
                    "ts_code": m["ts_code"],
                    "indices": ",".join(sorted(m["indices"])),
                    "first_date": m["first_date"],
                    "last_date": m["last_date"],
                }
                for m in (members[c] for c in union)
            ]
        )
        write_parquet_atomic(union_df, cfg.p("cn", "universe.parquet"))
    log.info("cn_universe union: %d unique A-share symbols", len(union))
    return union


# ---------------------------------------------------------------------------
# per-symbol fetch (CN + HK)
# ---------------------------------------------------------------------------


def _fetch_symbol_endpoint(
    client: TushareClient,
    path: Path,
    api: str,
    params: dict,
    *,
    force: bool,
    paged: bool = True,
) -> str:
    """Fetch one (symbol, endpoint) to ``path``. Returns 'skip'|'ok'|'empty'."""
    if not force and _exists_nonempty(path):
        return "skip"
    df = client.call_paged(api, params) if paged else client.call(api, params)
    write_parquet_atomic(df, path)
    return "ok" if df.height else "empty"


def _run_symbol_pool(
    client: TushareClient,
    cfg: TushareDownloadConfig,
    symbols: list[str],
    jobs: list[tuple[str, str, dict]],
    label: str,
    progress=None,
) -> dict:
    """Run (api, subdir, extra_params) jobs across ``symbols`` in a thread pool.

    ``jobs`` is a list of (api, subdir, extra_params) where the per-symbol path
    is ``{market_dir}/{subdir}/{ts_code}.parquet`` and ``ts_code`` is added to
    params. ``label`` is the on-disk market dir ('cn' or 'hk').
    """
    if cfg.limit_symbols:
        symbols = symbols[: cfg.limit_symbols]
    total = len(symbols)
    counts = {"ok": 0, "empty": 0, "skip": 0, "error": 0}
    failures: list[str] = []
    done = 0
    aborted: str | None = None

    def work(sym: str) -> tuple[str, dict, tuple[str, str] | None]:
        res: dict[str, str] = {}
        for api, subdir, extra in jobs:
            path = cfg.p(label, subdir, f"{sym}.parquet")
            params = {"ts_code": sym, **extra}
            try:
                res[subdir] = _fetch_symbol_endpoint(client, path, api, params, force=cfg.force)
            except (TusharePermissionError, TushareQuotaError) as exc:
                # Endpoint-wide condition, not a per-symbol hiccup: abort the step.
                return sym, res, ("abort", f"{api}: {exc.msg}")
            except Exception as exc:  # noqa: BLE001 — record and continue
                return sym, res, ("error", f"{subdir}: {type(exc).__name__}: {exc}")
        return sym, res, None

    with ThreadPoolExecutor(max_workers=cfg.max_workers) as pool:
        futs = {pool.submit(work, s): s for s in symbols}
        for fut in as_completed(futs):
            done += 1
            sym, res, err = fut.result()
            if err and err[0] == "abort":
                aborted = err[1]
                for f in futs:
                    f.cancel()
                break
            if err:
                counts["error"] += 1
                failures.append(f"{sym}\t{err[1]}")
            else:
                for status in res.values():
                    counts[status] = counts.get(status, 0) + 1
            if done % 100 == 0 or done == total:
                log.info(
                    "%s %d/%d symbols (ok=%d empty=%d skip=%d err=%d)",
                    label, done, total, counts["ok"], counts["empty"], counts["skip"], counts["error"],
                )
                if progress:
                    progress(done / total, f"{label} {jobs[0][1]}…: {done}/{total} symbols "
                                           f"({counts['ok']} new, {counts['skip']} cached)")

    if aborted:
        counts["aborted"] = aborted
        log.warning("%s: step aborted — endpoint unavailable/quota: %s", label, aborted)
    if failures:
        fpath = cfg.p("logs", f"failures_{label}_{jobs[0][1]}.txt")
        write_text(fpath, "\n".join(failures))
        log.warning("%s: %d symbol failures -> %s", label, len(failures), fpath)
    counts["symbols"] = total
    return counts


def run_cn_symbols(
    client: TushareClient,
    cfg: TushareDownloadConfig,
    symbols: list[str],
    steps: set[str],
    progress=None,
) -> dict:
    """Per-symbol CN price / adj / valuation / dividend, for the selected steps."""
    jobs: list[tuple[str, str, dict]] = []
    date = {"start_date": cfg.start_date, "end_date": cfg.end_date}
    if "cn_prices" in steps:
        jobs.append(("daily", "daily", date))
    if "cn_adj" in steps:
        jobs.append(("adj_factor", "adj_factor", date))
    if "cn_basic" in steps:
        jobs.append(("daily_basic", "daily_basic", date))
    if "cn_dividend" in steps:
        jobs.append(("dividend", "dividend", {}))  # full history; no date filter
    if "cn_limit" in steps:
        # Daily up/down price-limit bands (涨跌停价) — authoritative per-board limits
        # (10%/20%/30%/ST 5%), used for A-share execution constraints.
        jobs.append(("stk_limit", "stk_limit", date))
    if not jobs:
        return {}
    return _run_symbol_pool(client, cfg, symbols, jobs, "cn", progress=progress)


# ---------------------------------------------------------------------------
# incremental CN update — fetch BY trade_date, append to per-symbol files
# ---------------------------------------------------------------------------

# (step, subdir, api, date_param) for the endpoints that support a bulk by-date pull.
_CN_BYDATE = (
    ("cn_prices", "daily", "daily", "trade_date"),
    ("cn_adj", "adj_factor", "adj_factor", "trade_date"),
    ("cn_basic", "daily_basic", "daily_basic", "trade_date"),
    ("cn_limit", "stk_limit", "stk_limit", "trade_date"),
    ("cn_dividend", "dividend", "dividend", "ex_date"),
)


def _open_trade_dates(client: TushareClient, cfg: TushareDownloadConfig) -> list[str]:
    """Open SSE trading dates (YYYYMMDD, ascending) in ``[start, end]``."""
    df = client.call(
        "trade_cal",
        {"exchange": "SSE", "start_date": cfg.start_date, "end_date": cfg.end_date},
        "cal_date,is_open",
    )
    if not df.height:
        return []
    open_df = df.filter(pl.col("is_open").cast(pl.Utf8).is_in(["1", "1.0"]))
    return sorted(open_df["cal_date"].cast(pl.Utf8).to_list())


def _append_symbol_parquet(path: Path, new_df) -> None:
    """Merge ``new_df`` into the per-symbol parquet, de-duped and sorted."""
    if _exists_nonempty(path):
        try:
            old = pl.read_parquet(path)
            merged = pl.concat([old, new_df], how="diagonal_relaxed")
        except Exception:  # noqa: BLE001 — a corrupt prior file: replace rather than fail
            merged = new_df
    else:
        merged = new_df
    if "trade_date" in merged.columns:
        merged = merged.unique(subset=["trade_date"], keep="last").sort("trade_date")
    else:  # dividend etc. — no trade_date; dedup on the whole row
        merged = merged.unique(keep="last")
    write_parquet_atomic(merged, path)


def run_cn_by_date(
    client: TushareClient,
    cfg: TushareDownloadConfig,
    symbols: list[str],
    steps: set[str],
    progress=None,
) -> dict:
    """Incremental CN update: pull each endpoint by trade_date and append per symbol.

    Only the ``symbols`` universe is touched (by-date responses cover the whole
    market). ``progress(frac, msg)`` is called as ``(endpoint, date)`` advance.
    """
    endpoints = [(subdir, api, dparam) for (step, subdir, api, dparam) in _CN_BYDATE if step in steps]
    if not endpoints:
        return {}
    if cfg.limit_symbols:
        symbols = symbols[: cfg.limit_symbols]
    sym_set = set(symbols)
    dates = _open_trade_dates(client, cfg)
    counts = {"dates": len(dates), "calls": 0, "rows": 0, "symbols_touched": 0, "by_store": {}}
    if not dates:
        return counts

    total = max(1, len(endpoints) * len(dates))
    step_i = 0
    for subdir, api, dparam in endpoints:
        buf: dict[str, list] = {}
        for d in dates:
            try:
                df = client.call_paged(api, {dparam: d})
            except (TusharePermissionError, TushareQuotaError) as exc:
                log.warning("cn by-date %s unavailable: %s", subdir, exc.msg)
                break
            counts["calls"] += 1
            if df.height and "ts_code" in df.columns:
                df = df.filter(pl.col("ts_code").cast(pl.Utf8).is_in(list(sym_set)))
                for (code,), sub in df.group_by(["ts_code"], maintain_order=True):
                    buf.setdefault(str(code), []).append(sub)
                counts["rows"] += df.height
            step_i += 1
            if progress:
                progress(step_i / total, f"{subdir}: date {dates.index(d) + 1}/{len(dates)} ({d})")
        touched = 0
        for code, frames in buf.items():
            new_df = pl.concat(frames, how="vertical_relaxed") if len(frames) > 1 else frames[0]
            _append_symbol_parquet(cfg.p("cn", subdir, f"{code}.parquet"), new_df)
            touched += 1
        counts["symbols_touched"] += touched
        counts["by_store"][subdir] = {"symbols": touched}
        log.info("cn by-date %s: %d dates → %d symbols appended", subdir, len(dates), touched)
    return counts


# ---------------------------------------------------------------------------
# HK
# ---------------------------------------------------------------------------


def run_hk_index(client: TushareClient, cfg: TushareDownloadConfig) -> dict:
    out: dict[str, int] = {}
    for name, code in HK_INDEX_CODES.items():
        path = cfg.p("hk", "index_global", f"{name}.parquet")
        if not cfg.force and _exists_nonempty(path):
            continue
        df = client.call_paged(
            "index_global", {"ts_code": code, "start_date": cfg.start_date, "end_date": cfg.end_date}
        )
        write_parquet_atomic(df, path)
        out[name] = df.height
        log.info("hk_index %s (%s): %d rows", name, code, df.height)
    return out


def run_hk_constituents(client: TushareClient, cfg: TushareDownloadConfig) -> dict:
    """Write the vendored current-snapshot HSI/HSTECH membership table."""
    cons = pl.DataFrame(hk_constituents(), schema=["ts_code", "index"], orient="row")
    hk_basic_path = cfg.p("meta", "hk_basic.parquet")
    if _exists_nonempty(hk_basic_path):
        names = pl.read_parquet(hk_basic_path).select(["ts_code", "name"]).unique(subset=["ts_code"])
        cons = cons.join(names, on="ts_code", how="left")
    write_parquet_atomic(cons, cfg.p("hk", "constituents.parquet"))
    return {"constituents": cons.height}


# (step, subdir, api) for the HK endpoints pulled one whole-market call per date.
#
# Unlike the CN mirror (per-symbol files, symbol universe known up front) HK is
# stored **per trade date**: there is no HK membership API to derive a symbol list
# from, so the universe has to be discovered from the by-date responses themselves.
# That also makes the mirror survivorship-free — a delisted name simply stops
# appearing after its last trading day instead of never appearing at all.
_HK_BYDATE = (
    ("hk_prices", "daily", "hk_daily"),
    ("hk_adj", "adj_factor", "hk_adjfactor"),
    ("hk_valuation", "daily_adj", "hk_daily_adj"),
)


def _hk_open_dates(client: TushareClient, cfg: TushareDownloadConfig) -> list[str]:
    """Open HKEX trading dates (YYYYMMDD, ascending) in ``[start, end]``."""
    frames = [
        client.call("hk_tradecal", {"start_date": lo, "end_date": hi}, "cal_date,is_open")
        for lo, hi in _year_ranges(cfg.start_date, cfg.end_date)
    ]
    frames = [f for f in frames if f.height]
    if not frames:
        return []
    df = pl.concat(frames, how="vertical_relaxed")
    open_df = df.filter(pl.col("is_open").cast(pl.Utf8).is_in(["1", "1.0"]))
    return sorted(open_df["cal_date"].cast(pl.Utf8).to_list())


def hk_date_path(cfg: TushareDownloadConfig, subdir: str, date: str) -> Path:
    """Year-sharded so no single directory holds ~4k files."""
    return cfg.p("hk", subdir, date[:4], f"{date}.parquet")


def run_hk_by_date(
    client: TushareClient,
    cfg: TushareDownloadConfig,
    steps: set[str],
    progress=None,
) -> dict:
    """Pull each HK endpoint one whole-market call per trading date.

    Resume is per (endpoint, date) file, so an interrupted multi-hour backfill
    restarts where it stopped. The same routine serves the incremental update:
    already-present dates are skipped and only the new tail costs calls.
    """
    endpoints = [(subdir, api) for (step, subdir, api) in _HK_BYDATE if step in steps]
    if not endpoints:
        return {}
    dates = _hk_open_dates(client, cfg)
    counts: dict = {"dates": len(dates), "calls": 0, "rows": 0, "by_store": {}}
    if not dates:
        return counts

    total = max(1, len(endpoints) * len(dates))
    step_i = 0
    for subdir, api in endpoints:
        wrote = rows = skipped = pending = 0
        for i, d in enumerate(dates):
            step_i += 1
            path = hk_date_path(cfg, subdir, d)
            # The newest date is always re-fetched: an update run that lands before
            # the exchange has published the full session would otherwise freeze a
            # partial bar set behind resume-on-existence, and nothing would ever
            # revisit it (unlike the CN path, which merges and self-heals).
            is_tail = i == len(dates) - 1
            if not cfg.force and not is_tail and _exists_nonempty(path):
                skipped += 1
                continue
            try:
                df = client.call_paged(api, {"trade_date": d})
            except (TusharePermissionError, TushareQuotaError) as exc:
                log.warning("hk by-date %s unavailable: %s", subdir, exc.msg)
                counts["by_store"][subdir] = {"unavailable": exc.msg, "dates": wrote}
                break
            counts["calls"] += 1
            # hk_daily_adj pads its response with rows carrying only an adj_factor
            # and a null ts_code — real quotes are the ones with a symbol.
            if "ts_code" in df.columns:
                df = df.filter(pl.col("ts_code").is_not_null())
            if df.height == 0:
                # An open trading date with no quotes means "not published yet".
                # Persisting that would cache the hole forever.
                pending += 1
                continue
            write_parquet_atomic(df, path)
            wrote += 1
            rows += df.height
            if progress:
                progress(step_i / total, f"hk {subdir}: {i + 1}/{len(dates)} ({d})")
        else:
            counts["by_store"][subdir] = {
                "dates": wrote, "skipped": skipped, "rows": rows, "pending": pending
            }
        counts["rows"] += rows
        log.info("hk by-date %s: %d dates written, %d skipped, %d unpublished, %d rows",
                 subdir, wrote, skipped, pending, rows)
    return counts


# ---------------------------------------------------------------------------
# orchestration
# ---------------------------------------------------------------------------


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def run_download(cfg: TushareDownloadConfig, steps: set[str] | None = None, progress=None) -> dict:
    """Execute the selected steps and write the run manifest. Returns the manifest.

    ``progress(frac, msg)`` (optional) is called with overall download progress in
    ``[0, 1]`` so a UI can show a moving bar + which step/date is downloading.
    """
    steps = set(steps) if steps else set(ALL_STEPS)
    client = TushareClient(cfg.token, base_url=cfg.api_url or None, calls_per_min=cfg.calls_per_min)

    def _say(frac: float, msg: str) -> None:
        if progress:
            progress(max(0.0, min(1.0, frac)), msg)
    started = dt.datetime.now()
    manifest: dict = {
        "started": started.isoformat(timespec="seconds"),
        "data_dir": str(cfg.data_dir),
        "date_range": [cfg.start_date, cfg.end_date],
        "api_url": cfg.api_url or default_base_url(),
        "calls_per_min": cfg.calls_per_min,
        "max_workers": cfg.max_workers,
        "steps": sorted(steps),
        "results": {},
        "notes": [
            "HK index membership is a CURRENT snapshot only (no Tushare HK membership API); survivorship-biased.",
            "HK prices are stored per trade date (whole market), so the price mirror itself is survivorship-free.",
        ],
    }

    def guard(step: str, fn):
        """Run a step; record (don't raise) if its endpoint is blocked by the token."""
        try:
            return fn()
        except (TusharePermissionError, TushareQuotaError) as exc:
            log.warning("step %s unavailable on this token: %s", step, exc.msg)
            manifest["results"][step] = {"unavailable": f"{exc.api}: {exc.msg}"}
            return None

    if "meta" in steps:
        log.info("=== step: meta ===")
        _say(0.02, "meta (stock list, calendar) …")
        res = guard("meta", lambda: run_meta(client, cfg))
        if res is not None:
            manifest["results"]["meta"] = res

    cn_symbols: list[str] = []
    if "cn_universe" in steps or steps & set(CN_SYMBOL_STEPS):
        log.info("=== step: cn_universe ===")
        _say(0.06, "cn universe (index membership) …")
        cn_symbols = guard("cn_universe", lambda: run_cn_universe(client, cfg)) or []
        manifest["results"].setdefault("cn_universe", {"union_symbols": len(cn_symbols)})

    if steps & set(CN_SYMBOL_STEPS) and cn_symbols:
        cn_prog = lambda f, m: _say(0.10 + 0.80 * f, m)  # noqa: E731 — cn step owns 10%..90%
        if cfg.incremental:
            log.info("=== step: cn by-date incremental (%s) ===", ",".join(sorted(steps & set(CN_SYMBOL_STEPS))))
            manifest["results"]["cn_bydate"] = run_cn_by_date(client, cfg, cn_symbols, steps, progress=cn_prog)
        else:
            log.info("=== step: cn per-symbol (%s) ===", ",".join(sorted(steps & set(CN_SYMBOL_STEPS))))
            manifest["results"]["cn_symbols"] = run_cn_symbols(client, cfg, cn_symbols, steps, progress=cn_prog)

    if "hk_index" in steps:
        log.info("=== step: hk_index ===")
        _say(0.92, "hk index …")
        res = guard("hk_index", lambda: run_hk_index(client, cfg))
        if res is not None:
            manifest["results"]["hk_index"] = res

    if steps & set(HK_BYDATE_STEPS):
        log.info("=== step: hk by-date (%s) ===", ",".join(sorted(steps & set(HK_BYDATE_STEPS))))
        guard("hk_constituents", lambda: run_hk_constituents(client, cfg))
        hk_prog = lambda f, m: _say(0.93 + 0.07 * f, m)  # noqa: E731 — hk step owns 93%..100%
        # Guarded like every other step: _hk_open_dates calls hk_tradecal first, and
        # an endpoint entitled for HK prices but not its calendar must abort cleanly
        # into the manifest rather than kill the run before it is written.
        res = guard("hk_bydate", lambda: run_hk_by_date(client, cfg, steps, progress=hk_prog))
        if res is not None:
            manifest["results"]["hk_bydate"] = res
    _say(1.0, "download complete")

    finished = dt.datetime.now()
    manifest["finished"] = finished.isoformat(timespec="seconds")
    manifest["duration_sec"] = round((finished - started).total_seconds(), 1)
    _merge_manifest(cfg.p("_manifest.json"), manifest)
    log.info("=== done in %.1fs ===", manifest["duration_sec"])
    return manifest


def _merge_manifest(path: Path, manifest: dict) -> None:
    """Persist the manifest, keeping a short history of prior runs."""
    history = []
    if path.is_file():
        try:
            prev = json.loads(path.read_text(encoding="utf-8"))
            history = prev.get("history", [])
            if "started" in prev:
                history.append({k: prev[k] for k in ("started", "finished", "steps", "duration_sec") if k in prev})
        except (json.JSONDecodeError, OSError):
            pass
    manifest["history"] = history[-10:]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
