# Minute-Level (Intraday) Backtesting — Engineering Design (FINAL)

Status: FINAL (post adversarial review)
Author: factor-platform
Scope: add minute/intraday backtesting to Assay while keeping the daily path numerically unchanged.

---

## 1. Overview & goals

Assay today is a **daily-only** factor backtesting engine. Panels are `(T dates × N symbols)` matrices whose time axis is a Python `datetime.date`. Every read goes through `DataStore.get_panel(...)` with an `as_of_date`, is point-in-time (PIT) correct (only rows knowable on `as_of` are used; corporate actions applied at read time), an expression engine evaluates factors, an evaluator computes forward returns / IC / decay / turnover, and a portfolio backtester rebalances with cost/execution models.

We add **minute-level** backtesting using the local 1m mirror at `/data/massive_data/us_stocks_sip/minute_aggs_v1/{YYYY}/{MM}/{YYYY-MM-DD}.parquet` (columns `ticker, volume, open, close, high, low, window_start[ns,UTC], transactions`; `window_start` = bar START; ~390 RTH bars/day; ~1.47M rows/day; ~54 GB total).

### Goals

1. **PIT correctness at minute granularity** — no intraday look-ahead. A price bar is knowable only at its *close* (`window_start + step_seconds`). Day-grained events (splits, dividends, index membership) are knowable only at that **session's close**, never at a mid-session `as_of`.
2. **Daily path preserved within a stated tolerance** — existing API/SDK/CLI/MCP and daily golden numbers are unchanged for every milestone that does not rewrite shared kernels; the one milestone that rewrites shared kernels (M4) carries an explicit `rtol/atol` and consciously re-blessed fixtures (see §10). All daily *identities* (`config_hash`, `factor_id`, report JSON keys) are byte-stable.
3. **Reuse the existing design** — parquet stores, read-time adjustment, the row-indexed engine, the pure-numpy evaluator, the index-based backtester are value-opaque on the time axis. We widen the time *type* (Date → Datetime) and add a session dimension; we do **not** fork the numerics.
4. **Scale to 54 GB** — day-partitioned IO, predicate pushdown, lazy read-time resampling to coarser bars, session-chunked + streaming evaluation, and a real (net-new) memory-budget subsystem.

### Synthesis decision

**Unified Timestamp Axis** as the spine (one engine/store/evaluator/portfolio; `freq` is *data*, not a forked code path), grafted with the explicit **`Frequency` value object** (centralizes every granularity constant so no layer hard-codes 252/390), lazy **read-time 1m→5m/15m resampling**, **per-session physical partitions + row-group pruning**, and an **enforced memory-budget subsystem**. We **reject** a parallel `DataStore` class (it duplicates the PIT/corp-action logic, the most dangerous place for silent divergence) — one store branches on `freq` in a few well-named spots.

---

## 2. Data model & storage

### 2.1 The `Frequency` value object — `src/assay/data/frequency.py` (new)

```python
@dataclass(frozen=True)
class Frequency:
    code: str            # "1d" | "1m" | "5m" | "15m"
    base_unit: str       # "day" | "minute"
    multiple: int        # 1, 5, 15
    is_intraday: bool
    time_col: str        # "date" | "ts"
    partition_grain: str # "month" | "day"

    @property
    def step_seconds(self) -> int: ...          # 0(daily)/60/300/900
    @property
    def nominal_bars_per_day(self) -> int: ...  # 1/390/78/26 — sizing & default-horizon HINTS ONLY
    def polars_every(self) -> str: ...          # "5m"/"15m" for group_by_dynamic

DAILY     = Frequency("1d", "day",    1,  False, "date", "month")
MINUTE_1  = Frequency("1m", "minute", 1,  True,  "ts",   "day")
MINUTE_5  = Frequency("5m", "minute", 5,  True,  "ts",   "day")
MINUTE_15 = Frequency("15m","minute", 15, True,  "ts",   "day")

def parse_frequency(code: str | Frequency | None) -> Frequency: ...  # None/"1d"/"daily"->DAILY; else map; ValueError otherwise
```

`nominal_bars_per_day` is used **only** for sizing and default-horizon hints. Every correctness-bearing count (segmentation, scheduling, annualization) derives bars **per session** from the calendar (§3), so half-days/DST never shift boundaries.

### 2.2 Schemas — `src/assay/data/schemas.py`

Daily `PRICE_RAW_SCHEMA` (lines 35–46) is **untouched** (any change forces a daily-store rebuild because `upsert_parquet` raises on schema mismatch). Add a parallel minute schema:

```python
PRICE_RAW_MINUTE_SCHEMA: dict[str, pl.DataType] = {
    "ts":           pl.Datetime("ns", "UTC"),  # event_time = bar OPEN (window_start), stored UTC (DST-unambiguous)
    "session_id":   pl.Int32,                   # ET trading date YYYYMMDD; segmentation + corp-action join key
    "symbol":       pl.Utf8,
    "open":         pl.Float32,
    "high":         pl.Float32,
    "low":          pl.Float32,
    "close":        pl.Float32,                  # unadjusted
    "volume":       pl.Float32,
    "transactions": pl.Int64,
    "as_of_ts":     pl.Datetime("ns", "UTC"),    # knowledge_time = bar CLOSE = ts + step_seconds — THE PIT line
    "session_close_ts": pl.Datetime("ns","UTC"), # ET session close as UTC; the EOD-knowability instant for day-grained events (§4.4)
    "session_type": pl.UInt8,                    # 0=RTH, 1=pre, 2=post
    "source_id":    pl.Utf8,                     # provenance: per-day flat-file key
}
```

- `ts`/`as_of_ts`/`session_close_ts` are **UTC tz-aware on disk**; ET conversion happens only for derivation and for the panel axis surfaced to the engine.
- `session_id` (Int32 `YYYYMMDD`) is the cheap segmentation key carried into the engine as a `(T,)` vector and the join key to day-grained stores. A `date: pl.Date` is *not* stored; it is derived on read only for the corp-action join.
- `session_close_ts` is **stored per bar** specifically so the day-grained-event knowability cut (§4.4) is a pure column comparison with no per-read calendar call.
- `adj_events` and `universe_snapshots` schemas are **unchanged**.

### 2.3 Partition layout

Daily `price_partition_path` (schemas.py:74) is **unchanged** (zero migration). Add a frequency-aware path and a **day-level** partition for minute:

```python
def price_partition_path(data_dir, market, year, month, *, freq=DAILY, day=None) -> Path:
    if not freq.is_intraday:                      # EXISTING daily path, byte-identical (no "freq=" level)
        return data_dir/"price_raw"/f"market={market}"/f"year={year:04d}"/f"month={month:02d}"/"price_raw.parquet"
    return (data_dir/"price_raw_minute"/f"market={market}"/f"year={year:04d}"
            /f"month={month:02d}"/f"day={day:02d}"/"price_raw_minute.parquet")
```

