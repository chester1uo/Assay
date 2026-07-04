# Getting Started

Assay is a point-in-time-correct, agent-native factor backtesting engine. This guide takes
you from a clean checkout to your first scored factor and a portfolio backtest.

## 1. Install

Src-layout package; install from source (no PyPI release yet):

```bash
git clone <repo> && cd Assay
pip install -e .            # core deps
# optional extras (all already vendored in this environment):
pip install -e ".[perf,api,mcp]"
```

Everything runs with `PYTHONPATH=src` if you haven't `pip install -e .`'d.

## 2. Configure the data source & output dir

Configuration is environment-variable based, loaded by `AssayConfig.from_env()`. A project
`.env` is read at import (shell env always wins). The pipeline reads a **local** MASSIVE
mirror — no credentials needed. See [.env.example](../../../.env.example):

```bash
MASSIVE_DATA_DIR=/data/massive_data    # root of the local MASSIVE mirror (source)
ASSAY_DATA_DIR=data                    # parquet store root (point at any folder)
```

> US equities (MASSIVE — NASDAQ-100 / S&P 500) and China A-shares (Tushare — CSI300/500/1000)
> are both wired up. The bundled US data is **OHLCV + transaction count — no `vwap`**.

## 3. Transfer data

Transform the local mirror into a point-in-time NASDAQ-100 dataset (universe + corporate
actions + prices) under the folder `ASSAY_DATA_DIR` points at. See the
[data pipeline guide](data-pipeline.md) for detail.

```bash
python -m assay.cli prepare-nasdaq100 --start 2025-01-01 --end 2026-06-09
python -m assay.cli status                      # what's ingested
```

## 4. Evaluate a factor

**CLI** — a quick AST + over-the-panel check, then a full scored report:

```bash
python -m assay.cli parse 'cs_rank(ts_corr(close, volume, 20))'      # no data needed
python -m assay.cli run   'cs_rank(ts_corr(close, volume, 20))' \
    --start 2025-01-02 --end 2026-06-09
```

**Python SDK** — see the [SDK guide](python-sdk.md):

```python
import assay
report = assay.backtest(
    "cs_rank(ts_corr(close, volume, 20))",
    universe="NASDAQ100", period=("2025-01-02", "2026-06-09"),
)
print(report.rank_ic, report.rank_icir, report.decay_halflife_days, report.failure_mode)
```

A `FactorReport` carries `ic`/`rank_ic`/`icir`/`rank_icir`, `ic_by_horizon`, decay, turnover,
redundancy, look-ahead detection, a natural-language `suggestion`, and structured
`diagnostics`. `.to_dict()` is JSON-safe for agent consumption.

## 5. Run a portfolio backtest

```python
from assay.portfolio import PortfolioBacktestConfig
cfg = PortfolioBacktestConfig(universe="NASDAQ100",
                              period_start="2025-01-02", period_end="2026-06-09",
                              rebalance_type="monthly", weight_method="signal_prop")
pf = assay.backtest_portfolio("cs_rank(ts_returns(close, 20))", cfg)
print(pf.sharpe, pf.max_drawdown, pf.annual_turnover, pf.cost_drag)
```

See the [portfolio backtest guide](portfolio-backtest.md).

## 6. Start the surfaces

```bash
python -m assay.cli serve-api --port 8000     # REST API + WebUI at http://localhost:8000
python -m assay.cli serve-mcp                 # MCP server (stdio) for LLM agents
```

- **WebUI** — open `http://localhost:8000` (see the [WebUI guide](webui.md)).
- **REST API** — interactive docs at `/docs`; see the [REST guide](rest-api.md).
- **MCP** — 11 agent tools; see the [MCP guide](mcp-server.md).

## Where to go next

| Guide | What it covers |
|---|---|
| [Data pipeline](data-pipeline.md) | MASSIVE loaders, `prepare-nasdaq100`, data folders, caveats |
| [Python SDK](python-sdk.md) | `backtest`, `Session`, `batch_backtest`, `library`, `stream`, `backtest_portfolio` |
| [CLI](cli.md) | every `python -m assay.cli` subcommand |
| [REST API](rest-api.md) | `/v1/*` endpoints, SSE streaming, auth |
| [MCP server](mcp-server.md) | agent tools and client setup |
| [WebUI](webui.md) | the zero-install web app |
| [Portfolio backtest](portfolio-backtest.md) | config, running, metrics, A-share notes |
| [Performance](performance.md) | the Alpha-101 cache vs no-cache benchmark |
