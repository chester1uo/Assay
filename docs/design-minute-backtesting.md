# Minute-Level (Intraday) Backtesting — Engineering Design

Status: DRAFT (for adversarial review)
Author: factor-platform
Scope: add minute/intraday backtesting to Assay while keeping the daily path
byte-for-byte unchanged.

---

## 1. Overview & goals

Assay today is a **daily-only** factor backtesting engine. Panels are
`(T dates × N symbols)` matrices whose time axis is a Python `datetime.date`.
Every read goes through `DataStore.get_panel(...)` with an `as_of_date` and is
point-in-time (PIT) correct: only rows knowable on `as_of` are used, and
corporate actions are applied at read time. An expression engine evaluates
factors; an evaluator computes forward returns / IC / decay / turnover; a
portfolio backtester rebalances with cost/execution models.

We want to add **minute-level** backtesting using the already-downloaded local
minute mirror at
`/data/massive_data/us_stocks_sip/minute_aggs_v1/{YYYY}/{MM}/{YYYY-MM-DD}.parquet`
(columns identical to daily: `ticker, volume, open, close, high, low,
window_start[ns,UTC], transactions`; `window_start` marks the *start* of each
1-minute bar; ~390 regular-session bars/day; ~1.47M rows/day; ~54 GB total).

### Goals

1. **PIT correctness at minute granularity** — no intraday look-ahead. A bar is
   knowable only at its *close* (`window_start + 60s`), not at EOD.
2. **Daily path fully preserved** — existing API/SDK/CLI/MCP/tests and all daily
   golden numbers are unchanged. Every change is additive and frequency-gated.
3. **Reuse the existing design** — parquet stores, read-time adjustment, the
   row-indexed engine, the pure-numpy evaluator, and the index-based portfolio
   backtester are all *already* value-opaque on the time axis. We widen the time
   *type* (Date → Datetime) and add a session dimension; we do **not** fork the
   numerics.
4. **Scale to 54 GB** — day-partitioned IO, predicate pushdown, optional
   read-time resampling to coarser bars, and session-chunked evaluation.

### Synthesis decision (which approach we build)

The three candidate approaches scored within noise of each other. This design is
the **Unified Timestamp Axis** (one engine/store/evaluator/portfolio,
`freq` is data not code) as the spine, **grafted** with:

- the **explicit `Frequency` value object** from the Frequency-Parameter-Overlay
  (centralizes every granularity constant — `periods_per_year`, `bars_per_day`,
  `time_dtype`, `time_col`, `partition_grain` — so no layer hard-codes 252/390),
  and its **read-time 1m→5m/15m resampling** (one minute store, coarser bars are
  derived, the primary memory lever);
- the **per-session physical partition + Hive pruning** discipline and the
  **enforced memory guard** from the Intraday-Store approach (one parquet/day,
  refuse to eager-collect a panel over budget).

And we **fix the weaknesses every judge flagged**:

- **`ts_ema`/`ts_dema` must reset per session** — this is a *correctness* hole,
  not a perf note (the existing kernel carries EMA state across NaN gaps).
- **`cs_rank` per-row Python loop** is the dominant Alpha-101 hotspot at 98k
  rows — vectorize it, not just `ts_ema`.
- **The `(T,N,d)` window tensor** blows up to hundreds of GB for multi-day
  windows — windowed reductions get a streaming/cumulative rewrite.
- **Overnight masking and segmentation are enforced by construction**, not by
  caller convention: the engine refuses to evaluate an intraday panel without a
  session vector, and forward returns mask cross-session horizons internally.
- **Annualization aggregates minute NAV → daily first**, then uses `ppy=252`
  (avoids the `sqrt(390)` microstructure bias) — made the *default*, not advice.

We **reject** maintaining two parallel `DataStore` classes (the Intraday-Store
approach): it duplicates the corp-action and PIT logic, which is exactly where
silent divergence is most dangerous. We keep one `DataStore` that branches on
`freq` in a handful of well-named spots.

---

## 2. Data model & storage

### 2.1 The `Frequency` value object (new module)

`src/assay/data/frequency.py`:

```python
@dataclass(frozen=True)
class Frequency:
    code: str            # "1d" | "1m" | "5m" | "15m"
    base_unit: str       # "day" | "minute"
    multiple: int        # 1, 5, 15 ...
    is_intraday: bool
    time_col: str        # "date" (daily) | "ts" (intraday)
    partition_grain: str # "month" (daily) | "day" (intraday)

    # bar/annualization helpers are computed from the *actual* calendar slice,
    # never a hard-coded 390 (half-days vary).
    @property
    def step_seconds(self) -> int: ...        # 60 / 300 / 900
    @property
    def nominal_bars_per_day(self) -> int: ...  # 390/78/26 for 1m/5m/15m, 1 for 1d
                                                # ONLY for default-horizon hints + sizing

DAILY = Frequency("1d", "day", 1, False, "date", "month")
MINUTE_1 = Frequency("1m", "minute", 1, True, "ts", "day")
MINUTE_5 = Frequency("5m", "minute", 5, True, "ts", "day")
MINUTE_15 = Frequency("15m", "minute", 15, True, "ts", "day")

def parse_frequency(code: str | Frequency | None) -> Frequency:
    """None/"1d"/"daily" -> DAILY; "1m"/"5m"/"15m" -> intraday; else ValueError."""
```

`nominal_bars_per_day` is used **only** for default-horizon hints and chunk
sizing. All correctness-bearing math (segmentation, `periods_per_year`,
once-per-day scheduling) derives bar counts **per session** from the calendar
(§3), so half-days and DST never shift boundaries.

### 2.2 Schemas (`src/assay/data/schemas.py`)

Daily `PRICE_RAW_SCHEMA` (lines 35-46) is **untouched** (any change forces a
daily-store rebuild because `upsert_parquet` raises on schema mismatch). We add
a parallel minute schema:

