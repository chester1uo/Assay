# REST API

FastAPI app exposing `AssayService` over HTTP, with SSE streaming for evaluation. Run it:

```bash
python -m assay.cli serve-api --port 8000      # or: uvicorn assay.api.app:app --port 8000
```

- Base: `http://localhost:8000` · all data routes under `/v1`
- Interactive docs (Swagger): `http://localhost:8000/docs`
- The WebUI is served at `/` (see the [WebUI guide](webui.md)).

## Auth

Optional API-key auth. Set `ASSAY_API_KEYS` (comma-separated) to require an `X-API-Key` header;
**unset = open** (works out of the box). Same-origin WebUI needs no key.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET`  | `/health` | Liveness |
| `POST` | `/v1/factor/evaluate` | Evaluate one factor — JSON report, or SSE stream when `stream:true` |
| `POST` | `/v1/factor/batch` | Batch evaluate → `{total, elapsed_ms, reports}` |
| `POST` | `/v1/factor/lint` | Parse-only: `{dialect, canonical, fields, operators, ast, diagnostics}` (no data) |
| `GET`  | `/v1/library/factors` | List with filters (`min_rank_icir`, `source`, `sort_by`, `limit`, ...) |
| `GET`  | `/v1/library/factors/{id}` | Full `FactorReport` |
| `POST` | `/v1/library/factors` | Save a report |
| `DELETE` | `/v1/library/factors` | Delete by `{factor_ids: [...]}` |
| `GET`  | `/v1/library/correlation-matrix` | Pairwise correlation for `factor_ids` |
| `GET`  | `/v1/library/ic-heatmap` · `/embedding` · `/lineage` | RankIC-over-time, 2-D similarity map, derivation DAG |
| `POST` | `/v1/library/factors/bulk` | Bulk-evaluate + save expressions |
| `POST` | `/v1/library/prune` | Identify/remove redundant factors |
| `GET`  | `/v1/combination/methods` | List combination schemes (learned models flagged available) |
| `POST` | `/v1/combination` | Combine factors, score train/val/test → composite + model |
| `POST/GET/DELETE` | `/v1/combination/saved` | Save / list / delete saved combination runs |
| `GET`  | `/v1/combination/saved/{id}` | Reload one saved run (the fitted model) |
| `GET`  | `/v1/market/bars` | OHLCV bars for one symbol (`freq`, `adj`) |
| `POST` | `/v1/market/factor-series` | One factor's value for one symbol over time |
| `POST` | `/v1/session/create` · `DELETE /v1/session/{id}` | Create / release a panel-cached session |
| `POST` | `/v1/portfolio/backtest` | Run a portfolio backtest → `PortfolioReport` |
| `GET`  | `/v1/system/status` · `/universes` · `/data-calendar` | Engine/data/cache status, universes, coverage |
| — | **Data manager** (`/v1/admin/*`, operator) | see below |

### Data-manager (admin) endpoints

Operator surface behind the WebUI *Data* tab (hidden from the OpenAPI schema, but live):

| Method | Path | Purpose |
|---|---|---|
| `GET/PUT` | `/v1/admin/config` | Read (masked) / update dirs, credentials, system settings |
| `GET`  | `/v1/admin/data/status` · `/usage` | RAW↔ASSAY sync snapshot · per-market disk sizes (RAW + ASSAY) |
| `POST` | `/v1/admin/data/test` | Test a provider connection (`{provider: massive\|tushare}`) |
| `POST` | `/v1/admin/data/jobs` | Queue a job (`mode: init\|update\|ingest`); `GET` list; `GET /{id}` one |
| `GET/PUT` | `/v1/admin/schedule` | Auto-update schedule (per market: enabled + daily time) |
| `GET`  | `/v1/admin/cache/status` · `/entries` · `POST /rebuild` | Hot-cache status / contents / rebuild |

## Evaluate — blocking

```bash
curl -s -X POST http://localhost:8000/v1/factor/evaluate \
  -H 'Content-Type: application/json' \
  -d '{"expr":"ts_corr(close, volume, 20)","universe":"NASDAQ100",
       "period":["2025-01-02","2026-06-09"],"horizons":[1,5,10,20]}'
# -> { ...FactorReport }
```

## Evaluate — SSE stream

Set `"stream": true`; the response is `text/event-stream` with frames:

```
event: eval.started   data: {"factor_id": "...", "expr": "..."}
event: eval.ic_series data: {"ic": [...], "rank_ic": [...], "dates": [...]}
event: eval.decay     data: {"ic_by_horizon": {...}, "halflife": 12}
event: eval.groups    data: {"quintile_returns": [...]}
event: eval.complete  data: { ...full FactorReport }
```

All data is NaN-safe JSON (NaN/Inf → null), so `JSON.parse` never fails. `EventSource` can't
POST — consume the stream with `fetch` + a `ReadableStream` reader (the WebUI does this).

## Portfolio backtest

```bash
curl -s -X POST http://localhost:8000/v1/portfolio/backtest \
  -H 'Content-Type: application/json' \
  -d '{"expr":"cs_rank(ts_returns(close, 20))",
       "config":{"universe":"NASDAQ100","period_start":"2025-01-02","period_end":"2026-06-09",
                 "rebalance_type":"monthly","weight_method":"signal_prop"}}'
```

## Error schema

Errors mirror the engine diagnostics (`assay.engine.diagnostics`): stable `code`
(`ASSAY-P###/E###/O###`), `failure_mode` (`SYNTAX_ERROR · LOOKAHEAD · CONSTANT · ALL_NAN ·
RUNTIME_ERROR`), `severity`, `stage`, `message`, `location`, `suggestion`. A request that needs
ingested data returns **503** when the store is empty.

Full contract: [architecture.md](../design/architecture.md) §4.
