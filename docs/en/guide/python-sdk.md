# Python SDK

The SDK (`import assay`) is the lowest-latency surface — in-process, no serialization. It
auto-initializes from the environment on first use.

```python
import assay
assay.init()          # optional; reads AssayConfig.from_env(); backtest() auto-inits otherwise
```

Public surface (`assay.__all__`): `init`, `backtest`, `batch_backtest`, `backtest_portfolio`,
`stream`, `Session`, `library`, plus `AssayConfig`, `MassiveConfig`, `AssayService`.

## Single-factor evaluation

```python
report = assay.backtest(
    "ts_corr(close, volume, 20)",
    universe   = "NASDAQ100",
    period     = ("2025-01-02", "2026-06-09"),
    horizons   = [1, 5, 10, 20],
    execution  = "next_open",      # next_open | next_close  (no vwap)
    neutralize = None,             # ["sector"] needs group data (absent for US)
    as_of      = "2026-06-09",     # PIT: only data known by this date
    adj        = "split",          # none | split | total
)

report.rank_ic, report.rank_icir          # headline IC
report.ic_by_horizon                       # {1: .., 5: .., ...}
report.decay_halflife_days
report.turnover_1d, report.redundancy_score
report.lookahead_detected, report.failure_mode, report.suggestion
report.diagnostics                         # structured ASSAY-* diagnostics on failure/warning
report.to_dict(); report.to_json(); report.to_dataframe()   # JSON-safe / IC time series
```

Expressions accept **both syntaxes** (they lower to the same AST):

```python
assay.backtest("Corr($close, $volume, 20)")        # qlib
assay.backtest("ts_corr(close, volume, 20)")       # function-call / Alpha-101
```

## Batch evaluation

```python
factors = ["ts_returns(close, 20)", "ts_corr(close, volume, 20)",
           "cs_rank(ts_std(close, 20))"]
reports = assay.batch_backtest(factors, universe="NASDAQ100",
                               period=("2025-01-02", "2026-06-09"), sort_by="rank_icir")
for r in reports[:5]:
    print(f"{r.expr:<40} ICIR={r.rank_icir:.2f}")
```

## Sessions (amortize panel load)

A session loads the price panel and forward returns once; subsequent factors reuse them.

```python
with assay.Session(universe="NASDAQ100", period=("2025-01-02", "2026-06-09")) as sess:
    r1 = sess.backtest("ts_returns(close, 20)")        # loads the panel
    r2 = sess.backtest("ts_corr(close, volume, 20)")   # reuses it (much faster)
    reports = sess.batch_backtest(factors)
```

## Streaming (async)

```python
import asyncio
async def watch():
    async for ev in assay.stream("ts_corr(close, volume, 20)", universe="NASDAQ100"):
        if ev["event"] == "eval.complete":
            print(ev["data"]["rank_icir"])
asyncio.run(watch())
```

Events arrive in order: `eval.started`, `eval.ic_series`, `eval.decay`, `eval.groups`,
`eval.complete` (with the full report). All frames are NaN-safe JSON.

## Factor library

```python
assay.library.save(report)
factors = assay.library.list(min_rank_icir=0.5, sort_by="rank_icir", limit=20)
r       = assay.library.get(factor_id)
corr    = assay.library.correlation_matrix([f.factor_id for f in factors])
assay.library.prune(threshold=0.7, dry_run=True)
assay.library.delete([factor_id])
```

## Portfolio backtest

```python
from assay.portfolio import PortfolioBacktestConfig
cfg = PortfolioBacktestConfig(universe="NASDAQ100",
                              period_start="2025-01-02", period_end="2026-06-09",
                              market="US", rebalance_type="monthly",
                              weight_method="signal_prop", long_short=False)
pf = assay.backtest_portfolio("cs_rank(ts_returns(close, 20))", cfg)
print(pf.sharpe, pf.max_drawdown, pf.annual_turnover, pf.cost_drag, pf.n_rebalances)
```

`backtest_portfolio(expr, config=None, **config_kwargs)` — pass a `PortfolioBacktestConfig` or
keyword fields. See the [portfolio guide](portfolio-backtest.md).

## Custom config

```python
from assay.config import AssayConfig
svc = assay.init(AssayConfig.from_env())          # or AssayConfig.for_tests(tmp_dir) offline
```