```python
# window_start is Unix ns UTC = bar START; columns identical to daily, so the
# raw column tuple is reused. Rename for clarity, keep a back-compat alias.
AGG_CSV_COLUMNS = DAY_AGG_CSV_COLUMNS  # alias; identical on disk (verified)

PRICE_RAW_MINUTE_SCHEMA: dict[str, pl.DataType] = {
    "ts":          pl.Datetime("ns", "UTC"),   # event_time = bar OPEN (window_start). Stored UTC: DST-unambiguous on disk.
    "session_id":  pl.Int32,                    # ET trading date as YYYYMMDD int (e.g. 20240626). Segmentation + corp-action join key.
    "symbol":      pl.Utf8,
    "open":        pl.Float32,
    "high":        pl.Float32,
    "low":         pl.Float32,
    "close":       pl.Float32,                  # unadjusted
    "volume":      pl.Float32,
    "transactions":pl.Int64,
    "as_of_ts":    pl.Datetime("ns", "UTC"),    # knowledge_time = bar CLOSE = ts + step_seconds. THE PIT line.
    "session_type":pl.UInt8,                    # 0=RTH, 1=pre, 2=post. Lets reads include/exclude extended hours w/o re-deriving.
    "source_id":   pl.Utf8,                     # provenance: per-day flat-file relative key.
}
```

Notes:
- `ts` is **UTC tz-aware on disk** to make DST unambiguous in the file; we
  convert to `America/New_York` only for derivation (session_id, session_type)
  and for the panel time axis surfaced to the engine.
- `session_id` is an `Int32` (`YYYYMMDD`) not a Date: it is the cheap,
  hashable segmentation key carried into the engine as an `(T,)` vector and the
  join key to day-grained `adj_events`. A derived `date: pl.Date` is **not
  stored** (saves space) — it is computed on read only for the corp-action join.
- `adj_events` and `universe_snapshots` schemas are **unchanged**. Corporate
  actions and index membership are inherently day-grained events; the read layer
  broadcasts the per-session factor across all bars of a session.

### 2.3 Partition layout

Daily layout (`price_partition_path`, schemas.py:74) is **unchanged**, so
existing partitions need **zero migration**. We add a frequency-aware path and a
**day-level** partition for minute (one file/session ≈ 1.47M rows ≈ 22 MB raw,
~12-15 MB zstd):

```python
def price_partition_path(data_dir, market, year, month, *, freq=DAILY, day=None) -> Path:
    if not freq.is_intraday:
        # EXISTING daily path, byte-identical (no "freq=" level inserted).
        return data_dir/"price_raw"/f"market={market}"/f"year={year:04d}"/f"month={month:02d}"/"price_raw.parquet"
    return (data_dir/"price_raw_minute"/f"market={market}"
            /f"year={year:04d}"/f"month={month:02d}"/f"day={day:02d}"/"price_raw_minute.parquet")

def minute_root(data_dir, market) -> Path:
    return data_dir/"price_raw_minute"/f"market={market}"
```

We store **only the canonical 1m store**. 5m/15m are produced by deterministic,
PIT-correct **read-time resampling** (§4.3), never as separate stores — keeps
disk at ~54 GB and makes 5m/15m the recommended research default.

Each day file is **sorted by `["symbol", "ts"]`** so per-symbol slices are
contiguous (helps adjust + pivot) and parquet row-group statistics on `symbol`
let a NASDAQ-100 read (~100 of ~10,144 tickers) prune to ~1% of rows.
Compression: zstd (default of `write_parquet_atomic`).

### 2.4 Ingestion of `minute_aggs`

Config (`src/assay/config.py`, `MassiveConfig`):

```python
minute_aggs_subdir: str = "us_stocks_sip/minute_aggs_v1"
@property
def minute_aggs_dir(self) -> Path:
    return Path(self.source_dir) / self.minute_aggs_subdir
```

Reader (`src/assay/data/massive/flatfiles.py`): parameterize `LocalFlatFiles`
by `freq`:

```python
class LocalFlatFiles:
    def __init__(self, config: MassiveConfig, freq: Frequency = DAILY):
        self.config = config
        self.freq = freq
        self.root = config.minute_aggs_dir if freq.is_intraday else config.day_aggs_dir

    def list_aggs(self, start, end) -> list[AggFile]: ...   # generalize list_day_aggs (file-listing handles holidays)

    def read_minute_agg(self, date, symbols=None) -> pl.DataFrame | None:
        # Reuse the EXISTING window_start -> ET conversion (flatfiles.py:120-126)
        # but DROP the trailing .dt.date(): keep a tz-aware ts.
        df = pl.read_parquet(path, columns=list(AGG_CSV_COLUMNS))
        if symbols is not None:
            df = df.filter(pl.col("ticker").is_in(list(symbols)))
        ts_utc = pl.from_epoch("window_start", time_unit="ns").dt.replace_time_zone("UTC")
        ts_et  = ts_utc.dt.convert_time_zone("America/New_York")
        return df.with_columns(ts_utc.alias("ts"), ts_et.alias("_ts_et"))
```

Normalizer + ingester (`src/assay/data/ingest/prices.py`, minute branch):

```python
def normalize_minute_agg(df, source_id, freq, session_bounds) -> pl.DataFrame:
    # session_id from ET date; session_type from ET wall-clock vs calendar open/close;
    # as_of_ts = ts + freq.step_seconds (BAR CLOSE) -- the single PIT-critical line.
    ...

class MinutePriceIngester:
    """Per-DAY write, NOT month-wide upsert."""
    def run(self, start, end, symbols=None) -> dict:
        for f in self.client.list_aggs(start, end):           # one file per session
            raw = self.client.read_minute_agg(f.date, symbols=symbols)
            norm = normalize_minute_agg(raw, f.key, self.freq, session_bounds(f.date))
            path = price_partition_path(self.config.data_dir, self.config.market,
                                        f.date.year, f.date.month, freq=MINUTE_1, day=f.date.day)
            write_parquet_atomic(norm.sort(["symbol", "ts"]), path)  # idempotent re-ingest = overwrite that day
```

