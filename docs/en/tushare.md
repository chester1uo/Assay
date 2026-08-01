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
| Hong Kong | **whole market** (every name that traded), ranked into **HK100** | ✅ point-in-time, monthly (turnover-ranked) |
| Hong Kong | **H-shares** (mainland-incorporated issuers), ranked into **HSHARE100** | ✅ point-in-time, monthly (ISIN `CNE` + turnover) |
| Hong Kong | **HSI** + **Hang Seng TECH** constituents | ⚠️ current snapshot only (see limitations) |

Default date range: **2010-01-01 → today**.

> **CSI300 membership** is stitched from two provider codes: `000300.SH` carries
> weights only from 2016-01, so the SZSE mirror `399300.SZ` supplies the
> 2010-2015 history. They are merged (canonical code wins on overlap) into a
> single `cn/index_weight/CSI300.parquet`. CSI500/CSI1000 need only one code each
> (CSI1000's series starts at its 2014-10 inception).

## Endpoint and credentials

By default the token is spent against `https://api.tushare.pro`. Set
`TUSHARE_API_URL` (or **Data Settings → Tushare → API Endpoint** in the WebUI) to
route the same calls through a relay/cache gateway that speaks the identical
`{api_name, params, fields}` protocol. Gateways authenticate on an `X-API-Key`
header rather than the body `token`; the client sends the credential both ways,
so one configuration works against either endpoint.

This matters because **the HK price endpoints are not permissioned on a plain
Tushare token** — `hk_daily`, `hk_adjfactor` and `hk_daily_adj` all need an
entitled endpoint. Without one, only the HK index value series is reachable.

## Running

```bash
# from the repo root, with the project venv
TUSHARE_TOKEN=xxxx TUSHARE_API_URL=https://your-gateway/v1/tushare/query PYTHONPATH=src \
  /data/haoluo/_assay_venv/bin/python scripts/download_tushare.py --markets hk
```

The job is **resumable**: each per-symbol / per-index parquet is skipped if it
already exists, so re-running the same command continues an interrupted backfill.
Pass `--force` to overwrite. Useful flags:

> **Incremental daily refresh.** Because resume skips existing per-symbol files, this
> standalone script only *backfills* — it will not extend a symbol's file with new dates.
> For an incremental update, use the WebUI **Data → Update** job (or `orchestrate.run(...,
> mode="update")`): it fetches the new window **by trade-date** (all symbols per API call)
> via `run_cn_by_date` and **appends** to the raw files — a few calls instead of one per
> symbol. See [Data Pipeline → Data Manager](guide/data-pipeline.md).

- `--markets cn,hk` — pick markets (or use `--steps` for fine control)
- `--steps cn_universe,cn_prices,...` — explicit step list (see `ALL_STEPS`)
- `--start 20100101 --end 20260627` — date window
- `--rate 380 --workers 8` — API calls/min budget and concurrency
- `--api-url URL` — endpoint override (default `$TUSHARE_API_URL`)
- `--limit-symbols N` — cap the universe for a quick smoke test

Set `--rate` to the endpoint's real per-minute ceiling. A gateway that answers
HTTP 429 is telling you the budget is too high; the client waits out the
`Retry-After` rather than backing off exponentially, but a chronically
over-budget rate still costs throughput.

## On-disk layout (`$TUSHARE_DATA_DIR`, default `/data/tushare_data`)

```
_manifest.json                       run config, row counts, failures, history
meta/
  stock_basic.parquet                A-share listings (L + D + P) — names, list/delist dates
  hk_basic.parquet                   HK listings (L + D — delisted included, no survivorship bias)
  trade_cal.parquet                  SSE trading calendar
  hk_trade_cal.parquet               HKEX trading calendar
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
  daily/{YYYY}/{YYYYMMDD}.parquet       raw HK OHLCV (unadjusted), whole market per date
  adj_factor/{YYYY}/{YYYYMMDD}.parquet  cum_adjfactor + close_price, whole market per date
  daily_adj/{YYYY}/{YYYYMMDD}.parquet   total_mv/free_mv/total_share/turnover_ratio per date
logs/
  failures_*.txt                     per-step symbol failures (if any)
```

Files are zstd-compressed parquet (project convention). Columns are the **raw
Tushare fields**, unmodified, for fidelity — adjustment/normalization happens
downstream in the Assay ingest layer, not here.

### Why CN is sharded per symbol and HK per date

CN membership is known up front (`index_weight`), so the downloader can iterate a
symbol list. HK has **no membership API at all**, so there is no list to iterate:
the tradable universe has to be discovered from whole-market by-date responses.
Storing those responses as-is costs one API call per trading day per endpoint
(~4,100 days x 3 = ~12,300 calls for 2010→today) and yields a mirror that is
**survivorship-free by construction** — a name that delisted in 2014 is still in
every file up to its last trading day.

It also makes resume and incremental update the same code path: a date whose file
exists is skipped, so re-running only fetches the new tail.

## Adjustment (splits / dividends / merges)

A-share prices in `cn/daily` are **unadjusted**. To build forward/back-adjusted
series, combine them with `cn/adj_factor` (Tushare's cumulative factor) or derive
events from `cn/dividend` (`stk_div`, `stk_bo_rate`, `stk_co_rate` for
splits/bonus, `cash_div` for cash). This matches Assay's `adj_events` model,
which recomputes the cumulative factor from primitive events rather than trusting
a provider's cumulative number.

## HK corporate actions

Tushare gives HK **no dividend or split records** — only a cumulative
`hk_adjfactor`. The ingest layer therefore reconstructs events from its steps:
where the factor moves from `f_prev` to `f`, the total price ratio is
`r = f / f_prev`, and prices strictly before that date scale by `1 / r`.

Two vendor artifacts make a naive reading of the factor wrong, so each step is
cross-checked against `hk_daily.pre_close` (which is restated onto the
post-action basis on an action day) and dropped unless the two agree within 1%:

* the factor is occasionally **re-anchored** on a date with no corporate action,
  which would otherwise fabricate a double-digit dividend;
* `pre_close` itself **drifts a tick** off the prior close on sub-HK$1 names, so
  it is used only as a corroborating witness, never for the magnitude.

Surviving steps are attributed to a share change (`split_ratio = r`) when the
factor fell, when `hk/daily_adj`'s share count moved materially, **or when that
share count is unavailable**; only a step whose share count is known and flat
becomes a cash distribution of `close_prev * (1 - 1/r)`.

Defaulting to a share event is the safe way to be wrong. The total-return series
is exact under either attribution, but `adj="split"` is the *default* read basis
(`AssayConfig.default_adj`), and misreading a 2-for-1 split as a dividend would
book half the share price as cash and leave the raw split gap in the split-only
panel — every factor would then read a fabricated -50% return. The opposite error
merely over-adjusts the split-only view by a dividend. The ingester logs a warning
counting any steps it could not attribute from share data.

Two hard preconditions follow from `adj_events` being an upsert-only store — a
bad event cannot be withdrawn by a later run, only by deleting the store:

* `hk/daily` **must** be present or no events are written at all (without
  `pre_close` there is nothing to corroborate against, and a vendor re-anchor
  would become a permanent double-digit phantom dividend);
* an incomplete `hk/daily_adj` degrades the split/total distinction but never
  fabricates a payout.

Note that `hk_daily_adj`'s own `close`/`adj_factor` columns are *not* a usable
adjusted series: they do not compose across share consolidations. Use
`hk_adjfactor`, whose `close_price` reconciles exactly with `hk_daily.close`.

A consolidation is normally executed while the stock is **halted**, so the steps
with the largest ratios are exactly the ones with no `hk_daily` bar to witness
them. Those are corroborated against the factor feed's own restated `close_price`
instead, and accepted only when the ratio is extreme enough (≥2x either way) to be
unambiguously a share change — a near-1 ratio on a non-trading day cannot be told
apart from a re-anchor, and skipping a small dividend is a 2% error where missing
a consolidation is a 40x one.

### Measured accuracy

Against the exchange's own daily returns (`close / pre_close`), over the 487 names
that have ever been in HK100, 2010-2026, 1.45M bars:

| deviation | bars | share |
|---|---|---|
| > 0.1% | 1,892 | 0.13% |
| > 1% | 1,117 | 0.077% |
| > 20% | 23 | 0.0016% |

Restricted to a recent month's 300 most-traded names, **0 mismatches** (max 6e-05).

The 23 worst are split roughly evenly between two causes, both inherent to the
vendor feed rather than to the reconstruction:

* **`pre_close` is simply wrong** on some thin days (e.g. `01063.HK` reported a
  HK$0.01 prior close for a stock trading at HK$0.22). Here the reconstructed
  series is the correct one and the *reference* is the bad number.
* **`cum_adjfactor` misses an action that `pre_close` did record** — spin-offs in
  particular (e.g. `00004.HK`'s November 2017 demerger). Because events are
  sourced from factor steps, an action absent from the factor feed is absent from
  `adj_events` too, leaving the raw gap. Sourcing events from `pre_close`
  restatements as well would close this, and is the obvious next improvement.

Neither residual is material to a ranked factor (the default `signal_transform`
is `rank`), but both matter to anything that consumes raw returns.

## Known limitations

1. **HK index membership is a current snapshot, not history.** Tushare exposes
   **no** index-membership interface for HSI or Hang Seng TECH
   (`index_member` / `index_basic market=HK` return nothing). Those two universes
   are therefore a single **current** constituent list vendored in
   `src/assay/data/tushare/constituents.py` (captured 2026-06-27), and backtests
   over them are **survivorship-biased**. Prefer **HK100**, which the ingest layer
   derives point-in-time from realised turnover (see
   [Multi-market](multi_market.md)).

2. **No HK industry classification.** `hk_basic` carries no industry field and
   Tushare has no HK equivalent of the A-share classification, so no
   `security_groups` store is written for `market=HK`. Sector neutralization is
   honestly unavailable there rather than silently a no-op.

3. **Hang Seng TECH index code.** On `index_global` the Hang Seng TECH value
   series is `HKTECH` (not `HSTECH`, which returns nothing). The friendly name
   `HSTECH` is used for the on-disk file and constituent list.

4. **HK prices are HKD only.** No FX book exists, so a HK backtest's NAV is in
   HKD and is not comparable to a US or CN run without converting outside Assay.
