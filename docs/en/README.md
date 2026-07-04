# Assay Documentation

Assay is a point-in-time-correct, agent-native factor backtesting engine for LLM-driven alpha
mining. This folder holds the **design specs** and the **usage guides**.

New here? Start with **[Getting Started](guide/getting-started.md)**.

## Usage guides — `guide/`

| Guide | What it covers |
|---|---|
| [Getting Started](guide/getting-started.md) | Install → configure → ingest → first factor & portfolio backtest |
| [Data Pipeline](guide/data-pipeline.md) | MASSIVE loaders, `prepare-nasdaq100`, the parquet stores, data-folder & adjustment caveats |
| [Python SDK](guide/python-sdk.md) | `backtest`, `Session`, `batch_backtest`, `library`, `stream`, `backtest_portfolio` |
| [CLI](guide/cli.md) | Every `python -m assay.cli` subcommand |
| [REST API](guide/rest-api.md) | `/v1/*` endpoints, SSE streaming, lint, auth |
| [MCP Server](guide/mcp-server.md) | The 11 agent tools and client setup |
| [WebUI](guide/webui.md) | The zero-install web app served by FastAPI |
| [Factor Combination](guide/factor-combination.md) | Blend factors into a composite; train/val/test; analytic, optimization & ML methods |
| [Portfolio Backtest](guide/portfolio-backtest.md) | Config, running, metrics, A-share notes |
| [Performance](guide/performance.md) | The Alpha-101 cache-vs-no-cache benchmark |
| [Precompute & CSE](guide/precompute-cse.md) | Mine common sub-expressions, precompute for all assets, accelerate sweeps |

## Design specs — `design/`

| Spec | Scope |
|---|---|
| [Engineering](design/engineering.md) | System architecture, data layer, engine, cache, evaluation, performance model |
| [Full-Stack Architecture](design/architecture.md) | The four surfaces (SDK · REST · WebUI · MCP) over one shared engine |
| [WebUI Design](design/webui.md) | Screen-level design for the (target React) web app |
| [Portfolio Backtest](design/portfolio-backtest.md) | Factor-driven portfolio simulation with A-share constraints |
| [Operator Compatibility](design/operator-compatibility.md) | qlib ↔ Assay ↔ Alpha-101 operator mapping |

## Reports — `reports/`

- [Alpha-101 Test Report](reports/alpha101.md) — fidelity of the 101 Formulaic Alphas catalog.

---

### Status convention

The design docs are **forward-looking specs kept grounded to reality** with status badges:
✅ implemented · 🔶 implemented-but-simplified / activates only with the right inputs · 📋 planned.
What's built today: the data layer, factor engine, IC/decay evaluator, factor library,
`AssayService` + Python SDK, REST API (+ SSE), MCP server, the zero-install WebUI, and the
portfolio backtest. Universes wired up: **NASDAQ-100 / S&P 500** (US, OHLCV — no `vwap`) and
**CSI300 / CSI500 / CSI1000** (China A-shares, via Tushare).
