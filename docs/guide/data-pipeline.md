# Data Pipeline

Assay transforms a **local mirror** of the **MASSIVE** US-equity dataset into point-in-time
parquet stores. Nothing is downloaded — the pipeline reads pre-downloaded files from disk. The
data layer is the correctness foundation: every read takes an explicit `as_of_date`, so
look-ahead bias is structurally impossible.

## Local source

`MASSIVE_DATA_DIR` (default `/data/massive_data`) is the root of the local mirror, produced
out-of-band by the `downloader_*` scripts. Expected layout:

```
$MASSIVE_DATA_DIR/
├── us_stocks_sip/day_aggs_v1/{YYYY}/{MM}/{YYYY-MM-DD}.parquet   # daily OHLCV bars
├── corporate_actions/splits/{TICKER}.jsonl                      # one split record per line
└── corporate_actions/dividends/{TICKER}.jsonl                   # one dividend record per line
```

Each day-aggregate parquet has the MASSIVE flat-file columns
`ticker, volume, open, close, high, low, window_start, transactions` (`window_start` is the
Unix-nanosecond start of the trading-day window in US/Eastern).

## Stores

Under `ASSAY_DATA_DIR` (default `data/`):

```
<data_dir>/
├── price_raw/market=US/year=YYYY/month=MM/price_raw.parquet   # unadjusted OHLCV + transactions
├── adj_events/market=US/adj_events.parquet                    # splits & dividends (corp actions)
└── universe_snapshots/market=US/universe_snapshots.parquet    # PIT index membership
```

The MASSIVE day-aggregate source provides **open/high/low/close/volume/transactions** — there
is **no `vwap`**, and `market_cap` is not ingested.

## One-shot prepare

```bash
python -m assay.cli prepare-nasdaq100 --start 2025-01-01 --end 2026-06-09
```

Runs three stages in order, into the configured data dir:

1. **universe** — NASDAQ-100 PIT constituent snapshots (the survivorship-bias-free *union* of
   every ticker that was a member at any point in the range; built from vendored YAML, no source
   files needed).
2. **corp-actions** — splits & dividends read from the local `corporate_actions/` JSONL files
   (best-effort: a failure here no longer aborts the price transfer).
3. **prices** — daily OHLCV from the local day-aggregate parquet, filtered to the universe,
   written as monthly partitions.

Flags: `--skip-universe`, `--skip-corp-actions`, `--skip-prices`.

## Individual stages

```bash
python -m assay.cli universe     --index NASDAQ100 --start 2025-01-01 --end 2026-06-09
python -m assay.cli corp-actions --start 2025-01-01 --end 2026-06-09
python -m assay.cli prices       --start 2025-01-01 --end 2026-06-09
python -m assay.cli discover                         # show the local source layout (sanity check)
```

## Inspect & verify

```bash
python -m assay.cli status                                       # row counts, date range, partitions
python -m assay.cli verify --start 2025-06-01 --end 2025-06-30 --adj split   # read a PIT panel
```

`--adj` is `none | split | total` (alias `forward`). Adjustment uses only corporate actions
known as-of the query date.

## Separate data folders

`ASSAY_DATA_DIR` selects the active store, so you can keep multiple datasets side by side and
the loader/engine set up a fresh folder automatically:

```bash
ASSAY_DATA_DIR=data_2025_2026h1 python -m assay.cli prepare-nasdaq100 --start 2025-01-01 --end 2026-06-09
ASSAY_DATA_DIR=data_2025_2026h1 python -m assay.cli serve-api          # engine uses that folder
```

Set it permanently in `.env` (`ASSAY_DATA_DIR=data_2025_2026h1`). Folders matching `data*/` are
git-ignored.

## Known data caveats

- **No `adj_events` ⇒ unadjusted splits.** If the corp-actions stage was skipped or the local
  `corporate_actions/` tree is missing, the folder has no `adj_events`, so `adj='split'` is a
  no-op and real stock splits appear as large single-day "returns" that pollute factor/portfolio
  results. Backfill: `python -m assay.cli corp-actions --start ... --end ...`.
- **`vwap` is unavailable** — factor expressions referencing `vwap` (and `market_cap`/`cap`)
  cannot run on ingested data; ~52 of the 101 Alpha-101 factors run on OHLCV alone.

See the data-layer design in [engineering.md](../design/engineering.md) §3.
