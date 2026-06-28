# Tushare data source (China A-share + Hong Kong)

Raw market-data mirror for Chinese A-share and Hong Kong equities, downloaded
from the [Tushare Pro](https://tushare.pro/) HTTP API. This is the China/HK
analogue of the `massive` (Polygon, US equities) source: it lands **raw**
provider data on disk, which the Assay ingesters can later normalize into the
canonical `price_raw` / `adj_events` / `universe_snapshots` parquet stores
(`market=CN` / `market=HK`).

## Scope

| Market | Universe | Membership history |
|--------|----------|--------------------|
| A-share | union of **CSI300** (`000300.SH`), **CSI500** (`000905.SH`), **CSI1000** (`000852.SH`) constituents | ✅ point-in-time, monthly (`index_weight`) |
| Hong Kong | **HSI** + **Hang Seng TECH** constituents | ⚠️ current snapshot only (see limitations) |

Default date range: **2010-01-01 → today**.

> **CSI300 membership** is stitched from two provider codes: `000300.SH` carries
> weights only from 2016-01, so the SZSE mirror `399300.SZ` supplies the
> 2010-2015 history. They are merged (canonical code wins on overlap) into a
> single `cn/index_weight/CSI300.parquet`. CSI500/CSI1000 need only one code each
> (CSI1000's series starts at its 2014-10 inception).

## Running

```bash
# from the repo root, with the project venv
TUSHARE_TOKEN=xxxx PYTHONPATH=src \
  /data/haoluo/_assay_venv/bin/python scripts/download_tushare.py
```

The job is **resumable**: each per-symbol / per-index parquet is skipped if it
already exists, so re-running the same command continues an interrupted backfill.
Pass `--force` to overwrite. Useful flags:

- `--markets cn,hk` — pick markets (or use `--steps` for fine control)
- `--steps cn_universe,cn_prices,...` — explicit step list (see `ALL_STEPS`)
- `--start 20100101 --end 20260627` — date window
- `--rate 380 --workers 8` — API calls/min budget and concurrency
- `--limit-symbols N` — cap the universe for a quick smoke test

## On-disk layout (`$TUSHARE_DATA_DIR`, default `/data/tushare_data`)

```
_manifest.json                       run config, row counts, failures, history
meta/
  stock_basic.parquet                A-share listings (L + D + P) — names, list/delist dates
  hk_basic.parquet                   HK listings
  trade_cal.parquet                  SSE trading calendar
cn/
  index_weight/CSI300.parquet        monthly constituent + weight history ...
  index_weight/CSI500.parquet
  index_weight/CSI1000.parquet
  universe.parquet                   deduped union (ts_code, indices, first_date, last_date)
  daily/{ts_code}.parquet            raw OHLCV (unadjusted): open/high/low/close/pre_close/vol/amount
  adj_factor/{ts_code}.parquet       split/merge adjustment factor (for qfq/hfq)
  daily_basic/{ts_code}.parquet      PE/PB/PS/turnover/total_mv/circ_mv/total_share/float_share/...
  dividend/{ts_code}.parquet         cash + stock dividends and split ratios
hk/
  index_global/HSI.parquet           index value series
  index_global/HSTECH.parquet
  constituents.parquet               current-snapshot HSI/HSTECH membership (ts_code, index, name)
  daily/{ts_code}.parquet            raw HK OHLCV (unadjusted)
logs/
  failures_*.txt                     per-step symbol failures (if any)
```

Files are zstd-compressed parquet (project convention). Columns are the **raw
Tushare fields**, unmodified, for fidelity — adjustment/normalization happens
downstream in the Assay ingest layer, not here.

## Adjustment (splits / dividends / merges)

A-share prices in `cn/daily` are **unadjusted**. To build forward/back-adjusted
series, combine them with `cn/adj_factor` (Tushare's cumulative factor) or derive
events from `cn/dividend` (`stk_div`, `stk_bo_rate`, `stk_co_rate` for
splits/bonus, `cash_div` for cash). This matches Assay's `adj_events` model,
which recomputes the cumulative factor from primitive events rather than trusting
a provider's cumulative number.

## Known limitations

1. **HK stock-level prices are effectively undownloadable on this token.** The
   `hk_daily` interface is quota-capped to a few calls/day (Tushare returns code
   40203 `频率超限 ... 5次/天`), so bulk-pulling ~95 HK constituents is not
   feasible — `hk/daily` is left empty and the step aborts cleanly. The HK
   deliverable is therefore the **index value series** (`hk/index_global`) plus
   the **constituents table** only. Per-stock HK history needs a higher Tushare
   points tier (or a different HK source). Relatedly, `hk_adjfactor` /
   `hk_daily_adj` are **not permissioned** either, so any HK prices would be
   unadjusted regardless.

2. **HK index membership is a current snapshot, not history.** Tushare exposes
   **no** index-membership interface for HSI or Hang Seng TECH
   (`index_member` / `index_basic market=HK` return nothing). The HK universe is
   therefore a single **current** constituent list vendored in
   `src/assay/data/tushare/constituents.py` (captured 2026-06-27). Backtests over
   the HK universe using this list are **survivorship-biased**. Replace those
   lists with dated membership if a historical source becomes available — the
   A-share path (`index_weight`) is already point-in-time correct.

3. **Hang Seng TECH index code.** On `index_global` the Hang Seng TECH value
   series is `HKTECH` (not `HSTECH`, which returns nothing). The friendly name
   `HSTECH` is used for the on-disk file and constituent list.
