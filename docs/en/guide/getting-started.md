# Getting Started

Assay scores factor expressions on point-in-time data: IC, RankIC, decay, turnover,
redundancy, and a portfolio backtest with costs. There are two ways in: the hosted site,
or your own install.

## Use the hosted site

[assay.qfinverse.com](https://assay.qfinverse.com) runs Assay on QFinVerse's US daily data
(2003 onward, universes TOP500 / TOP1000 / TOP3000, SP500, NASDAQ100). See
[US data from QFinVerse](qfv-data.md) for what that data holds.

1. **Get an invitation.** Sign-up is by invitation only. Ask an admin for a link, open it,
   and choose a username and password (at least 10 characters).
2. **Sign in** at `/login`. The session lasts 7 days.
3. **Score a factor.** Open *Single Factor Test*, pick a universe and period in the bar at
   the top, write an expression such as `-ts_delta(close, 5) / ts_delay(close, 5)` and press
   *Evaluate*. Save good ones to the library. See the [WebUI guide](webui.md).
4. **Create an API key** for scripts and agents: *Account* → *API keys* → *Create key*.
   The key (`asy_...`) is shown once. It acts as you; revoke it when you no longer need it.
5. **Call the REST API** with the key in the `X-API-Key` header:

```bash
export ASSAY_KEY=asy_...
curl -s -X POST https://assay.qfinverse.com/v1/factor/evaluate \
  -H "X-API-Key: $ASSAY_KEY" -H 'Content-Type: application/json' \
  -d '{"expr": "-ts_delta(close, 5) / ts_delay(close, 5)", "universe": "SP500",
       "period": ["2019-01-01", "2026-10-30"], "horizons": [1, 5, 10, 20]}'
```

All endpoints are listed in the [REST guide](rest-api.md); the interactive schema is at
`/docs` on the site.

## Run it yourself

### 1. Install

From source (there is no PyPI release):

```bash
git clone https://github.com/QFinVerse/Assay.git && cd Assay
pip install -e ".[perf,api,mcp]"      # perf = numba + scipy (much faster evaluation)
```

Without installing, everything also runs with `PYTHONPATH=src`.

### 2. Point it at data

Settings are environment variables, read by `AssayConfig.from_env()`. A `.env` file in the
project is loaded at import; the shell always wins. See
[.env.example](../../../.env.example).

- **US, from a QFinVerse daily panel** (recommended): set `ASSAY_QFV_PANEL_DIR` to the panel
  root. Nothing else to ingest; see [US data from QFinVerse](qfv-data.md).
- **US, from a local MASSIVE mirror**: set `MASSIVE_DATA_DIR` and build the Parquet stores:

```bash
export MASSIVE_DATA_DIR=/data/massive_data   # local mirror, no credentials needed
export ASSAY_DATA_DIR=data                   # where the stores are written
python -m assay.cli prepare-nasdaq100 --start 2025-01-01 --end 2026-06-09
python -m assay.cli status
```

- **China A-shares and Hong Kong** come from Tushare; see [Multi-market](../multi_market.md)
  and [Tushare](../tushare.md).

The MASSIVE day bars are OHLCV plus trade count; there is no `vwap` field.

### 3. Evaluate a factor

From the CLI:

```bash
python -m assay.cli parse 'cs_rank(ts_corr(close, volume, 20))'      # syntax only, no data
python -m assay.cli run   'cs_rank(ts_corr(close, volume, 20))' \
    --start 2025-01-02 --end 2026-06-09
```

From Python ([SDK guide](python-sdk.md)):

```python
import assay
report = assay.backtest(
    "cs_rank(ts_corr(close, volume, 20))",
    universe="NASDAQ100", period=("2025-01-02", "2026-06-09"),
)
print(report.rank_ic, report.rank_icir, report.decay_halflife_days, report.failure_mode)
```

A `FactorReport` holds `ic` / `rank_ic` / `icir` / `rank_icir`, `ic_by_horizon`, decay,
turnover, redundancy, look-ahead checks, a `suggestion` and structured `diagnostics`.
`.to_dict()` is JSON-safe.

### 4. Backtest a portfolio

```python
from assay.portfolio import PortfolioBacktestConfig
cfg = PortfolioBacktestConfig(universe="NASDAQ100",
                              period_start="2025-01-02", period_end="2026-06-09",
                              rebalance_type="monthly", weight_method="signal_prop")
pf = assay.backtest_portfolio("cs_rank(ts_returns(close, 20))", cfg)
print(pf.sharpe, pf.max_drawdown, pf.annual_turnover, pf.cost_drag)
```

See the [portfolio backtest guide](portfolio-backtest.md).

### 5. Start the server

```bash
python -m assay.cli serve-api --port 8000     # REST API, WebUI and Doc Hub on :8000
python -m assay.cli serve-mcp                 # MCP server over stdio, for agents
```

The server is open by default. To require sign-in, set `ASSAY_ACCOUNTS=1` and create the
first admin with the token in the state directory; see [REST API → Auth](rest-api.md).

## Where to go next

| Guide | What it covers |
|---|---|
| [WebUI](webui.md) | the console: pages, controls, accounts |
| [US data from QFinVerse](qfv-data.md) | universes, security ids, splits and dividends |
| [Data & Adjustment](data-and-adjustment.md) | how prices are adjusted and read point-in-time |
| [Data pipeline](data-pipeline.md) | MASSIVE loaders, `prepare-nasdaq100`, data folders |
| [Python SDK](python-sdk.md) | `backtest`, `Session`, `batch_backtest`, `library`, `backtest_portfolio` |
| [CLI](cli.md) | every `python -m assay.cli` subcommand |
| [REST API](rest-api.md) | `/v1/*` and `/auth/*` endpoints, SSE streaming, auth modes |
| [MCP server](mcp-server.md) | agent tools and client setup |
| [Portfolio backtest](portfolio-backtest.md) | config, running, metrics, A-share notes |
| [Performance](performance.md) | the Alpha-101 cache benchmark |