**Why not `upsert_parquet`** (io_utils.py:23): it is a full read-modify-write
(read whole partition, concat, `unique`, rewrite). A month of minute data is
~30M rows; that is memory/IO-prohibitive. Per-day `write_parquet_atomic`
(reused as-is, granularity-agnostic) writes one append-only file per session and
re-ingest is a single-file overwrite. Dedup within a file is on `["ts","symbol"]`
(NOT `["date","symbol"]`, which would collapse 390 bars to one row).

Pre/post-market bars (>390/day) are tagged via `session_type` and **kept on
disk** but **excluded by default at read** (`include_extended=False`).

A new CLI/pipeline entry point ingests minute data (§9); `prepare_nasdaq100`
gains a `freq` arg routing prices to the minute reader/schema/layout. Universe
and corp-action ingestion are unchanged.

---

## 3. Time axis & calendar

### 3.1 The bar grid

For an intraday run the panel time axis `T` is the **ordered set of bar
timestamps actually present**, surfaced as a `pl.Datetime("ns","America/New_York")`
column named `ts`. Daily is the special case `freq.time_col == "date"`
(`pl.Date`). The engine's axis construction (`np.unique`/`np.searchsorted`,
engine.py:112-115) is dtype-agnostic and works on `datetime64[ns]` with **zero
kernel change** — verified.

### 3.2 Calendar additions (`src/assay/data/calendar.py`)

`trading_days`/`is_trading_day` are **kept** for the daily path. We add, all
wrapping `exchange_calendars` (XNYS, already a dependency and already DST/half-day
aware):

```python
def session_open_close(day, calendar="XNYS") -> tuple[datetime, datetime]:
    """ET (open, close). Half-days return 13:00 ET close automatically."""

def session_bars(day, *, freq=MINUTE_1, include_extended=False, calendar="XNYS") -> list[datetime]:
    """Authoritative bar-start timestamps for one session at `freq`.
    Half-days yield ~210/~42/~14 bars at 1m/5m/15m — variable, never hard-coded 390."""

def bars_per_session(day, *, freq=MINUTE_1, include_extended=False) -> int:
    return len(session_bars(day, freq=freq, include_extended=include_extended))

def session_type(ts_et, day) -> int:   # 0=RTH, 1=pre, 2=post
def session_ids(time_index_et) -> np.ndarray:  # (T,) Int32 YYYYMMDD per bar -- the engine segment vector
```

- **Half-days**: handled intrinsically because every bar count is derived from
  `session_open`/`session_close`, never assumed.
- **Pre/post-market**: `session_type` tags them; default reads pass
  `include_extended=False` and filter `session_type == 0`. An opt-in keeps them.
- **DST**: `ts`/`as_of_ts` are stored UTC and derived against ET via the
  DST-correct calendar, so the RTH `09:30-16:00` filter and the UTC↔ET offset are
  correct year-round. The summer example (09:30 ET = 13:30 UTC) and the winter
  offset both fall out of the conversion; an ingest-time assertion checks that the
  derived bar count matches `bars_per_session` (catches calendar/data drift).

### 3.3 `periods_per_year` (annualization)

Computed from the **actual calendar slice** of the run, not `252 * nominal`:

```python
def periods_per_year(start, end, *, freq) -> float:
    sessions = trading_days(start, end)
    total_bars = sum(bars_per_session(d, freq=freq) for d in sessions)
    years = len(sessions) / 252.0
    return total_bars / years if years else float("nan")
```

But see §7: the **default** annualization aggregates minute NAV to one
point/session and uses `ppy=252` for numerical stability; `periods_per_year`
above is used only if a caller explicitly opts into per-bar annualization.

---

## 4. Point-in-time semantics intraday

The daily PIT guarantee is structural: every store read filters
`pl.col("as_of_date") <= as_of` (datastore.py:95,106) and `_as_date` truncates
any datetime to a date (datastore.py:33-38). We **widen the time type** and add a
**per-bar knowledge time**, preserving the same one-filter-per-read proof.

### 4.1 Per-bar knowledge time

Ingest sets `as_of_ts = ts + freq.step_seconds` (the **bar close**). The 10:30
bar (`window_start = 10:30`) becomes knowable at 10:31; an `as_of` of 10:30:30
excludes it, an `as_of` of 10:31:00 includes it. **This single line is the entire
intraday no-look-ahead guarantee.** Setting it to the session close instead would
silently reintroduce look-ahead — it is the highest-risk line in the design and
gets a dedicated PIT-exclusion test (§11).

### 4.2 `get_panel` widening

```python
def _as_time(value, *, freq):
    if not freq.is_intraday:
        return _as_date(value)            # BYTE-IDENTICAL daily behavior (still truncates datetimes)
    # intraday: parse full ISO datetime, localize to ET, keep UTC internally
    return _parse_et_datetime(value)
```

`DataStore.get_panel(..., freq=DAILY)`:
- daily branch: unchanged (filters `date`, `as_of_date`; returns `["date","symbol",*fields]`).
- minute branch: `start/end/as_of` accept full ISO datetimes; filters become
  `pl.col("ts").is_between(start, end)` and `pl.col("as_of_ts") <= as_of` and
  `session_type == 0` (unless extended); returns `["ts","session_id","symbol",*fields]`.

The daily 10-calendar-day dividend lead-in (`_DIV_LOOKBACK_DAYS`, datastore.py:30,144)
is **replaced for minute** by reading only the **prior session's last RTH bar**
(via `session_open_close` on the session before `start`). Pulling 10 days of
minute bars (~14.7M rows) just for a dividend `close_prev` is prohibitive; the
prior-session bar is exactly what the dividend factor needs.

### 4.3 Resampling preserves PIT

1m→5m/15m happens **inside `DataStore`** (lazily) so the engine only ever sees
the coarse panel:

