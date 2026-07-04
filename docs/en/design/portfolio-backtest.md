# Assay — Portfolio Backtest Design

**Version:** 0.1 · Draft → **Implemented** (market-agnostic core)
**Scope:** Factor-driven portfolio simulation with full A-share constraint support
**Depends on:** Assay FactorEngine, DataStore PIT layer, evaluator

> **Implementation status (2026-06): ✅ implemented** at [`src/assay/portfolio/`](../../../src/assay/portfolio/)
> (config, report, signal, rebalance, weights, constraints, costs, execution, accounting,
> metrics, backtester) and wired into `AssayService.backtest_portfolio`, the SDK
> (`assay.backtest_portfolio`), REST `POST /v1/portfolio/backtest`, and CLI `assay portfolio`.
> 745+ tests green; a real NASDAQ-100 backtest runs end-to-end (see [the usage guide](../guide/portfolio-backtest.md)).
>
> **Grounded to reality.** The market-agnostic core runs today on the only data we have —
> **US equities (NASDAQ-100, OHLCV)**. The A-share *price-mechanical* rules (T+1 settlement,
> price-limit blocking, the stamp-duty/commission/transfer-fee cost model) are **config that
> activates when its inputs are supplied**. The A-share *data-dependent* filters (ST filter,
> suspension handling, index-reconstitution avoidance, northbound flow, sector-neutral) are
> scaffolded as optional inputs that **no-op gracefully** because that data is not in the
> store. The benchmark is an **equal-weight-universe proxy** (Assay has no index price series).
> Status legend: ✅ implemented · 🔶 active only with the right inputs · 📋 needs data we lack.

---

## 1. Overview

Portfolio backtesting answers a question IC analysis cannot: *given a factor signal, real
trading constraints, and market costs, what net return is achievable?* The module consumes a
factor **expression** (re-evaluated to a `(T, N)` matrix) and simulates position-taking,
rebalancing, cost-charging, and mark-to-market over the evaluation period, emitting a
structured `PortfolioReport`.

### 1.1 Pipeline

```
factor expression
    │  evaluate -> (T, N) factor matrix + close/open prices
    ▼
UniverseFilter        eligible = finite factor & finite price (& tradable mask)   ✅
    ▼
SignalProcessor       winsorize · neutralize · normalize (rank/zscore/raw)         ✅
    ▼
RebalanceScheduler    calendar · threshold · signal-autocorr                       ✅
    ▼
WeightConstructor     equal · signal_prop · quantile · mean-variance · risk-parity ✅
    ▼
ConstraintApplicator  position/count caps · turnover cap · gross/net · sector-neut ✅ / 🔶
    ▼
ExecutionSimulator    slippage · ADV cap · price limits · T+1 settlement           ✅ / 🔶
    ▼
PortfolioAccountant   weight drift · daily MTM NAV · costs · trade/position log    ✅
    ▼
PortfolioReport       Sharpe · drawdown · turnover · cost drag · benchmark · A-share ✅
```

The optimization rebalancer uses **scipy** (`scipy.optimize.minimize`, SLSQP) and a numpy
Ledoit-Wolf shrinkage — `cvxpy`/`scikit-learn` are intentionally *not* dependencies.

---

## 2. Configuration

All parameters live on one `PortfolioBacktestConfig` (72 fields). `PortfolioBacktestConfig.preset('US'|'A'|'HK')`
applies the market table (§6) cost/limit defaults. Fields marked 🔶 are A-share price-mechanics
(active with the right inputs); 📋 need A-share data absent from the store.

**Evaluation period** — `period_start`, `period_end`, `oos_split_date`, `warmup_days`

**Universe & market** — `universe` (default `NASDAQ100`), `market` (default `US`),
`custom_symbols`, `as_of_date`, `include_delisted`

**Rebalance** — `rebalance_type` (daily/weekly/monthly/quarterly/threshold/signal),
`rebalance_day`, `threshold_rank_shift`, `threshold_weight_drift`, `signal_autocorr_floor`,
`min_rebalance_interval`, `execution_offset_days` (≥1; 0 is look-ahead bias)

**Weight construction** — `weight_method` (equal/signal_prop/mv/risk_parity/quintile/decile/bl),
`long_short`, `gross_exposure`, `net_exposure`, `signal_transform`, `quintile_long_n`,
`quintile_short_n`, `mv_risk_aversion`, `cov_window`, `cov_method`, `bl_tau`

**Constraints** — `max_single_weight`, `min_single_weight`, `max_sector_weight` 📋,
`sector_neutral` 📋, `market_neutral`, `max_turnover_per_period`, `max_annual_turnover`,
`min_stock_count`, `max_stock_count`, `benchmark_tracking_err`, `capacity_adv_limit`

