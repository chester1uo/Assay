# Portfolio Backtest

Turn a factor signal into a simulated portfolio with real trading constraints and costs, and
get a `PortfolioReport` (Sharpe, drawdown, turnover, cost drag, NAV series). Design spec:
[portfolio-backtest.md](../design/portfolio-backtest.md).

## Quick start

```python
import assay
from assay.portfolio import PortfolioBacktestConfig

cfg = PortfolioBacktestConfig(
    universe="NASDAQ100", period_start="2025-01-02", period_end="2026-06-09",
    market="US",
    rebalance_type="monthly",        # daily|weekly|monthly|quarterly|threshold|signal
    weight_method="signal_prop",     # equal|signal_prop|quintile|decile|mv|bl|risk_parity
    long_short=False,
    max_single_weight=0.05,
    execution_price="next_open",     # next_open|next_close
)
pf = assay.backtest_portfolio("cs_rank(ts_returns(close, 20))", cfg)

print(pf.total_return, pf.annual_return, pf.sharpe, pf.max_drawdown,
      pf.annual_turnover, pf.cost_drag, pf.n_rebalances)
pf.to_dict()   # JSON-safe, agent-ready
```

CLI:

```bash
python -m assay.cli portfolio 'cs_rank(ts_returns(close, 20))' \
    --start 2025-01-02 --end 2026-06-09 --rebalance monthly --weight-method signal_prop
```

REST: `POST /v1/portfolio/backtest` (see the [REST guide](rest-api.md)).

## Long/short, dollar-neutral

```python
cfg = PortfolioBacktestConfig(
    universe="NASDAQ100", period_start="2025-01-02", period_end="2026-06-09",
    weight_method="quintile", long_short=True, net_exposure=0.0,   # dollar-neutral
    quintile_long_n=1, quintile_short_n=1,
)
pf = assay.backtest_portfolio("cs_rank(ts_corr(close, volume, 20))", cfg)
```

## Weight methods

| Method | Notes |
|---|---|
| `equal` | equal weight across the eligible set |
| `signal_prop` | proportional to the (normalized) signal |
| `quintile` / `decile` | top/bottom buckets long/short |
| `mv` | mean-variance via scipy SLSQP + numpy Ledoit-Wolf shrinkage (no cvxpy/sklearn) |
| `risk_parity` | inverse-vol / equal-risk-contribution (long-only) |

## Key config groups

`rebalance_*` (schedule + `min_rebalance_interval`, `execution_offset_days`), weights
(`gross_exposure`, `net_exposure`, `signal_transform`), constraints (`max_single_weight`,
`max/min_stock_count`, `max_turnover_per_period`), execution (`slippage_model`, `slippage_k`,
`max_adv_fraction`, `partial_fill_handling`), benchmark (`benchmark`, `risk_free_rate`), output
(`save_trade_log`, `save_position_log`). `PortfolioBacktestConfig.preset('US'|'A'|'HK')` applies
market cost/limit defaults. Full field list: the [design doc](../design/portfolio-backtest.md) §2.

## Metrics in the report

Returns (total/annual/gross/excess), risk-adjusted (Sharpe, Sortino, Calmar, information ratio,
max drawdown + recovery, beta, CAPM alpha, tracking error), turnover & cost (annual turnover,
cost drag, avg holding days), plus `nav_series`/`nav_dates`, `monthly_returns`, `trade_log`,
`position_log`, and `a_share_metrics` (only when `market='A'`).

## A-share

Set `market="A"` (or `PortfolioBacktestConfig.preset("A")`) to activate T+1 settlement, price-limit
blocking, and the stamp-duty/commission/transfer-fee cost model. These run on any market's data
**when their inputs are supplied** (`prev_close` for price limits, a `tradable_mask` for
ST/suspension, etc.). On US data the data-dependent filters are inert (no crash). A true index
benchmark needs an index price series Assay doesn't have — the benchmark is an equal-weight-universe
proxy.

## Caveats on the current dataset

- **Unadjusted splits.** If the data folder has no `adj_events`, a few real splits show up as
  ~−90% single-day returns that inflate results. Backfill corporate actions
  (`assay.cli corp-actions`) to get clean numbers — see the [data pipeline guide](data-pipeline.md).
- **`rebalance_type='signal'`** with a high-autocorrelation factor may never fire (spec-compliant),
  leaving a flat all-cash NAV — choose a calendar schedule for a non-degenerate run.