```python
lf.group_by_dynamic("ts", every=freq.code_to_polars(), closed="left", label="left", by="symbol")
  .agg(open=first, high=max, low=min, close=last, volume=sum, transactions=sum,
       as_of_ts=max("as_of_ts"))   # aggregate knowledge_time = LATEST constituent bar close
```

PIT rules:
- `closed="left", label="left"` — bar labeled by its start, covers `[start, start+step)`.
- aggregate `as_of_ts = max(constituent as_of_ts)` = the coarse bar's close.
- **partial bars at the as_of frontier are dropped** — only emit a coarse bar if
  *all* its constituent 1m bars are knowable by `as_of` (enforced by the
  `as_of_ts <= as_of` filter applied **before** resampling, plus a completeness
  check against `bars_per_session`/expected constituent count). This is the
  subtle PIT edge the judges flagged; it gets its own test.

### 4.4 Corp actions (session-aware cut)

`adj_events` stays day-grained and the `as_of_date <= as_of` filter is unchanged
(membership/actions are genuinely day-knowable; an intraday `as_of` coerces to its
date for these stores, which is correct because the events were published EOD).
The adjustment math (`src/assay/data/store/adjust.py`) becomes session-aware:

- Today `_adjust_one_symbol` (adjust.py:53-85) does `bisect_left(dates, ex_date)`
  on a per-symbol date list and reads `close_raw[pos-1]` as the prior close, with
  a `_MAX_PRIOR_GAP_DAYS=10` guard. On minute data `dates` would be per-bar
  timestamps, so `pos-1` is the prior *minute* and the gap guard never fires —
  **wrong by construction**.
- Fix: build a per-symbol **session-date array** (unique `session_id`s), bisect
  `ex_date` against *that*, take the cut at the **first bar of the ex-date
  session**, and set `close_prev` = the **last RTH bar of the prior session**.
  Replace the day-gap heuristic with a **prior-session-existence** check.