**A-share (§2.6)** — price-mechanical 🔶: `t_plus_1`, `price_limit_pct`, `star_chinext_limit`,
`enforce_limit_price`, `stamp_duty_rate`, `commission_rate`, `commission_min`,
`transfer_fee_rate`; data-dependent 📋: `st_filter`, `new_listing_lockout_days`/`ipo_lockout_days`,
`suspend_handling`, `rebalance_around_index`, `inclusion_anticipation`, `northbound_flow_filter`,
`sz_sh_connect_only`

**Execution** — `execution_price` (next_open/next_close/vwap/arrival), `slippage_model`
(sqrt/linear/zero/almgren_chriss), `slippage_k`, `adv_window`, `max_adv_fraction`,
`partial_fill_handling` (defer/cancel/force), `include_bid_ask`

**Benchmark & attribution** — `benchmark` (index→equal-weight proxy/cash/custom/none),
`benchmark_symbol`, `risk_free_rate`, `attribution_model`, `attribution_factors`

**Output** — `output_frequency`, `save_trade_log`, `save_position_log`, `compute_attribution`,
`bootstrap_sharpe`, `n_bootstrap`

---

## 3. Rebalance algorithms

| Type | When to use | Typical annual turnover |
|---|---|---|
| `daily` | fast-decay factors (half-life < 5d) | 400–1200% |
| `weekly` | half-life 5–20d (momentum / reversal) | 100–400% |
| `monthly` | half-life 20–60d (most fundamental/quality) | 50–150% |
| `quarterly` | half-life > 60d (value, balance sheet) | 20–60% |
| `threshold` | stable factor; rebalance on rank-shift or weight-drift | varies |
| `signal` | rebalance when factor rank-autocorr drops below a floor | varies |

`min_rebalance_interval` is enforced for every type. *Size the rebalance frequency to the
factor's half-life, not to the maximum technically possible.*

---

## 4. Performance metrics

**Returns** — total, annualized (×252/T), excess (vs benchmark), gross (pre-cost), net.
**Risk-adjusted** — Sharpe, Sortino, Calmar, information ratio, max drawdown (+start/end/recovery),
beta, CAPM alpha, tracking error.
**Turnover & cost** — one-way / annual turnover, cost drag (gross − net), implied avg holding days.
**A-share (`market='A'`)** — limit-hit rate, forced-hold ratio (T+1), suspension impact,
ST-exposure days, index-recon alpha, northbound-flow correlation. *(Populated only where the
underlying inputs exist; on US data only limit-hit/forced-hold are meaningful and are inert.)*

All annualization uses 252 trading days.

---

## 5. PortfolioReport schema

Mirrors `FactorReport`: machine-readable JSON for agent consumption. Identity (`run_id`
= sha256[:12] of factor_id + config hash, `factor_id`, `config`); period (`period_start/end`,
`n_trading_days`, `n_rebalances`); returns (`total/annual/gross/excess_return`); risk
(`sharpe`, `sortino`, `calmar`, `information_ratio`, `max_drawdown` + `_start`/`_end`/
`drawdown_recovery_days`, `beta`, `alpha_capm`, `tracking_error`); cost (`annual_turnover`,
`cost_drag`, `avg_holding_days`); detail (`nav_series`, `nav_dates`, `benchmark_series`,
`monthly_returns`, `trade_log`, `position_log`, `attribution`, `a_share_metrics`); provenance
(`lineage.{data_snapshot, eval_timestamp, adj_version}`). `to_dict()`/`to_json()` are JSON-safe
(NaN→None); `from_dict()` round-trips.

---

## 6. Market comparison

| Parameter | A-share (SSE/SZSE) | US (NYSE/NASDAQ) | HK (HKEX) |
|---|---|---|---|
| Settlement | T+1 | T+2 | T+2 |
| Price limits | ±10% (main), ±20% (STAR) | none | none |
| Stamp duty | 0.10% sell | 0% | 0.13% both |
| Commission | 0.03% both, min ¥5 | 0.01–0.05% | 0.03–0.05% |
| Transfer fee | 0.002% (SSE only) | 0% | 0.003% |
| Index rebalance | semiannual (Jun/Dec) | quarterly | quarterly |
| Sector classification | CSRC | GICS | GICS / Hang Seng |
| Market impact `k` | 0.20 | 0.10 | 0.15 |

Only the **US** column is exercised by the current data. A/HK presets are fully specified and
activate when the corresponding market data is ingested.

---

## 7. Correctness notes

- **Survivorship bias** — the download universe is the PIT union over the range
  (de-listed/removed names included); never use today's composition for a historical backtest.
- **Corporate-action alignment** — the `adj_factor` is versioned and queried as-of the backtest
  date. (The current dataset has no `adj_events`, so a few real splits survive unadjusted — see
  the [usage guide](../guide/portfolio-backtest.md) caveats; backfill corp-actions to fix.)
- **Cost = rate × notional** — the cost model returns a *rate*; the executor multiplies by the
  traded weight fraction. (A regression of this exact bug was caught and fixed.)

---

*The original Word source is kept alongside this file as `portfolio-backtest.docx`.*
*— Assay · Portfolio Backtest Design · AlphaBench Project —*