Only the **canonical 1m store** is materialized; 5m/15m are **lazy read-time resamples** (§4.3). Each day file is written with `pl.write_parquet(..., row_group_size=R)` where **R is tuned so each row group holds a contiguous block of whole symbols** (file pre-sorted by `["symbol","ts"]`), so a 100-of-10,144-symbol `is_in` prunes by row-group statistics on `symbol` instead of scanning every group. R is *verified empirically* (§8.1), not assumed. Compression: zstd.

### 2.4 Ingestion of `minute_aggs`

`MassiveConfig` (`src/assay/config.py`): `minute_aggs_subdir: str = "us_stocks_sip/minute_aggs_v1"` + `minute_aggs_dir` property.

`LocalFlatFiles` (`src/assay/data/massive/flatfiles.py`) parameterized by `freq`:

```python
class LocalFlatFiles:
    def __init__(self, config, freq: Frequency = DAILY):
        self.root = config.minute_aggs_dir if freq.is_intraday else config.day_aggs_dir
    def list_aggs(self, start, end) -> list[AggFile]: ...      # generalizes list_day_aggs (file-listing handles holidays)
    def read_minute_agg(self, date, symbols=None) -> pl.DataFrame | None:
        # reuse existing window_start->ET conversion but DROP the trailing .dt.date(); keep tz-aware ts
```

Normalizer + ingester (`src/assay/data/ingest/prices.py`):

```python
def normalize_minute_agg(df, source_id, freq, session) -> pl.DataFrame:
    # session_id, session_type from ET wall-clock vs calendar open/close;
    # as_of_ts = ts + freq.step_seconds  (BAR CLOSE) — the single PIT-critical line;
    # session_close_ts = session.close (UTC)         — the EOD-knowability instant.
    ...

class MinutePriceIngester:                 # per-DAY atomic write, NOT month-wide upsert
    def run(self, start, end, symbols=None) -> dict:
        for f in self.client.list_aggs(start, end):
            norm = normalize_minute_agg(self.client.read_minute_agg(f.date, symbols),
                                        f.key, self.freq, session_open_close(f.date))
            path = price_partition_path(self.config.data_dir, self.config.market,
                                        f.date.year, f.date.month, freq=MINUTE_1, day=f.date.day)
            write_parquet_atomic(norm.sort(["symbol","ts"]), path)   # idempotent: re-ingest overwrites the day
```

`upsert_parquet` is **not** used (a month is ~30M rows; read-modify-write is prohibitive). Dedup within a file is on `["ts","symbol"]` (never `["date","symbol"]`, which would collapse 390 bars to one row). Pre/post-market bars are tagged via `session_type` and kept on disk but **excluded by default at read**. An ingest-time assertion checks the derived RTH bar count equals `bars_per_session(day)` (catches calendar/data drift).

---

## 3. Time axis & calendar

### 3.1 Bar grid
The intraday panel time axis `T` is the ordered set of bar timestamps present, surfaced as `pl.Datetime("ns","America/New_York")` named `ts`. Daily is the special case `time_col=="date"` (`pl.Date`). The engine's axis construction (`np.unique`/`np.searchsorted`, engine.py:112–115) is dtype-agnostic over `datetime64[ns]` — verified, zero kernel change.

### 3.2 Calendar additions — `src/assay/data/calendar.py`
`trading_days`/`is_trading_day` are kept. Add (all wrapping `exchange_calendars` XNYS, already DST/half-day aware):

```python
def session_open_close(day, calendar="XNYS") -> tuple[datetime, datetime]: ...  # ET; half-days -> 13:00 close
def session_bars(day, *, freq=MINUTE_1, include_extended=False) -> list[datetime]: ...  # authoritative bar starts; half-day -> ~210/42/14
def bars_per_session(day, *, freq=MINUTE_1, include_extended=False) -> int: ...
def session_type(ts_et, day) -> int: ...           # 0/1/2
def session_ids(time_index_et) -> np.ndarray: ...  # (T,) Int32 YYYYMMDD — the engine segment vector
def session_count(start, end) -> int: ...          # distinct trading sessions in span
```

Half-days, pre/post-market, and DST all fall out of `session_open_close` + the UTC↔ET conversion; nothing assumes 390.

### 3.3 `periods_per_year` (annualization)

```python
def periods_per_year(start, end, *, freq) -> float:
    sessions = trading_days(start, end)
    total_bars = sum(bars_per_session(d, freq=freq) for d in sessions)
    span_years = max((end - start).days, 1) / 365.25     # ACTUAL calendar span, not 252-nominal
    return total_bars / span_years
```

The denominator is the **actual calendar span** (no 252 hard-code), and is consistent in units with the measured numerator (resolves the §3.3 252-hard-code finding). This `"bar"` basis is **opt-in**; the **default** annualization aggregates per-bar NAV to one point per session and uses the conventional `ppy=252` (§7.1), where 252 is documented as a daily convention and confined to that path.

---

## 4. Point-in-time semantics intraday

The daily PIT guarantee is structural: every store read filters `as_of_date <= as_of` (datastore.py:95,106) and `_as_date` truncates datetimes (datastore.py:33–38). We widen the time type, add a **per-bar knowledge time**, and add an **EOD-knowability cut for day-grained stores**.

### 4.1 Per-bar knowledge time
Ingest sets `as_of_ts = ts + freq.step_seconds` (bar close). The 10:30 bar becomes knowable at 10:31; `as_of=10:30:30` excludes it, `as_of=10:31:00` includes it. **This single line is the entire intraday price no-look-ahead guarantee** and gets a dedicated exclusion test (§11). An ingest assert enforces `as_of_ts > ts` on every row.

### 4.2 `get_panel` widening — `intraday as_of must dominate end`

```python
def _as_time(value, *, freq):
    return _as_date(value) if not freq.is_intraday else _parse_et_datetime(value)  # daily byte-identical
```

`DataStore.get_panel(..., *, freq=DAILY)`:
- **daily branch unchanged** — filters `date`, `as_of_date`; returns `["date","symbol",*fields]`.
- **minute branch**: parse ISO datetimes; then **clamp the effective end**:

  ```python
  effective_end = min(end, as_of)        # RESOLVES the "adjustment basis = end > as_of" finding
  if as_of < end: log/optionally raise   # no bar later than as_of may be returned OR used as the adjustment basis
  ```

  Filters become `pl.col("ts").is_between(start, effective_end)`, `pl.col("as_of_ts") <= as_of`, `session_type == 0` (unless extended). The forward-adjust basis is computed toward `effective_end`, never `end`. Returns `["ts","session_id","symbol",*fields]`.

  **Invariant (new, stated in code & test):** `effective_end = min(end, as_of)`; a read with `end > as_of` returns *identical* adjusted values to a read with `end == as_of`. Test asserts this.

