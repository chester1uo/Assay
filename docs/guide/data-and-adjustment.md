# Data & Adjustment

**English** · [简体中文](data-and-adjustment.zh.md)

This is the most important document for trusting a backtest: **what data Assay stores,
and exactly what prices a factor sees under corporate actions (splits, dividends).**
Get this wrong and every IC number is meaningless. Assay's design goal is that a factor
can *never* see information that was not knowable on the query date, and that adjusted
prices are reproducible from first principles.

---

## 1. Two layers: RAW → ASSAY

| Layer | What | Where |
|---|---|---|
| **RAW** | Vendor data, as delivered, read in place | `MASSIVE_DATA_DIR` (US), `TUSHARE_DATA_DIR` (CN) |
| **ASSAY** | Normalized, point-in-time parquet stores the engine reads | `ASSAY_DATA_DIR` / `ASSAY_DATA_DIR_CN` |

The **ingest** step (`prepare_us` / `prepare_cn`) normalizes RAW into a fixed schema and
writes the ASSAY stores. Nothing downstream ever reads RAW again — the engine, IC
evaluation, portfolio backtest and WebUI all read the ASSAY stores through one interface,
[`DataStore`](../../src/assay/data/store/datastore.py).

### The ASSAY stores

| Store | Grain | Key columns |
|---|---|---|
| `price_raw` | one row per (date, symbol) | `date, symbol, open, high, low, close, volume, transactions, as_of_date, source_id` |
| `adj_events` | one row per corporate action | `symbol, ex_date, event_type, split_ratio, dividend_cash, as_of_date, provider_adj_factor` |
| `universe_snapshots` | one row per membership change | `index_id, effective_date, symbols[], as_of_date` |
| `trade_status` *(CN)* | one row per (date, symbol) | `date, symbol, up_limit, down_limit, limit_up_locked, limit_down_locked, close` |
| `security_groups` *(CN)* | one row per symbol | `symbol, group, as_of_date` |

Prices are stored **raw / unadjusted**. Corporate actions live *separately* in
`adj_events`, and adjustment is applied **at read time** — never baked into storage. This
is what makes any point-in-time slice reproducible: the same raw prices + only the events
known as-of a date yield exactly one adjusted panel.

---

## 2. Bi-temporal: `date` vs `as_of_date`

Every stored row carries two times:

- **`date`** — *event time*: the day the bar/action happened.
- **`as_of_date`** — *knowledge time*: the first day that row was knowable.

Every read takes a **required `as_of_date`**, and the store returns only rows with
`as_of_date <= as_of_date`. Look-ahead bias is therefore structurally impossible — you
cannot query data that did not yet exist. Knowledge time per source:

| Source | `as_of_date` is… |
|---|---|
| Price bar (EOD) | the trading date (known at that day's close) |
| Split / share change | the ex / execution date |
| Cash dividend | the declaration date (falls back to ex-date) |
| Universe membership | the effective date |

```python
panel = store.get_panel(
    fields=["close", "volume"], symbols=universe,
    start_date="2024-01-01", end_date="2024-06-28",
    as_of_date="2024-06-28",   # REQUIRED — everything after this is invisible
    adj="split",
)
```

---

## 3. How factors see prices under splits & dividends

A factor never touches `adj_events` directly. It sees the `(T, N)` price matrices that
`DataStore.get_panel(..., adj=...)` returns, already adjusted. The math lives in
[`adjust.py`](../../src/assay/data/store/adjust.py) and is **forward (a.k.a. "backward-
adjusted to today")**: history is rescaled onto the **most recent date's basis**, so the
latest bar always equals the raw price and only the past is rescaled.

### 3.1 Splits & share changes

A share-change event (split, reverse-split, merger ratio) with **forward ratio**

```
r = split_to / split_from        # 2-for-1 split → r = 2 ; 1-for-10 reverse → r = 0.1
```

divides every price **strictly before** its ex-date by `r`, and multiplies the
corresponding **volume** by `r`. The ex-date bar itself already reflects the split, so it
is left unchanged. Multiple splits compose multiplicatively. Reverse splits (`r < 1`) and
merger share-changes use the same mechanism.

> Example: a 2-for-1 split on 2024-06-10. A close of `$400` on 2024-06-07 becomes `$200`
> after adjustment (÷2), lining up continuously with the ~`$200` bars from 2024-06-10 on.
> Volume on 2024-06-07 is doubled. Returns computed across the split are therefore correct.

### 3.2 Cash dividends (`adj="total"` only)

In `total` mode, a cash dividend `D` with ex-date `e` multiplies every price **strictly
before** `e` by

```
ratio = 1 − D / close_prev
```

where `close_prev` is the **raw** close on the session immediately before `e`. Because both
`D` and `close_prev` are in raw price space, the dividend factor composes correctly with
the split factor (`price_factor = split_factor × dividend_factor`). Two guards:

- **Prior-close gap** — if the session before `e` is more than
  `_MAX_PRIOR_GAP_DAYS = 10` calendar days back (a data hole, or outside the loaded lead-in),
  the dividend is skipped rather than mis-scaled.
- **Dividend ≥ price** — if `D ≥ close_prev` (ratio ≤ 0), the dividend is skipped rather
  than flipping the sign of history.

### 3.3 Adjustment modes (`adj`)

| Mode | Splits | Dividends | Use when |
|---|---|---|---|
| `none` | ✗ | ✗ | you want the literal traded price (e.g. price-limit logic) |
| `split` *(default)* | ✓ | ✗ | most alpha research — continuity across splits, no dividend drift |
| `total` (alias `forward`) | ✓ | ✓ | total-return studies; dividends reinvested |

The provider's own `historical_adjustment_factor` is **stored but not used** for the math —
adjustment is computed only from events known as-of the query date, so there is no leakage
from a vendor factor that silently encodes future actions.

### 3.4 Choosing `adj` for a factor

- Momentum / reversal / most price-shape factors → **`split`** (the default). You want a
  continuous series without dividend step-downs, but you do not want raw price jumps at
  splits.
- Anything comparing to a total-return benchmark, or explicitly modelling reinvested
  dividends → **`total`**.
- Execution-constraint / limit-up-down logic (CN) → **`none`** on the price you compare to
  the raw `up_limit` / `down_limit` bands (see §4).

---

## 4. China A-share specifics

CN data (Tushare) adds a few wrinkles the US path does not have:

- **送转 (bonus/transfer shares)** are converted to a split ratio: "10转15" → `1 + 15/10 =
  2.5`, i.e. treated as a share-change event with `r = 2.5`. Cash dividends flow into
  `adj_events.dividend_cash` (tax-inclusive value preferred).
- **Price limits (涨跌停)** are stored in `trade_status` (`up_limit` / `down_limit` and the
  `*_locked` flags). Use them with `adj="none"` prices for execution constraints — a name
  locked limit-up cannot be bought at the close.
- **Volume units** — Tushare reports volume in 手 (100-share lots). It is normalized during
  ingest; the ASSAY `volume` column is in shares, consistent with the US path.
- **Incremental updates** fetch **by trade-date** (all symbols per API call) and append to
  the per-symbol raw files, so a daily update is a handful of calls, not one per symbol.

---

## 5. Reproduce it yourself

```python
from assay.data.store import DataStore
store = DataStore(cfg)

raw   = store.get_panel(["close"], syms, s, e, as_of, adj="none")   # traded price
split = store.get_panel(["close"], syms, s, e, as_of, adj="split")  # split-continuous
total = store.get_panel(["close"], syms, s, e, as_of, adj="total")  # + dividends

# The latest bar is identical across modes; only history is rescaled.
```

CLI cross-check (adjusted vs raw around a known action):

```bash
python -m assay.cli verify --start 2024-06-01 --end 2024-06-30 --adj split
```

See also: [Data Pipeline](data-pipeline.md) (how to load/update data) ·
[Getting Started](getting-started.md) · [Engineering design](../design/engineering.md).