- The split/dividend factor for a session is then **broadcast across all bars of
  that session** (one factor per `session_id`, applied to every bar). The raw-space
  composition (`split_factor * div_factor`) is reused verbatim; only the
  index/lookup changes. This keeps the daily code path identical (one session =
  one row → the session-aware path degenerates to today's behavior).

---

## 5. Engine changes

The core math is row-indexed and value-opaque; we make three additive,
frequency-gated changes plus two perf rewrites.

### 5.1 Time-axis name/dtype (the one hard correctness bug)

`FactorResult.to_frame` (engine.py:79-93) hard-casts the time column to
`pl.Date` (line 86). On a minute panel this collapses all ~390 bars/day into
duplicate `(date, symbol)` rows — silent corruption. Fix: record the source
polars dtype + time-col name on the engine and emit them back:

```python
class FactorEngine:
    def __init__(self, panel, group_data=None, *, time_col="date",
                 session_ids=None, freq=DAILY):
        for required in (time_col, "symbol"):
            if required not in panel.columns: ...
        self._time_col = time_col
        self._time_dtype = panel.schema[time_col]      # Date (daily) | Datetime (minute)
        self._session_ids = session_ids                # (T,) int32 or None
        self._freq = freq
        d_all = panel[time_col].to_numpy()             # datetime64 works unchanged
        ...
```

`to_frame` emits a column named `self._time_col` cast back to `self._time_dtype`
(daily → `date:pl.Date`, unchanged; minute → `ts:Datetime`). `EvalContext` gains
`session_ids: np.ndarray | None`.

**Enforcement (anti-silent-failure):** `from_store(..., freq=MINUTE_*)` builds the
session vector from the panel and passes it in; `FactorEngine.__init__` **raises**
if `freq.is_intraday and session_ids is None`. The kernels cannot be invoked on an
intraday panel without segmentation.

### 5.2 Overnight-gap / session-boundary handling

A rolling window of length `d` spans `d` consecutive **rows**. On a stacked
minute panel that silently bleeds yesterday's 15:59 bar into today's 09:30
window, and `ts_delay(x, 1)` at 09:30 pulls yesterday's 16:00 — economically
wrong, no error. Fix: segment every time-series operator at session boundaries.

- `operators/_base.windows(x, d, session_ids=None)`: when `session_ids` is given,
  any window whose `d`-row span reaches before its **own session start** is
  NaN-padded at the boundary (same NaN mechanism already used for warm-up,
  _base.py:33-35). Implemented via a per-row "rows-since-session-start" vector:
  a window at row `t` is valid only if `rows_since_start[t] >= d-1`.
- `ts_delay/ts_delta/ts_returns/ts_log_returns` (time_series.py:18-44): a shift
  by `d` that crosses a session boundary yields NaN (so delay-by-1 at the first
  bar of a session is NaN, never the prior session's last bar).
- **`ts_ema`/`ts_dema` MUST reset per session** (correctness, not perf). The
  current recurrence (time_series.py:113-125) explicitly carries `prev` through
  gaps (`np.where(np.isnan(xt), prev, stepped)`) — across the overnight boundary
  that is a look-back error. The segmented variant **reseeds `prev = NaN` at each
  session start**, so each session's EMA is independent. (We expose an explicit
  `segment_overnight` flag, default `True` for intraday, to allow deliberate
  multi-day EMAs.)

Daily: `session_ids` is one-per-row, so every guard is a **no-op** and output is
byte-identical (asserted in tests across all ~20 `ts_*` operators).

### 5.3 Window units (granularity-portable horizons)

Windows stay **bar counts** internally (`_coerce_window`, parsing.py:354, is
untouched, so the daily integer path is unchanged). We add a **parse-time** unit
convention so the same factor text is portable:

- A `d`-suffixed literal, e.g. `ts_mean(close, '20d')`, lowers to
  `LitNode(20 * bars_per_day)` when an intraday context is set, else `LitNode(20)`.
- The `adv{d}` macro (parsing.py:301-303) and bare `returns` → `ts_returns(close,1)`
  (parsing.py:346-347) bind to `'{d}d'` / `'1d'`, so Alpha-101 factors keep
  **day** semantics across frequencies. With no intraday context, the `d` suffix
  equals a raw int — daily expansion is unchanged.
- Because `bars_per_day` varies (half-days), the lowering uses the per-session bar
  map when the window must be exact; for the common case it uses the nominal count
  and the segmentation NaN-pad absorbs short sessions.

### 5.4 Cross-sectional ops (the real Alpha-101 hotspot)

Cross-sectional kernels reduce along the symbol axis per row and are
granularity-blind, BUT `cs_rank` (cross_sectional.py) is
`np.vstack([_rank01_row(x[t]) for t in range(T)])` — a **per-row Python loop with
an inner tie-handling while-loop**. `rank()` is the single most common Alpha-101
op; at `T≈98k` (vs 252) this is the dominant wall-clock cost and is **not**
addressed by chunking. Fix: vectorize via `scipy.stats.rankdata`-style
double-`argsort` over `axis=1` with NaN masking and average-tie handling, falling
back to the current per-row path only when `scipy` is unavailable. Same treatment
for the `rank` branch of `_group_apply` (cs_group_rank). This is a pure perf
rewrite with golden-number tests against the existing implementation.

### 5.5 Diagnostics

`diagnostics.output_diagnostics` thresholds (`warmup_frac`, `min_coverage`,
252-row assumptions) are parameterized by `freq`; stat labels `n_dates` →
`n_periods` (additive; daily keeps `n_dates`). Math is row-based and unchanged.
The AST module, parser grammar, registry, and arithmetic/math kernels need
**zero** change.

---

## 6. Forward returns & evaluation at minute horizons

The evaluator kernels (`metrics.py` IC/RankIC, `decay.py`, `turnover.py`,
`groups.py`) are pure per-row cross-sectional / row-offset math and stay
**granularity-agnostic — zero code change**. Units live at the call boundary
(`service.py`). Two changes are needed in `forward_returns.py`.

### 6.1 `forward_returns` (bar horizons + session masking)

```python
def forward_returns(close, open_, horizons, execution="next_open",
                    *, session_ids=None, entry_lag=1):
```

- Horizons are **bar counts** for intraday (`h=1` = 1 bar). The row-shift math
  (forward_returns.py:86-99) is unchanged.
- `entry_lag` generalizes the hard-coded `t+1` skip (line 90). Default `1` keeps
  daily behavior; for a "day-equivalent" intraday entry the caller passes
  `entry_lag = bars_to_next_session_open`.
- **Session masking (enforced):** when `session_ids` is given, a forward return
  whose entry and exit fall in different sessions is set to **NaN**, *unless* the
  horizon is explicitly a whole-day horizon (e.g. `'1d'` = `bars_per_day`). This
  prevents silent overnight contamination of intraday horizons. Masking happens
  **inside the function**, not in the caller, so it cannot be forgotten.
- The stale `execution == "vwap"` `ValueError` (lines 63-67) is rewritten:
  intraday data now exists, so `vwap` can use a genuine per-bar typical price
  `(h+l+c)/3` (or transactions-weighted). Implemented as an optional branch.

### 6.2 Units at the service boundary

- `decay_halflife` (decay.py) returns half-life in the **same unit as the horizon
  keys** (bars for minute). The service converts to days
  (`÷ bars_per_day`) before storing `decay_halflife_days`, and **also** stores a
  unit-tagged `decay_halflife` + `granularity` field (additive — no relabeling).
- `turnover` lag defaults to 1 for daily; minute uses `lag = bars_per_day`
  (once-per-day-equivalent churn). `lag=1` at minute scale is minute-to-minute
  autocorrelation (~1, ~0 turnover) and is meaningless.
- Minute default horizons: `default_horizons_minute = (1, 5, 30, 390)`
  (1m/5m/30m/1d) in `AssayConfig`.
- The session forward-return memo key (service.py:180) becomes
  `f"fwd::{freq.code}::{execution}"` so a shared session never serves a daily
  matrix for a minute request.

---

## 7. Portfolio backtest intraday

All numeric stages (`accounting.py` drift, `weights.py`, `constraints.py`,
`signal.py`, `execution.py`, `costs.py`) are granularity-neutral and reused
verbatim. Changes are concentrated in annualization, scheduling, units, and
labels.

### 7.1 Annualization (default = aggregate to daily)

`_PPY = 252` is a module constant in **two** places (metrics.py:34,
backtester.py:72). Replace both with a config-derived `periods_per_year` threaded
into `compute_metrics`. Every metric fn **already** accepts a `ppy`/`periods_per_year`
param (metrics.py:100,156,175,209,307,361,403) — only the two constants and the
`compute_metrics` call site (metrics.py:571) change.

**Default convention (made explicit, not advice):** for intraday runs,
`compute_metrics` **first aggregates the per-bar NAV to one point per
`session_id`** (compounding within each session), then annualizes with
`ppy=252`. Minute returns have large microstructure autocorrelation that biases
naive `sqrt(390)` time-scaling; aggregating to daily is both numerically stable
and reuses the proven 252 path. A `annualization_basis: "daily"|"bar"` config
field defaults to `"daily"`; `"bar"` uses `periods_per_year` from §3.3 for users
who know what they want. The risk-free de-annualization (backtester.py:495) uses
the same per-period divisor.

### 7.2 Rebalance scheduler (`rebalance.py`)

Today `_REBALANCE_TYPES = {daily, weekly, monthly, quarterly, threshold, signal}`
(config.py:40) and the groupers key on calendar fields (`isocalendar`,
`weekday`). At minute scale `daily` would mean *every minute*. Add intraday
families + dispatch branches + enum members + validator update:

- `every_n_bars` — stride over the row index (n in bars).
- `at_open` / `at_close` — first / last RTH bar of each session (group by
  `session_id`). **Redefines `daily` for minute** to mean once-per-session (first
  bar) rather than every row.
- `at_time HH:MM` — pick the bar matching an ET wall-clock per session (a
  bar-of-day selector analogous to the existing weekday selector,
  rebalance.py:160).

The grouper keys on `session_id` + intraday `ts`. Weekly/monthly group on the
**session date** of each bar so 390 bars don't collapse.

### 7.3 Execution, costs, accounting

- `execution_offset_days` (config.py:83, range `[1,3]`) is reinterpreted in
  **bars** and the range relaxed for intraday (a true T+1-day lag is
  `bars_per_day`; a 1-bar lag is fine). The no-look-ahead invariant
  (`offset >= 1` bar, signal at `s` executed at `s+offset`) is preserved in bar
  terms. Add `execution_offset_bars` as the intraday-facing name; daily keeps the
  old field/default.
- `cov_window`/`adv_window` (config.py:94,136) are expressed in bars or accept a
  `'Nd'` unit converted to bars at consumption (backtester.py:358, weights.py
  windows). A 252-**bar** cov window is 0.65 sessions — wrong; intraday uses
  day-counts→bars. To bound the Ledoit-Wolf `O(W)` loop (weights.py:255) and the
  SLSQP QP, the **risk-aware weight methods run on session-aggregated returns**,
  not raw minute returns.
- `_exec_price_matrix` (backtester.py:417-423): with real intraday bars,
  `vwap`/`arrival` use genuine per-bar prices instead of the close proxy; `next_open`
  is unambiguously the next *bar* open.
- ADV/capacity baseline becomes per-session daily volume (sum of bars) so
  participation caps stay calibrated.
- `output_frequency` gains a daily-from-minute downsample; `_sample_series`
  (backtester.py:616-653) groups minute NAV by `session_id`. `_to_pydate`'s
  `s[:10]` truncation (metrics.py:448) and `monthly_returns` keep the intraday
  `ts`. `n_trading_days` reports **distinct sessions** (not row count); add an
  `n_bars` field.
- `accounting.py` per-row Python loop (accounting.py:141) and `_benchmark_nav`
  loops (backtester.py:498,505) were fine at 252 rows; at 98k they are hot. The
  daily-aggregation default keeps the **metrics** loop at ~252, but the
  **accountant** still walks every bar — vectorize the drift/cost walk where it
  was a Python loop (cumulative-product NAV between rebalances).

---

## 8. Scale & performance

Targets: ~1.47M rows/day, ~54 GB, a year of NASDAQ-100 minute ≈ 98k×100. Note
the corrected memory math: a single `(T,N)` field pivot is ~98k×100×8 ≈ **78 MB**
(not 8 GB — earlier drafts conflated this with the window tensor). The real
blowup is the **`(T,N,d)` window tensor** (§8.2).

### 8.1 IO

- Day-level partitions + a freq-aware enumerator replacing `_months_in_range`:
  per-day files for minute (enumerate via `trading_days`, skipping holidays so
  empty days never read 1.47M rows), per-month for daily.
- Push **all** filters into `pl.scan_parquet` predicate pushdown (symbol
  `is_in`, `ts` range, `as_of_ts <= as_of`, `session_type == 0`) so row-group
  statistics prune before `collect`. The existing read already uses
  `scan_parquet` (datastore.py:92-99); we extend the predicates.
- Dividend lead-in reads **one** prior-session file, not 10 days.

### 8.2 Memory — the window tensor

`windows()` returns a `(T,N,d)` `sliding_window_view`; it is a zero-copy stride
trick **until a reduction materializes it**. `ts_rank`, `ts_skew`/`ts_kurt`,
`ts_decay_linear`, `ts_argmax` force the full allocation. At `T=98k, N=100,
d=390` that is ~30 GB for **one** operator; a `'20d'`-equivalent window
(`20*390=7800`) is ~600 GB — impossible. Fixes:

1. **Rewrite reductions to streaming/cumulative kernels** that never materialize
   `(T,N,d)`:
   - `ts_mean`/`ts_sum`/`ts_std`/`ts_cov`/`ts_corr` → cumulative-sum /
     Welford-style rolling (O(T·N), O(1) extra per row).
   - `ts_min`/`ts_max`/`ts_argmax`/`ts_argmin` → monotonic-deque rolling.
   - `ts_rank` → rolling order-statistic (or numba) — no full window copy.
   - `ts_decay_linear` → maintain the weighted sum incrementally.
   These are gated so daily behavior/golden numbers are identical; the streaming
   path is required for intraday and a strict win for large-`d` daily too.
2. **Session-chunked evaluation:** because segmentation makes most `ts_*`
   windows session-local, evaluate in N-session chunks with a `max(window)-1`-bar
   carry-over buffer from the prior session (for deliberately cross-session
   windows). Peak RAM is bounded to `(chunk_sessions × bars × N)`.
3. **Float32 raw fields:** keep `Float32` on disk; the pivot can hold `Float32`
   and upcast to `Float64` only per active matrix. (This touches the `_matrix`
   dtype contract, gated by `freq`; daily stays `Float64`.)

### 8.3 Caches

- `SessionCache` (`cache/session.py`) pivots on the **`time_col` axis** (default
  `date`; `ts` for minute). Today it requires a `date` column and `np.unique`s on
  it (session.py:49-56) — on minute that collapses 390 bars/day with
  last-writer-wins. Fix: configurable time-axis column, narrower per-session
  sessions for minute to fit `l1_memory_gb`.
- `L2FactorCache` (`cache/l2.py`): fold `freq.code` into the key preimage and
  bump the namespace `assay-l2-v1` → `assay-l2-v2` so daily/minute never alias.
  A single 1m year×100 matrix is ~78 MB; `l2_max_gb=20` holds ~260. Add an
  LRU/size cap (today `clear()` is all-or-nothing) and prefer **per-session
  sharding** + memmap for large minute matrices so the headline workload is not
  forced to recompute.
- Resampled 5m/15m panels are memoized in the session cache keyed on `freq.code`
  so iterative research does not re-resample on every read.

### 8.4 Enforced memory guard

A hard guard refuses to eager-`collect` a minute panel whose estimated size
exceeds `l1_memory_gb`/`l2_max_gb`, raising a clear error that suggests a coarser
`freq` or shorter window (or auto-switching to chunked/streaming mode). This
turns silent OOM into an actionable message.

### 8.5 Wall-clock posture

`scripts/bench_alpha101.py` gains a minute variant as a **performance gate**: a
single-factor 1yr NASDAQ-100 5m eval must complete within a target (TBD, see
§12). The streaming kernels + vectorized `cs_rank` + 5m-default are what make
this tractable; without them a 1m/1yr Alpha-101 factor would be minutes, not
sub-second.

---

## 9. API / SDK / CLI / config surface

One optional `frequency` (alias `freq`/`granularity`, default `"1d"`) threaded
everywhere; omitting it reproduces today's behavior exactly.

- **`AssayConfig`**: `default_frequency="1d"`,
  `default_horizons_minute=(1,5,30,390)`, `annualization_basis="daily"`;
  `MassiveConfig.minute_aggs_subdir` + `minute_aggs_dir`.
- **`DataStore.get_panel(fields, symbols, start, end, as_of, adj, *, freq=DAILY)`** —
  minute accepts ISO datetimes for `start/end/as_of`; output columns
  `["date","symbol",*fields]` (daily, unchanged) or `["ts","session_id","symbol",*fields]`
  (minute).
- **`FactorEngine.__init__(panel, group_data=None, *, time_col="date",
  session_ids=None, freq=DAILY)`**; `from_store(..., freq="1d")` builds the
  session vector for intraday and routes the store/schema/layout.
- **`FactorResult.to_frame`** preserves source time dtype.
- **`evaluator.forward_returns(close, open_, horizons, execution="next_open", *,
  session_ids=None, entry_lag=1)`**.
- **`AssayService.evaluate/batch/create_session/correlation_matrix(...,
  frequency="1d")`**; `period`/`as_of` accept datetimes when intraday;
  `_resolve` (service.py:106-123) branches on freq for parsing and minute horizon
  defaults.
- **Portfolio config**: `bar_interval`/`annualization_basis`; intraday
  `rebalance_type` values (`every_n_bars`, `at_open`, `at_close`, `at_time`);
  `execution_offset_bars`; intraday `output_frequency`.
- **REST** (`api/models.py`): `EvaluateRequest`/`BatchRequest`/`SessionCreateRequest`/
  `PortfolioBacktestRequest` gain optional `frequency`; `service_kwargs()`
  drop-None forwards it.
- **CLI** (`cli.py`): `--frequency` flag; `--start`/`--end` accept ISO datetimes
  in intraday mode; `--horizons` accept `Nd`/`Nm` suffixes; new
  `assay ingest-minute` command.
- **MCP** (`mcp/server.py`): `assay_evaluate`/`assay_batch`/
  `assay_library_correlation` gain `frequency` (default `"day"`);
  `assay_system_status` reports the resolved frequency + minute defaults.
- **`FactorReport`**: add `granularity` field + tagged aliases
  (`decay_halflife`, `n_periods`, `turnover`) while keeping
  `decay_halflife_days`/`n_dates`/`turnover_1d` for daily back-compat;
  `compute_factor_id` folds in `granularity` so daily and minute reports for the
  same expression coexist.

---

## 10. Backward compatibility & migration

**Zero daily migration is the design goal and it is structural, not aspirational.**

- **Storage**: `PRICE_RAW_SCHEMA`, `price_partition_path` (month grain),
  `price_root`, `adj_events`, `universe_snapshots` are untouched. Minute is a
  **new store** (`price_raw_minute`), so `upsert_parquet`'s schema-mismatch raise
  never fires on the daily store. No rebuild.
- **Reads**: `_as_date` keeps truncating datetimes for `freq=DAILY` (callers that
  pass datetimes expecting date semantics are unaffected). Daily `get_panel`
  returns the identical `date`/`pl.Date` long frame.
- **Engine**: the only edits to shared code are (a) `to_frame` casting back to the
  recorded source dtype (a no-op for daily, which records `Date`), and (b) new
  `time_col`/`session_ids`/`freq` params defaulting to today's behavior. The
  segmented `windows`/`ts_*` paths are gated on `session_ids` being non-None, so
  daily output is byte-identical. The `cs_rank` vectorization is validated against
  the current implementation. AST/parser/registry/arithmetic kernels: zero change.
- **Evaluator/portfolio**: new params default to daily behavior (`entry_lag=1`,
  `lag=1`, day labels). `bar_interval` defaults to `"day"` → `ppy=252`.
- **`config_hash`/`run_id` stability**: portfolio `config_hash` (config.py:355)
  is a flat hash over all fields. New fields must be added with daily-equivalent
  defaults **and excluded from the hash preimage when at their default**, or the
  hash must be versioned, so existing daily `run_id`s are stable. This is the one
  fragile carve-out and is called out explicitly for review.
- **Library/cache identity**: `factor_id` folds in `granularity` (daily keeps its
  legacy id because `"1d"` canonicalizes to the existing preimage), preventing a
  minute report from overwriting a daily one. L2 namespace bump only affects the
  (currently unused) L2 cache.
- **Report JSON**: all new fields are additive; existing consumers (WebUI/agent)
  keep reading `decay_halflife_days`/`n_dates`/`turnover_1d`.
- **Tests**: existing daily tests exercise the unchanged paths; a parallel
  `tests/{data,engine,portfolio}/minute_*` suite covers the new path.

---

## 11. Phased implementation plan

Each milestone is independently shippable and testable; the daily path stays
green throughout.

**M0 — `Frequency` + config + calendar (no behavior change).**
Add `frequency.py`, `MassiveConfig.minute_aggs_dir`, `AssayConfig` minute
defaults, and calendar helpers (`session_open_close`, `session_bars`,
`bars_per_session`, `session_ids`, `session_type`).
Tests: half-day bar count (e.g. day-after-Thanksgiving = 210 1m bars), DST
boundary (summer vs winter UTC offset), `bars_per_session` vs the actual file.

**M1 — Minute ingestion + minute store schema/layout.**
`PRICE_RAW_MINUTE_SCHEMA`, `price_partition_path(freq=...)`, freq-parameterized
`LocalFlatFiles.read_minute_agg`, `MinutePriceIngester` (per-day atomic write),
`assay ingest-minute` CLI.
Tests: ingest round-trip (a known day file → expected rows, `as_of_ts = ts+60s`,
`session_type` tagging, pre/post excluded count), idempotent re-ingest.

**M2 — Intraday PIT read path in `DataStore`.**
`_as_time`, `get_panel(freq=...)` minute branch, day-file enumerator, prior-session
dividend lead-in, session-aware `forward_adjust`.
Tests: **intraday `as_of_ts` exclusion** (as_of of 10:30:30 excludes the 10:31
bar), dividend prior-close = prior session last RTH bar, split factor broadcast
across all bars of a session, daily path unchanged (golden).

**M3 — Engine intraday semantics.**
`to_frame` dtype fix, `time_col`/`session_ids`/`freq` plumbing + the
intraday-requires-session-vector guard, segmented `windows`/`ts_*`, **`ts_ema`/
`ts_dema` per-session reset**, `'Nd'` window units, vectorized `cs_rank`.
Tests: overnight-gap NaN in windows and `ts_delay`; EMA independence across
sessions; `cs_rank` vectorized == per-row golden; all `ts_*` byte-identical on
daily.

**M4 — Streaming windowed kernels (perf, correctness-preserving).**
Rewrite `ts_mean/sum/std/min/max/argmax/argmin/rank/decay_linear/cov/corr` to
streaming/cumulative form; session-chunked evaluation; memory guard.
Tests: streaming == materialized golden (daily + minute); peak-RAM assertion on a
large-`d` minute panel; benchmark gate in `scripts/bench_alpha101.py`.

**M5 — Evaluator at minute horizons.**
`forward_returns` bar horizons + enforced session masking + `entry_lag` + real
`vwap`; service unit conversion (`decay_halflife`/`turnover`/`granularity`
labels); minute horizon defaults; freq-tagged fwd memo key.
Tests: cross-session forward return masked to NaN; whole-day horizon allowed to
cross; half-life unit conversion; daily evaluator unchanged.

**M6 — Portfolio intraday.**
`periods_per_year` + daily-aggregation annualization default; intraday rebalance
types + dispatch + validator; `execution_offset_bars`; cov/adv windows in bars;
intraday execution prices; daily-from-minute output; `n_bars`/`granularity`
fields; vectorized accountant walk.
Tests: Sharpe/vol stable under daily-aggregation; `at_open`/`every_n_bars`
schedules; no-look-ahead (`offset>=1` bar); `config_hash` stable for default
(daily) configs; daily portfolio golden unchanged.

**M7 — Surfaces + caches + library.**
`frequency` on REST/CLI/MCP/SDK; `SessionCache` time-axis generalization; L2 key
+ namespace + LRU; `factor_id`/report `granularity` coexistence.
Tests: daily + minute reports coexist in the library; L2 no daily/minute aliasing;
existing surface tests pass with `frequency` omitted.

---

## 12. Open questions & risks

**Top risks (judge-flagged).**

1. **`as_of_ts = bar close` is the entire intraday PIT guarantee** — a single
   wrong line (e.g. session close) silently reintroduces look-ahead with no
   error. Mitigation: dedicated exclusion test in M2; consider a runtime invariant
   that `as_of_ts > ts` for every minute row at ingest.
2. **Silent-corruption-on-regression** of the two load-bearing fixes (`to_frame`
   dtype; segmentation). Mitigation: structural guards (intraday engine raises
   without a session vector; a post-`to_frame` row-count `== T*N` assert in tests)
   plus per-operator daily-no-op golden tests.
3. **Window-tensor blowup** is the dominant scale risk; M4's streaming rewrite is
   non-trivial and must match the materialized kernels exactly (NaN semantics,
   `ddof`, tie handling). Until M4 lands, intraday is limited to small `d`.
4. **`cs_rank` vectorization** must reproduce average-tie ranks and NaN masking
   bit-for-bit, or every Alpha-101 factor shifts subtly.

**Open questions.**

- **EMA across sessions:** default is per-session reset (§5.2). Is a deliberate
  multi-day intraday EMA a real use case worth the `segment_overnight=False`
  escape hatch, or should it be removed to reduce footguns?
- **Annualization basis:** default daily-aggregation (§7.1). Do we ever expose
  per-bar annualization, or hide it entirely? Which bar marks the session NAV for
  aggregation — last RTH bar (16:00) vs an auction/settlement proxy?
- **Resampling label/closed convention** (`left`/`left`) and partial-bar drop at
  the as_of frontier — confirm against how downstream horizons interpret bar
  timestamps; off-by-one here is a PIT leak.
- **`config_hash` stability mechanism** — exclude-default-when-default vs
  version-the-hash. The former is a maintenance trap (every future field must
  repeat the dance); the latter changes the on-disk identity scheme. Pick one.
- **`adv{d}` at minute scale** — translate to `d` sessions of volume vs bind to a
  daily-aggregated companion volume field? The latter is more correct but adds a
  field-mixing concept.
- **Benchmark target** for the M4 perf gate (e.g. "1yr NASDAQ-100 5m single
  Alpha-101 factor < N seconds") — needs a number once streaming kernels exist.
- **Storage budgets** (`l1_memory_gb=4`, `l2_max_gb=20`) were calibrated for
  ~10k-row daily panels; likely need re-tuning for minute, which constrains 1m
  research ergonomics and feeds the memory guard thresholds.
- **Extended-hours research** — pre/post-market bars are stored but excluded by
  default; is there demand for an extended-session research mode, and how does it
  interact with `bars_per_session`/annualization?