The daily 10-calendar-day dividend lead-in (`_DIV_LOOKBACK_DAYS`, datastore.py:30,144) is **replaced for minute** by reading only the **prior session's last RTH bar** (via `session_open_close` on the session before `start`). This lead-in bar is consumed **only inside `forward_adjust`** to supply `close_prev`; it is **never surfaced as a factor row** (so it cannot seed a window — see §5.2 / §8.2).

### 4.3 Resampling preserves PIT — lazy, session-anchored, calendar-complete

1m→5m/15m happens **inside `DataStore`, pushed into the LazyFrame before `.collect()`** so the engine only ever sees the coarse panel and (per §8) the 1m frame is **never fully materialized**:

```python
(lf.filter(pl.col("as_of_ts") <= as_of)                      # PIT cut first
   .group_by_dynamic("ts", every=freq.polars_every(),
                     closed="left", label="left",
                     group_by=["symbol","session_id"],         # anchor WITHIN a session — never straddle the open / mix pre-market
                     start_by="datapoint")                      # first bin starts at the session's first RTH bar
   .agg(open=first, high=max, low=min, close=last,
        volume=sum, transactions=sum,
        as_of_ts=max("as_of_ts"),
        n_constituents=count())
   .join(expected_bin_counts(freq, sessions), on=["session_id","ts"])  # calendar-expected count per SPECIFIC bin
   .filter(pl.col("n_constituents") == pl.col("n_expected"))   # emit ONLY calendar-complete bins; drops partial frontier bin entirely
   .collect())
```

PIT rules (resolves the "partial-bar completeness" and "alignment to session open" findings):
- Grouping is **anchored within `session_id`** with `start_by="datapoint"`, so coarse bars are `[09:30,09:35),...` and never straddle the open or mix in pre-market bars.
- A coarse bar is emitted **only if all its calendar-expected 1m constituents are knowable by `as_of`** — completeness is checked against `n_expected` for *that specific bin* (from `session_bars` at the coarse freq, so the genuinely-short final/half-day bin has the correct smaller expected count), never a nominal 5/15. A mid-coarse-bin `as_of` drops the whole bin (no partial close), guaranteeing the coarse `close` is deterministic and reproducible.
- The coarse `as_of_ts = max(constituent as_of_ts) = bin_end`; asserted equal to `bin_end` only for complete bins.

### 4.4 Corp actions & universe — EOD-knowability cut (resolves the CRITICAL look-ahead)

Day-grained events (`adj_events`, `universe_snapshots`) are knowable only at the publishing **session's close**, *not* at a mid-session `as_of`. We do **NOT** coerce an intraday `as_of` down to its date. Instead we filter against the EOD-knowability instant:

```python
# adj_events / get_universe, intraday:
known = ((pl.col("as_of_date") <  as_of.date())                                  # earlier session: fully knowable
       | ((pl.col("as_of_date") == as_of.date()) & (as_of >= session_close(as_of.date()))))  # same session: only after close
events = df.filter(known)
```

Equivalently: derive `as_of_known_ts = session_close(as_of_date)` for day-grained rows and filter `as_of_known_ts <= as_of`. Daily collapses to the existing `as_of_date <= as_of` (a daily `as_of` *is* end-of-day).

Consequence: a split/dividend/membership change with `as_of_date == D` is **invisible** to a minute read at `D 10:00` and **visible** at `D 16:00`. Dedicated tests for both a split and the dividend ratio.

The adjustment math (`src/assay/data/store/adjust.py`) becomes session-aware (resolves the daily-grain bisect bug):
- Build a per-symbol **unique-session array** (sorted `session_id`s), `bisect` `ex_date`'s session against *that* (not the per-bar array), cut at the **first bar of the ex-date session**, set `close_prev` = the **last RTH bar of the prior session**.
- Replace `_MAX_PRIOR_GAP_DAYS` with a **prior-session-existence** check.
- The per-session factor (`split_factor * div_factor`) is **broadcast across all bars of the session** via a `session_id`→factor join (see §7-perf / §8 for the vectorized rewrite). Daily degenerates to today's behavior (one session = one row), so the daily code path is identical.

Intraday eligibility = `membership-known-by(as_of) AND finite-bar`. A symbol that first-lists or is halted intraday surfaces as NaN bars (not silently absent rows that shift the cross-section); a note + test cover this.

---

## 5. Engine changes

Core math is row-indexed and value-opaque. Three additive frequency-gated changes plus two perf rewrites. **Enforcement gate (lands in M3, before any surface exposes minute):** `FactorEngine.__init__` **raises** if `freq.is_intraday and session_ids is None` — an intraday panel cannot be evaluated without segmentation.

### 5.1 Time-axis name/dtype (the one hard corruption bug)
`FactorResult.to_frame` (engine.py:79–93) hard-casts the time column to `pl.Date` (line 86), collapsing ~390 bars/day into duplicate `(date,symbol)` rows. Fix: record source dtype + time-col name and emit them back:

```python
class FactorEngine:
    def __init__(self, panel, group_data=None, *, time_col="date", session_ids=None, freq=DAILY):
        ...
        self._time_col, self._time_dtype = time_col, panel.schema[time_col]   # Date | Datetime
        self._session_ids, self._freq = session_ids, freq
        if freq.is_intraday and session_ids is None:
            raise ValueError("intraday engine requires a session_ids segment vector")
```
`to_frame` emits `self._time_col` cast to `self._time_dtype` (daily→`date:Date` unchanged; minute→`ts:Datetime`). `EvalContext` gains `session_ids`.

### 5.2 Overnight-gap / session-boundary handling, reconciled with carry-over

`operators/_base.windows(x, d, session_ids=None)`: when given `session_ids`, a window at row `t` is valid only if `rows_since_session_start[t] >= d-1` (same NaN mechanism as warm-up, _base.py:33–35). `ts_delay/ts_delta/ts_returns/ts_log_returns` (time_series.py:18–44): a shift crossing a session boundary yields NaN. **`ts_ema`/`ts_dema` reseed `prev = NaN` at each session start** (the current kernel at time_series.py:121–122 *carries `prev` across NaN gaps* — across the overnight boundary that is look-back, a correctness hole).

**Carry-over reconciliation (resolves the "buffer reintroduces leakage" finding):** for session-chunked evaluation (§8.2), the `max(window)-1`-bar carry-over buffer from the prior session **carries its true `session_ids`**, and segmentation (NaN-pad / EMA reseed) is applied **after concatenating buffer+chunk**, so a buffered prior-session bar is a different segment exactly as in the whole-panel case. The dividend lead-in bar (§4.2) is excluded from the engine panel entirely. A **chunked-vs-whole-panel golden test** asserts byte-identical factor values.

**Cross-session vs sub-session windows (resolves the CRITICAL all-NaN finding):** segmentation is the policy **only for sub-session windows**. A multi-day window (e.g. `'20d'` = 7800 bars > a 390-bar session) under per-session segmentation would be structurally all-NaN. So each windowed operator takes an explicit `segment: "session" | "continuous"` mode:
- **`segment="session"`** (default for windows `< bars_per_session`): NaN-pad at session start; window is session-local.
- **`segment="continuous"`** (auto-selected when the lowered window `>= bars_per_session`, e.g. multi-day windows): the window legitimately spans prior sessions via the carry-over buffer; the streaming kernels (§8.2) checkpoint state across chunk boundaries. Sub-session bleed is intentional here because the horizon is explicitly multi-day.

This is decided per operator from the resolved (post-`Nd`-lowering) window length, so `ts_mean(close,'20d')` is **not** all-NaN on a multi-day minute panel (asserted by test). Warm-up cost (first `d-1` bars per continuous series) is documented.

**EMA across sessions (resolves the "long EMA meaningless" finding):** bar-level `ts_ema(x,d)` is meaningful only for sub-session `d` (per-session reset). For a genuine **multi-day** smoother we provide `ts_ema_daily(x, d_days)`: compute the EMA on the **session-aggregated** series (last-RTH-bar per session) with a day time-constant, then broadcast back to bars by `session_id`. Bar-level `ts_ema` **raises** when `d >= bars_per_session` under `segment_overnight=True`, steering callers to `ts_ema_daily`; `segment_overnight=False` remains available but documented as carrying state across gaps.

### 5.3 Window units — runtime session-aware, not a parse-time scalar (resolves the half-day finding)

`_coerce_window` (parsing.py:354) and the daily integer path are untouched. The `'Nd'` convention is **not** baked to a single `LitNode(N*390)` at parse time. Instead a `'Nd'` literal lowers to a `DayWindowNode(N)` that the engine resolves **per row** against `session_ids`: "look back to the bar `N` session-boundaries earlier." This requires a window-operator variant accepting a per-row span (the `segment="continuous"` path already walks by session). For the common case where an exact span is unnecessary, the nominal `N * nominal_bars_per_day` is used as an approximation **with a logged note quantifying the half-day/DST error** — we do **not** claim "never shifts." `adv{d}` (parsing.py:301–303) and bare `returns`→`ts_returns(close,1)` (parsing.py:346–347) bind to `'{d}d'`/`'1d'` so Alpha-101 keeps day semantics; with no intraday context the `d` suffix equals a raw int (daily unchanged).

**Arg-extreme / rank output units (resolves the silent unit-change finding):** `ts_argmax`/`ts_argmin` return "bars since," and `ts_rank` is a within-bar-window percentile. For a day-denominated window these are explicitly documented as **bar counts**; the registry `output_range` and diagnostics labels are updated to say "bars," and a `*_days` helper divides by `bars_per_day` when a caller wants day units. Added to the Alpha-101 parity tests so downstream thresholds aren't silently rescaled ~390×.

**Returns at session open (resolves the minor finding):** bare `returns` → `ts_returns(close,1)` is NaN at the first bar of every session under segmentation (correct no-overnight-return behavior). This is documented; the 09:30 cross-section is known-degraded for returns-derived factors. A session-aware `overnight_return` operator (prior-session-close→open) is offered for callers who want a daily-equivalent return rather than relying on bar-level `ts_returns`.

### 5.4 Cross-sectional ops — vectorize `cs_rank` (the real Alpha-101 hotspot)
`cs_rank` (cross_sectional.py) is `np.vstack([_rank01_row(x[t]) for t in range(T)])` — a per-row Python loop with an inner tie while-loop; `rank` is the most common Alpha-101 op and dominant at `T≈98k`. Vectorize via double-`argsort` over `axis=1` with NaN masking and average-tie handling (`scipy.stats.rankdata` semantics), falling back to the per-row path if `scipy` is absent. Same for the `rank` branch of `_group_apply`. **This is a shared kernel** — see §10 for the daily tolerance/gating decision.

### 5.5 Diagnostics
`diagnostics.output_diagnostics` thresholds (`warmup_frac`, `min_coverage`, 252-row assumptions) are parameterized by `freq`; `n_dates` → `n_periods` (additive; daily keeps `n_dates`). AST, parser grammar, registry, arithmetic/math kernels: **zero change**.

---

## 6. Forward returns & evaluation at minute horizons

`metrics.py` IC/RankIC, `decay.py`, `turnover.py`, `groups.py` are per-row math — **zero code change**. Units live at the call boundary (`service.py`). Two changes in `forward_returns.py`.

### 6.1 `forward_returns` — bar triple masking + explicit policy enum (resolves the masking-bypass finding)

```python
def forward_returns(close, open_, horizons, execution="next_open", *,
                    session_ids=None, entry_lag=1,
                    cross_session: str = "mask_any_crossing"):  # | "allow_whole_day"
```

Masking is keyed on the **actual three-bar triple per row** — signal `t`, entry `t+entry_lag`, exit `t+entry_lag+h` — using `session_ids`, never on a `h == nominal_bars_per_day` comparison:
- **`mask_any_crossing`** (default): valid only if `session_id[t+entry_lag] == session_id[t+entry_lag+h]` (and the signal→entry crossing is documented; for the day-equivalent-entry convention where `entry_lag = bars_to_next_session_open` the signal→entry crossing is intentional and not masked, but exit must share the entry's session).
- **`allow_whole_day`**: whole-day horizons are defined by **session count** — `session_index(exit) == session_index(entry) + k` via `session_ids` — so half-days are correct (never by bar-count equality).

`entry_lag` generalizes the hard-coded `t+1` skip (line 90; default 1 = daily). The stale `execution=="vwap"` `ValueError` (lines 63–67) becomes a real per-bar typical-price `(h+l+c)/3` (or transactions-weighted) branch now that intraday bars exist. Masking happens **inside** the function so it cannot be forgotten.

### 6.2 Units at the service boundary
- `decay_halflife` returns half-life in horizon units (bars for minute). Service stores `decay_halflife` + `granularity` (tagged) and **also** a derived `decay_halflife_days` (`÷ bars_per_day`, documented as an approximation that can round small values).
- `turnover` lag defaults to 1 daily; minute uses `lag = bars_per_day` (once-per-day churn; `lag=1` is meaningless minute autocorrelation).
- Minute default horizons `default_horizons_minute = (1, 5, 30, 390)` in `AssayConfig`.
- The session forward-return memo key (service.py:180) becomes `f"fwd::{freq.code}::{execution}"` so a shared session never serves a daily matrix for a minute request (assertion test).

---

## 7. Portfolio backtest intraday

All numeric stages (`accounting.py`, `weights.py`, `constraints.py`, `signal.py`, `execution.py`, `costs.py`) are granularity-neutral and reused. Changes concentrate in annualization, scheduling, units, labels.

### 7.1 Annualization (default = aggregate to daily)
`_PPY = 252` exists in **two** places (metrics.py:34, backtester.py:72). Replace both with a config-derived `periods_per_year` threaded into `compute_metrics`; every metric fn already accepts a `ppy`/`periods_per_year` param. **Default for intraday:** `compute_metrics` first aggregates per-bar NAV to one point per `session_id` (compounding within each session), then annualizes with `ppy=252` — avoiding the `sqrt(390)` microstructure bias and reusing the proven 252 path. `annualization_basis: "daily"|"bar"` defaults to `"daily"`; `"bar"` uses §3.3. Risk-free de-annualization (backtester.py:495) uses the same per-period divisor.

### 7.2 Rebalance scheduler (`rebalance.py`)
`_REBALANCE_TYPES` (config.py:40) keys on calendar fields. Add intraday families + dispatch + **enum members + validator update** (treated as a *breaking enum expansion*, see §10): `every_n_bars` (stride over row index), `at_open`/`at_close` (first/last RTH bar per session — **redefines `daily` for minute** to once-per-session, not every row), `at_time HH:MM` (ET wall-clock bar per session). Grouper keys on `session_id`; weekly/monthly group on each bar's **session date** so 390 bars don't collapse.

### 7.3 Execution, costs, accounting — dual offset field with explicit precedence (resolves the offset finding)
- Add `execution_offset_bars` (intraday-facing, daily-neutral default) alongside `execution_offset_days` (config.py:83, range [1,3], validator at config.py:206 kept). **Precedence:** `execution_offset_bars` is authoritative when `bar_interval != "day"`; otherwise `execution_offset_days` is authoritative. Validator updated to range-check the bar field only on the intraday path. The no-look-ahead invariant (`offset >= 1` bar) is preserved.
- `cov_window`/`adv_window` (config.py:94,136) accept a `'Nd'` unit converted to bars at consumption; **risk-aware weight methods run on session-aggregated returns** (bounds the Ledoit-Wolf `O(W)` loop and SLSQP QP). To keep `from_dict` of persisted payloads valid, these stay **int** (bar counts) with an optional string form parsed in `__post_init__`; the on-disk type is unchanged.
- `_exec_price_matrix` (backtester.py:417–423): `vwap`/`arrival` use genuine per-bar prices; `next_open` is the next *bar* open.
- ADV/capacity baseline = per-session daily volume so participation caps stay calibrated.
- `output_frequency` gains a daily-from-minute downsample; `_sample_series` (backtester.py:616–653) groups minute NAV by `session_id`. `n_trading_days` reports **distinct sessions**; add `n_bars`.
- The `accounting.py` per-row loop (accounting.py:141) and `_benchmark_nav` loops (backtester.py:498,505) are vectorized (cumulative-product NAV between rebalances) so they don't walk 98k bars.

---

## 8. Scale & performance

Corrected memory math: one `(T,N)` field pivot at `98k×100×8` ≈ **78 MB** (not 8 GB). The **window tensor** and **concurrent in-memory copies** are the real risks.

### 8.1 IO
Day-level partitions + a freq-aware enumerator (per-day files via `trading_days`, skipping holidays). Push **all** filters into `pl.scan_parquet` (symbol `is_in`, `ts` range, `as_of_ts <= as_of`, `session_type==0`). Dividend lead-in reads **one** prior-session file. **Verify pruning empirically:** `scripts/bench_alpha101.py` minute variant measures rows-read vs rows-returned for a 100-symbol read and is the gate that fixes `row_group_size`. Quantify the 252-files/year scan fixed cost; if a multi-month read pays too much open/stat overhead, evaluate a coarser (weekly) physical partition.

### 8.2 Memory — the window tensor + chunked evaluator
`windows()` returns a `(T,N,d)` `sliding_window_view`, zero-copy until a reduction materializes it. At `T=98k,N=100,d=390` that is ~30 GB for one op; `'20d'` (7800) ~600 GB. Fixes:

1. **Streaming/cumulative kernels** (M4): `ts_mean/sum/std/cov/corr` → cumulative-sum / Welford rolling; `ts_min/max/argmax/argmin` → monotonic-deque; `ts_rank` → rolling order-statistic; `ts_decay_linear` → incremental weighted sum. Never materialize `(T,N,d)`.
2. **Session-chunked evaluator (specified, resolves the under-specification finding):** chunk **along axis-0 (time) only** — cross-sectional ops (`cs_rank`, `cs_demean`, neutralization) need the full cross-section per row but are per-row independent of T, so chunking does not affect them (stated explicitly). `max_window` is computed from the **parsed AST** (in bars, post-`Nd` lowering); require `chunk_sessions × bars >= max_window` or **fall back to whole-panel**. For `segment="continuous"` windows the streaming kernels **checkpoint/restore state across chunk boundaries** via the session-tagged carry-over buffer (§5.2). Test: a `'20d'` window on minute data over a chunk-spanning boundary equals the unchunked result.
3. **Float32 — disk-only by default (resolves the contradiction):** Float32 on disk saves disk, not RAM, because every pivot/kernel hardcodes float64 (engine.py:131–132, session.py:58–59, _base.py:30, l2.py:130/139). We therefore **drop the float32-in-RAM claim** for the default path. A genuine float32 compute path is a *separate, opt-in* effort (parameterize `windows()/_matrix()/_panel_to_matrices`/streaming kernels by dtype and re-validate NaN/ddof/precision) — not sold as "free, gated."

**Eliminate the triple in-memory copy (resolves the double/triple-copy finding):** today a session run holds the long-frame panel + `FactorEngine._matrix_cache` (engine.py:119,133) + a *second independent* `SessionCache._panel_to_matrices` pivot (session.py:111,49–61) ≈ 1 GB+ before any tensor. Fix: `SessionCache` **borrows the engine's `_matrix_cache` by reference** instead of re-pivoting, and the engine drops `self._panel` once matrices are cached. The true per-session footprint is stated as `n_fields × T × N × 8 × ~1.x` + long frame, and `batch()`'s ThreadPoolExecutor concurrency multiplies it — accounted against the memory budget (§8.4).

### 8.3 Caches
- `SessionCache` pivots on `time_col` (default `date`; `ts` for minute) — fixes the `np.unique` on `date` collapsing 390 bars (session.py:49–56).
- `L2FactorCache` key (l2.py:78–105) currently keys on date-string `period` + universe/adj/market — **no time-of-day, no `as_of`**. Two intraday runs over the same calendar dates but different intraday window/`as_of` would collide and serve a 14:00 matrix for a 16:00 request. **Fix (resolves the L2-key finding):** extend the preimage with the full intraday period `(start_ts, end_ts)` and `as_of_ts`; for daily these collapse to the existing date strings so the **daily key is byte-unchanged**. Bump namespace `assay-l2-v1`→`v2`. Add a **real byte-accounted LRU** (today only `clear()`, l2.py:178). **L2 is wired into `evaluate()` only after** the key carries the as_of axis and the LRU exists.
- **Cache-key cardinality (resolves the explosion finding):** intraday `as_of` is per-bar (~390/day), multiplying L2 entries and SessionRegistry sessions. Policy: **only cache at session-close `as_of` by default** (or quantize `as_of` to a coarse bucket), bounding the key space; `SessionRegistry` gets a **byte/size cap with LRU eviction** and a documented expected-sessions budget for minute (today expiry is manual, session.py:236). Capacity arithmetic redone including the `as_of` and `freq` multipliers.

### 8.4 Memory-budget subsystem (NET-NEW — its own milestone, resolves the "vaporware" CRITICAL)
`l1_memory_gb=4.0`/`l2_max_gb=20.0` (config.py:120–121) are currently read **nowhere**. The guard is built from scratch:
- **(a) Pre-flight estimate** from partition file sizes × symbol/row-group pruning ratio × `freq.step` × dtype — independent of `.collect()`.
- **(b) Enforced at `DataStore.get_panel` before `.collect()`** (datastore.py:99): if estimate > budget, raise an actionable error (suggest coarser `freq` / shorter window / chunked mode) or auto-switch to the chunked/streaming path.
- **(c)** Byte-accounted LRU in `L2FactorCache` + per-session byte cap with eviction in `SessionRegistry`.
- **(d)** Re-tune `l1_memory_gb`/`l2_max_gb` defaults (sized for 10k-row daily panels) for minute, fed by the bench.

### 8.5 Wall-clock gate
`scripts/bench_alpha101.py` minute variant is a perf gate (target set once streaming kernels land, §12). Streaming kernels + vectorized `cs_rank` + 5m-default make a 1yr NASDAQ-100 single-factor eval tractable.

---

## 9. API / SDK / CLI / config surface

One optional `frequency` (alias `freq`/`granularity`, default `"1d"`) threaded everywhere; **omitting it reproduces today's behavior** — verified by a surface-contract test (below), not merely asserted. The edit set spans ~15 files; three carry behavior and get their own tests.

- **`AssayConfig`**: `default_frequency="1d"`, `default_horizons_minute=(1,5,30,390)`, `annualization_basis="daily"`; `MassiveConfig.minute_aggs_subdir`/`minute_aggs_dir`.
- **`DataStore.get_panel(fields, symbols, start, end, as_of, adj, *, freq=DAILY)`** — minute accepts ISO datetimes; `effective_end=min(end,as_of)` (§4.2).
- **`FactorEngine.__init__(panel, group_data=None, *, time_col="date", session_ids=None, freq=DAILY)`**; `from_store(..., freq="1d")` builds the session vector and routes store/schema/layout; intraday-without-session-vector raises.
- **`forward_returns(..., *, session_ids=None, entry_lag=1, cross_session="mask_any_crossing")`**.
- **`AssayService.evaluate/batch/create_session/correlation_matrix(..., frequency="1d")`**. The non-pass-through spots are enumerated as explicit sub-tasks with tests: **(1)** freq-folded **session memo key** (assert daily+minute in one session don't collide); **(2)** `_resolve` (service.py:106–123) branches on freq for **period/as_of parsing AND horizon/turnover-lag/group-returns-horizon defaults**; **(3)** per-surface `service_kwargs()` drop-None for `frequency`.
- **`as_of`/period parsing (resolves the overload finding):** in **CLI**, when `--frequency` is intraday parse `--start/--end/--as-of` with `datetime.fromisoformat` (accepts date *or* datetime); when daily keep `_date` (which raises on a datetime). At the **service boundary, raise** if an intraday time component is supplied with `freq="1d"` (it would silently truncate). REST field descriptions/validation accept ISO datetime when intraday. Tests: `datetime as_of + freq=1d` rejected; `datetime as_of + freq=1m` honored.
- **Portfolio config**: `bar_interval`/`annualization_basis`; intraday `rebalance_type` values; `execution_offset_bars` (precedence §7.3); intraday `output_frequency`.
- **REST** (`api/models.py`, `routes/portfolio.py:79`): optional `frequency`; `service_kwargs()` drop-None forwards it; `build_config` rejects unknown intraday enum values with an actionable message naming the required `schema_version` (§10).
- **CLI** (`cli.py`): `--frequency`; ISO-datetime parsing in intraday mode; `--horizons` accept `Nd`/`Nm`; new `assay ingest-minute`.
- **MCP** (`mcp/server.py`): `frequency` (default `"day"`) on evaluate/batch/correlation; `assay_system_status` reports resolved frequency + minute defaults.
- **Surface-contract test:** call every REST/CLI/MCP/SDK entry with `frequency` omitted and assert the resolved service kwargs are identical to pre-change.

---

## 10. Backward compatibility & migration

Zero daily migration; structural, not aspirational.

- **Storage**: daily `PRICE_RAW_SCHEMA`, `price_partition_path` (month), `adj_events`, `universe_snapshots` untouched. Minute is a **new store** (`price_raw_minute`); no rebuild.
- **Reads**: `_as_date` keeps truncating datetimes for `freq=DAILY`; daily `get_panel` returns the identical `date`/`pl.Date` frame.
- **Engine**: only edits to shared code are `to_frame` casting back to the recorded dtype (no-op for daily) and new `time_col`/`session_ids`/`freq` params defaulting to today's behavior. Segmented paths gate on `session_ids is not None` → daily byte-identical.

- **`config_hash`/`run_id` (resolves the CRITICAL):** `config_hash()` hashes `json.dumps(self.to_dict())` and `to_dict()` is a plain `asdict()` consumed by `from_dict`, REST `build_config`, reports, and the WebUI. We **do NOT touch `to_dict()`**. Instead introduce a frozen `_HASH_FIELDS_V1` allowlist (the fields that exist today) and compute `config_hash()` over **only those fields when `bar_interval=="day"`**; new intraday fields are excluded from the daily preimage entirely. A regression test asserts `config_hash()` of a default US config is **byte-identical to the committed pre-change value**. Decided in M0/M6 (not an open question).

- **`compute_factor_id` (resolves the major):** signature becomes `compute_factor_id(expr_canonical, granularity="1d")` with the **load-bearing conditional**: `return hash(expr) if granularity=="1d" else hash(f"{expr}::{granularity}")`. Both daily call sites (service.py:313,374) pass the resolved frequency. A test pins `compute_factor_id(expr) == compute_factor_id(expr,"1d") == <committed legacy digest>`, so `library.save()` (store.py:84) never re-keys existing daily `.json` files.

- **Schema versioning for enum expansion (resolves the cross-version major):** `from_dict` ignores unknown keys (protects new code reading old payloads) but old code rejects new enum values (`rebalance_type="at_open"` fails `_enum`). Enum expansion is therefore a **breaking schema change**: bump `schema_version` into `config.to_dict()` and the report JSON; old readers fail **loudly** with a message naming the required version. `build_config`/`from_dict` reject unknown intraday enums with an actionable error.

- **Shared-kernel rewrite vs "byte-identical" (resolves the major):** M4 streaming `ts_*` and the §5.4 `cs_rank` vectorization are **shared** kernels; cumulative-sum rolling variance/cov is **not** bit-identical to the windowed computation, and a vectorized tie-break can differ. We resolve by **gating the streaming kernels behind `freq.is_intraday` and keeping the materialized path for daily** (so daily stays byte-identical) — except where a deliberate decision is made to adopt streaming for daily large-`d`, which is done **consciously** with golden fixtures re-blessed under an explicit `rtol/atol`. The doc's "byte-identical" promise is **downgraded to "within stated tolerance" only for any milestone that changes a shared daily kernel**, and the per-operator gating is stated explicitly. `cs_rank` is validated against the current implementation with average-tie/NaN parity tests; if any divergence remains it ships gated to intraday.

- **Report JSON field aliasing (resolves the minor):** new tagged fields (`granularity`, `decay_halflife`, `n_periods`, `turnover`) are additive. For **minute** reports `n_dates` = **distinct-session count** (matching its name), `decay_halflife_days` documented as derived/approximate; WebUI/agent read the tagged fields when `granularity!="1d"`. A serialization test asserts that for `granularity="1d"` the JSON keys/values are byte-identical to pre-change.

- **L2 namespace** bump only affects the (currently unused) L2 cache; daily key byte-unchanged (§8.3).

---

## 11. Phased implementation plan

**Framing (resolves the incrementality finding):** every milestone is **daily-safe** (changes freq-gated). The **minute path is silently-wrong until M3+M5 land**, so `frequency != "1d"` on the public surfaces (REST/CLI/MCP/SDK) raises `NotImplementedError` until M5 completes; the §5.1 intraday-requires-session-vector guard lands in M3. Each milestone below is independently shippable **and** independently testable.

**M0 — `Frequency` + config + calendar + identity guards (no behavior change).**
`frequency.py`; `MassiveConfig.minute_aggs_dir`; `AssayConfig` minute defaults; calendar helpers (`session_open_close`, `session_bars`, `bars_per_session`, `session_ids`, `session_type`, `session_count`); **`config_hash` `_HASH_FIELDS_V1` allowlist** + regression test; `compute_factor_id(expr, granularity="1d")` + legacy-digest pin; `schema_version` field.
Tests: half-day bar count, DST boundary, `config_hash`/`factor_id` byte-stability vs committed digests.

**M1 — Minute ingestion + minute store schema/layout.**
`PRICE_RAW_MINUTE_SCHEMA` (incl. `session_close_ts`), `price_partition_path(freq=...)`, freq-parameterized `read_minute_agg`, `MinutePriceIngester` (per-day atomic write, tuned `row_group_size`), `assay ingest-minute`.
Tests: round-trip (`as_of_ts=ts+step`, `session_close_ts`, `session_type`, pre/post excluded count), idempotent re-ingest, row-group pruning ratio.

**M2 — Intraday PIT read path in `DataStore`.**
`_as_time`, `get_panel(freq=...)` minute branch with `effective_end=min(end,as_of)`, day-file enumerator, prior-session dividend lead-in (excluded from panel), **EOD-knowability cut for `adj_events`/`get_universe`**, session-aware `forward_adjust` (vectorized `session_id`→factor broadcast, no per-bar Python list).
Tests: intraday `as_of` exclusion (10:30:30 excludes 10:31); **split with `as_of_date==D` invisible at D 10:00, visible at D 16:00**; `end>as_of` ⇒ identical values to `end==as_of`; daily golden unchanged.

**M3 — Engine intraday semantics (gates minute exposure).**
`to_frame` dtype fix; `time_col`/`session_ids`/`freq` plumbing + intraday-requires-session-vector guard; segmented `windows`/`ts_*` with `segment="session"|"continuous"` auto-select; `ts_ema`/`ts_dema` per-session reset + `ts_ema_daily`; runtime `'Nd'`/`DayWindowNode` lowering; arg-extreme/rank unit relabel; vectorized `cs_rank` (gated to intraday or tolerance-blessed for daily per §10).
Tests: overnight-gap NaN; EMA session-independence; `ts_mean(close,'20d')` **not** all-NaN on a multi-day minute panel; chunked==whole-panel golden; all `ts_*` byte-identical on daily.

**M4 — Streaming windowed kernels + memory-budget subsystem (§8.4).**
Streaming `ts_mean/sum/std/min/max/argmax/argmin/rank/decay_linear/cov/corr` with cross-chunk state checkpointing; session-chunked evaluator (AST max-window, axis-0 chunking, whole-panel fallback); **pre-flight size estimate + `get_panel` budget guard + L2/SessionRegistry LRU**; re-tuned budgets; bench gate.
Tests: streaming==materialized golden (daily within tolerance / intraday); `'20d'` cross-chunk-boundary == unchunked; peak-RAM assertion; budget-guard raises with actionable message.

**M5 — Evaluator at minute horizons (unlocks public minute surface).**
`forward_returns` bar horizons + bar-triple masking + `cross_session` enum + `entry_lag` + real `vwap`; service unit conversion + freq-tagged memo key; minute horizon defaults; lift the `NotImplementedError` surface gate.
Tests: cross-session return masked; `allow_whole_day` by session count (half-day correct); half-life unit conversion; daily evaluator unchanged.

**M6 — Portfolio intraday.**
`periods_per_year` + daily-aggregation default; intraday rebalance types + dispatch + validator + `schema_version`; `execution_offset_bars` precedence; cov/adv windows in bars; intraday execution prices; daily-from-minute output; `n_bars`/`granularity`; vectorized accountant walk.
Tests: Sharpe/vol stable under daily aggregation; `at_open`/`every_n_bars`; `offset>=1`; `config_hash` byte-stable for default daily configs; daily portfolio golden unchanged.

**M7 — Surfaces + caches + library + L2 wiring.**
`frequency` on REST/CLI/MCP/SDK with ISO-datetime parsing + daily-truncation rejection; `SessionCache` time-axis generalization + **borrow engine matrices**; **L2 key carries `(start_ts,end_ts,as_of_ts)` + namespace bump + LRU, then wire into `evaluate()`** with session-close-only `as_of` caching policy; `factor_id`/report `granularity` coexistence.
Tests: surface-contract (frequency omitted ⇒ identical kwargs); daily+minute reports coexist; L2 no daily/minute and no as_of aliasing; report JSON byte-identical for `granularity="1d"`.

---

## 12. Open questions & residual risks

**Top residual risks.** (1) `as_of_ts = bar close` and the EOD-knowability cut are the entire intraday PIT story — a single wrong line silently reintroduces look-ahead; dedicated exclusion tests in M2 + ingest invariant `as_of_ts > ts`. (2) Streaming kernels (M4) must match materialized NaN/`ddof`/tie semantics within the stated tolerance; gated to intraday for daily safety. (3) The runtime `'Nd'` lowering is an *approximation* off nominal bar counts unless the exact per-row session walk is used — half-day/DST error is logged, not hidden.

**Open questions (non-blocking).** Which bar marks session NAV for aggregation (16:00 RTH vs auction proxy)? Whether to expose per-bar (`"bar"`) annualization at all. `adv{d}` at minute scale — `d` sessions of summed bar volume vs a daily-aggregated companion volume field. The M4 bench target number. Extended-hours research mode and its interaction with `bars_per_session`/annualization. Whether daily large-`d` should adopt streaming kernels (tolerance-blessed) or stay materialized.

---

## Resolved review issues

- **Corp-action/universe intraday as_of coercion (critical):** §4.4 — no date-coercion; EOD-knowability cut (`as_of_date < as_of.date()` OR same-day-after-`session_close`) via stored `session_close_ts`; split-invisible-mid-session test.
- **Forward-adjust basis = end > as_of (major):** §4.2 — `effective_end = min(end, as_of)` clamps both the bar filter and adjustment basis; invariant + test.
- **Resample partial-bar completeness (major):** §4.3 — completeness checked against the calendar-expected constituent set for each specific bin; partial frontier bin dropped entirely; coarse `as_of_ts = bin_end`.
- **`forward_returns` masking bypass (major):** §6.1 — masking on the signal/entry/exit bar triple via `session_ids`; explicit `cross_session` enum; whole-day defined by session count.
- **Survivorship/universe intraday (minor):** §4.4 — same EOD-knowability rule; intraday-listing/halt surfaces as NaN; test noted.
- **Carry-over reintroduces leakage (minor):** §5.2 — buffer carries true `session_ids`; segmentation applied after buffer+chunk concat; dividend lead-in excluded from panel; chunked==whole-panel golden.
- **Memory-budget guard is vaporware (critical):** §8.4 — net-new subsystem: pre-flight estimate, enforcement at `get_panel`, byte-accounted LRU in L2 + SessionRegistry, re-tuned budgets, its own milestone (M4).
- **Double/triple in-memory copy (major):** §8.2 — `SessionCache` borrows engine matrices; engine drops `self._panel` after caching; true footprint stated incl. `batch()` concurrency.
- **Float32-in-RAM contradiction (major):** §8.2 — downgraded to a disk-only win; float32 compute path is separate opt-in, not "free/gated."
- **`forward_adjust` per-symbol Python loop at scale (major):** §4.4 — vectorized per-session-factor `session_id`→factor join/broadcast; bisect against unique-session array; no per-bar `.to_list()`/DataFrame rebuild.
- **Resample collects full 1m first (major):** §4.3 — resampling pushed into the LazyFrame, collected at coarse granularity; peak RAM bounded, not the full 1m slice.
- **L2 key omits intraday window/as_of (major):** §8.3 — preimage extended with `(start_ts,end_ts,as_of_ts)` (daily collapses unchanged); L2 wired only after this + LRU.
- **Cache-key cardinality explosion (major):** §8.3 — session-close-only as_of caching (or quantization); SessionRegistry byte cap + LRU; redone arithmetic.
- **Streaming kernels load-bearing yet deferred (major):** §11 — minute exposure gated behind M3+M5; budget guard (M4) raises rather than OOMs; chunked path bounds RAM.
- **Chunked evaluator under-specified (minor):** §8.2 — axis-0 only; cs ops unaffected; AST max-window; whole-panel fallback; cross-chunk state checkpoint.
- **Row-group pruning unverified (minor):** §8.1 — tuned `row_group_size`, empirically verified via bench; 252-file overhead quantified.
- **Window NaN under segmentation = all-NaN multi-day (critical):** §5.2 — `segment="session"|"continuous"`; multi-day windows span sessions via carry-over; `ts_mean(close,'20d')` not all-NaN test.
- **`'Nd'` lowering vs half-days (major):** §5.3 — runtime `DayWindowNode` resolved per row against `session_ids`; nominal used only as a logged approximation; "never shifts" claim dropped.
- **Resample alignment to session open (major):** §4.3 — `group_by_dynamic` anchored within `session_id` (`start_by="datapoint"`); first/last/half-day bins covered by completeness test.
- **Long EMA meaningless intraday (major):** §5.2 — `ts_ema_daily` (day time-constant on session-aggregated series); bar-level `ts_ema` raises when `d >= bars_per_session`.
- **Arg-extreme/rank unit change (major):** §5.3 — documented as bar counts; registry/diagnostics relabeled; `*_days` helper; parity tests.
- **`returns` NaN at session open (minor):** §5.3 — documented; `overnight_return` operator offered.
- **`periods_per_year` hard-codes 252 (minor):** §3.3 — years from actual calendar span; 252 confined to the daily-aggregation convention.
- **`config_hash`/`run_id` stability (critical):** §10 — `to_dict()` untouched; frozen `_HASH_FIELDS_V1` allowlist excludes intraday fields when daily; byte-stability regression test; decided in M0.
- **`compute_factor_id` signature (major):** §10 — `(expr, granularity="1d")` with explicit `"1d"`-preserves-legacy conditional; legacy-digest pin; no daily file re-keying.
- **Daily "byte-identical" vs M4 shared-kernel rewrite (major):** §10 — streaming/`cs_rank` gated to intraday (daily byte-identical) or consciously tolerance-blessed; promise downgraded to "within tolerance" only where a shared daily kernel changes.
- **Cross-version config serialization / enum expansion (major):** §10 — `schema_version` in config + report JSON; old readers fail loudly; intraday enums rejected with actionable error.
- **`as_of` overload daily vs intraday (major):** §9 — freq-aware CLI/REST parsing; service rejects datetime as_of with `freq=1d`; tests both ways.
- **Surface blast radius vs "one kwarg" (major):** §9 — non-pass-through spots enumerated (freq-folded memo key, `_resolve` defaults, per-surface drop-None) with a surface-contract test.
- **Phased incrementality (major):** §11 — "daily-safe always; minute-usable only at marked milestone"; `NotImplementedError` surface gate until M5; engine guard in M3.
- **FactorReport day-unit aliasing (minor):** §10 — minute `n_dates` = distinct-session count; consumers read tagged fields; byte-identical JSON for daily.
- **`execution_offset_days` dual field (minor):** §7.3 — explicit precedence (`bars` wins intraday), daily-neutral default, excluded from hash; cov/adv stay int to keep `from_dict` valid.

## Recommended first milestone

**Build M0 first.** It is the smallest self-contained increment with real value and zero behavior change: the `Frequency` value object, calendar helpers (`session_open_close`/`session_bars`/`bars_per_session`/`session_ids`), config plumbing, and — critically — the two **identity guards** (`config_hash` `_HASH_FIELDS_V1` allowlist and `compute_factor_id(expr, granularity="1d")` with the legacy-digest pin) plus the `schema_version` field. It ships entirely behind defaults, touches no read/eval/portfolio numerics, is fully unit-testable (half-day counts, DST offset, byte-stable `config_hash`/`factor_id` against committed digests), and **de-risks the three CRITICAL backward-compat findings before any minute code exists** — so every later milestone builds on a frozen, verified daily identity contract.