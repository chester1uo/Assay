# Assay — Full-Stack Architecture Design

**Version:** 0.1 · Draft  
**Scope:** Functional API · REST API · WebUI · MCP for Agents

> **Implementation status (2026-06).** The **entire Python backend is now implemented and
> tested** (662 offline tests green): data layer, factor engine, the IC/RankIC/decay/groups/
> turnover **evaluator**, the **factor library** (persistence + correlation + redundancy +
> prune), the **`AssayService`** singleton (evaluate / batch / sessions / streaming), the
> **Python SDK** (`assay.backtest/batch_backtest/Session/library/stream`), the **REST API**
> (FastAPI + SSE), the **MCP server** (8 FastMCP tools, stdio + SSE/HTTP), and the **CLI**
> (`run`/`batch`/`report`/`library`/`serve-api`/`serve-mcp`). See [`src/assay/`](../../src/assay/).
>
> Still **planned**: the React **WebUI** (§5, no `assay-ui/` yet); and two performance
> optimizations the backend works correctly without — the **L1 Arena / incremental O(1)
> cache** (only a session cache + L2 disk cache exist today) and **DAG/CSE batch merging**
> (`batch()` evaluates factors independently for now). Sections are tagged accordingly.
>
> Status legend — ✅ **Implemented** · 🔶 **Implemented, simplified vs spec** · 📋 **Planned**
>
> The single universe wired up end-to-end today is **NASDAQ-100** (PIT constituent
> history from MASSIVE); SP500 / Russell 2000 are roadmap. The data source is **MASSIVE**
> US-equity day aggregates, which provide OHLCV + transaction count but **no `vwap`**.

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [Core Engine (Shared Backend)](#2-core-engine-shared-backend)
3. [Functional Python API](#3-functional-python-api)
4. [REST API](#4-rest-api)
5. [WebUI Frontend](#5-webui-frontend)
6. [MCP Server for Agents](#6-mcp-server-for-agents)
7. [Cross-Cutting Concerns](#7-cross-cutting-concerns)
8. [Deployment](#8-deployment)

---

## 1. Architecture Overview

### 1.1  Design philosophy

Assay exposes four consumption surfaces over a single shared engine. Every surface calls the same underlying computation, cache, and data layer — there is no "API version" of a result vs a "SDK version". This means:

- A factor evaluated via Python SDK, REST API, WebUI, or MCP agent call produces an identical `FactorReport`
- Cache is shared across all surfaces — a REST call that warms the L1 operator cache benefits a subsequent SDK call
- Auth, rate limiting, and lineage tracking apply uniformly regardless of entry point

### 1.2  Layer diagram

Boxes are tagged with their build status: ✅ implemented, 🔶 partial, 📋 planned.

```
┌────────────────────────────────────────────────────────────────────┐
│  Consumption surfaces                                              │
│                                                                    │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────┐  ┌──────────┐  │
│  │  Python SDK  │  │   REST API   │  │  WebUI   │  │   MCP    │  │
│  │ ✅ assay.*   │  │ ✅ (FastAPI) │  │🔶 vanilla│  │✅(FastMCP)│ │
│  └──────┬───────┘  └──────┬───────┘  └────┬─────┘  └────┬─────┘  │
│         │                 │               │              │        │
│         └─────────────────┴───────────────┴──────────────┘        │
│                                   │                                │
├───────────────────────────────────┼────────────────────────────────┤
│  Service layer   ✅ IMPLEMENTED    │                                │
│                    ┌──────────────▼──────────────┐                 │
│                    │      AssayService            │                 │
│                    │  evaluate() · batch() ·      │                 │
│                    │  library() · session()       │                 │
│                    └──────────────┬──────────────┘                 │
├───────────────────────────────────┼────────────────────────────────┤
│  Core engine                      │                                │
│         ┌─────────────────────────┼──────────────────────┐        │
│         │             ┌───────────▼──────────┐            │        │
│         │             │   FactorEngine ✅     │            │        │
│         │             │ Parser ✅ · DAG/CSE 📋 │            │        │
│         │             └───────────┬──────────┘            │        │
│  ┌──────┴──────┐   ┌──────────────┴──────┐  ┌──────────┐  │        │
│  │ DataStore ✅ │   │ Cache 🔶 session+L2 │  │Evaluator │  │        │
│  │  (Parquet)  │   │  (L1 Arena/incr 📋)  │  │IC/Decay✅│  │        │
│  └─────────────┘   └─────────────────────┘  └──────────┘  │        │
│         └─────────────────────────────────────────────────┘        │
└────────────────────────────────────────────────────────────────────┘
```

Built today: the full stack except the React WebUI — `DataStore`, `FactorEngine` +
dual-syntax parser + operators + diagnostics, the IC/decay/groups/turnover `Evaluator`,
the `FactorLibrary`, the `AssayService` facade, and the SDK / REST / MCP / CLI surfaces.
The cache is a session + L2-disk cache (the L1 Arena and incremental O(1) update layer
remain a perf optimization), and `batch()` runs factors independently (DAG/CSE is future).

### 1.3  Surface comparison

Latency targets remain design goals (no L1 cache / DAG-CSE yet), but all four code
surfaces below except the WebUI are implemented and tested.

| Surface | Protocol | Auth | Latency target | Primary user | Status |
|---|---|---|---|---|---|
| Python SDK | In-process | None (local) | < 400ms warm | Researcher, notebook | ✅ `assay.backtest/batch_backtest/Session/library/stream` |
| REST API | HTTP/SSE | API key (optional) | < 500ms warm | External apps, CI | ✅ FastAPI + SSE, all `/v1/*` routes |
| WebUI | HTTP → REST | same-origin | < 600ms warm | Human researcher | 🔶 zero-install vanilla-JS app (`api/static/`, served by FastAPI); React stack is the target rebuild |
| MCP Server | JSON-RPC (stdio/SSE) | API key | < 600ms warm | LLM agent | ✅ FastMCP, 11 tools, stdio + SSE |

---

## 2. Core Engine (Shared Backend)

> **Status: ✅ Implemented** ([`src/assay/service.py`](../../src/assay/service.py)). The
> `AssayService` singleton, sessions, streaming, and library wiring all exist. The sketch
> below matches the shipped API closely; the live constructor wires `DataStore` (lazily),
> `FactorLibrary`, the `L2FactorCache`, and a `SessionRegistry`. `batch()` is implemented
> via a thread pool (DAG/CSE merging is still a future optimization). See §2.1 for a minimal
> direct-engine path that bypasses the service.

All four surfaces route through `AssayService`, which owns the engine, cache, and data access. It is never instantiated more than once per process.

```python
# assay/service.py   ✅ IMPLEMENTED (sketch — see source for the exact signature)

class AssayService:
    """
    Singleton service. All surfaces call this.
    Thread-safe: uses a read-write lock on the factor library.
    """
    _instance: "AssayService | None" = None

    def __init__(self, config: AssayConfig):
        self.data_store    = DataStore(config.data_path)
        self.factor_engine = FactorEngine(config)
        self.cache         = CacheManager(config.cache)
        self.library       = FactorLibrary(config.library_path)
        self.evaluator     = Evaluator(config)

    @classmethod
    def get(cls) -> "AssayService":
        if cls._instance is None:
            raise RuntimeError("AssayService not initialized")
        return cls._instance

    @classmethod
    def init(cls, config: AssayConfig) -> "AssayService":
        cls._instance = cls(config)
        return cls._instance

    # ── Primary methods (all surfaces call these) ────────────

    async def evaluate(
        self,
        expr:       str,
        universe:   str,
        period:     tuple[str, str],
        horizons:   list[int] = [1, 5, 10, 20],
        execution:  str = "next_open",
        neutralize: list[str] | None = None,
        as_of:      str | None = None,
        stream:     bool = False,
    ) -> FactorReport | AsyncGenerator[FactorEvent, None]:
        ...

    async def batch(
        self,
        exprs:     list[str],
        universe:  str,
        period:    tuple[str, str],
        n_jobs:    int = 8,
        **kwargs,
    ) -> list[FactorReport]:
        ...

    def library_query(
        self,
        universe:       str | None = None,
        min_rank_icir:  float = 0.0,
        max_redundancy: float = 1.0,
        source:         str | None = None,
        sort_by:        str = "rank_icir",
        limit:          int = 100,
    ) -> list[FactorSummary]:
        ...

    def correlation_matrix(
        self,
        factor_ids: list[str],
        universe:   str,
        period:     tuple[str, str],
    ) -> CorrelationResult:
        ...
```

### 2.1  What exists today — the engine entry point ✅

Until `AssayService` lands, the engine is driven directly. A `DataStore` reads a
point-in-time panel; `FactorEngine.from_store` pivots it into aligned `(T, N)` matrices
and evaluates a parsed expression. This is the real, runnable cold path
([`src/assay/engine/engine.py`](../../src/assay/engine/engine.py),
[`src/assay/data/store/datastore.py`](../../src/assay/data/store/datastore.py)):

```python
from assay.config import AssayConfig
from assay.data.store import DataStore
from assay.engine import FactorEngine, parse, lint

store = DataStore(AssayConfig.from_env())

# Build an engine over a PIT panel (look-ahead-safe: as_of is required).
eng = FactorEngine.from_store(
    store,
    universe = "NASDAQ100",                       # only universe wired up today
    period   = ("2023-01-01", "2023-12-31"),
    as_of    = "2023-12-31",
    adj      = "split",                            # none | split | total (alias: forward)
)

result = eng.evaluate("cs_rank(ts_corr(close, volume, 20))")
result.values        # (T, N) float64 factor matrix
result.to_frame()    # long (date, symbol, factor) Polars DataFrame

# Structured, JSON-serialisable diagnostics for the agent loop (never raises):
fd = eng.diagnose("ts_corr(close, volume, 20)")
fd.to_dict()         # {status, failure_mode, errors[], warnings[], stats}

# Panel-free syntax lint (no data needed):
lint("ts_mean(close,").to_dict()                  # ASSAY-P00x parse diagnostic
```

The IC/RankIC evaluator, forward returns, cache and `FactorReport` assembly are **now
implemented** — `AssayService.evaluate()` returns a scored `FactorReport` (the §2.1 engine
path here is just the lowest-level primitive). Batch DAG/CSE merging remains the one
unbuilt performance item; `service.batch()` works but evaluates factors independently.

---

## 3. Functional Python API

> **Status: ✅ Implemented** ([`src/assay/__init__.py`](../../src/assay/__init__.py)). The full
> SDK below — `assay.init`, `assay.backtest`, `assay.batch_backtest`, `assay.Session`,
> `assay.library`, `assay.stream` — is shipped and tested. `assay.init()` reads config from
> the environment / project `.env`; `backtest()` auto-initializes if you skip it.

The SDK is a thin, ergonomic wrapper over `AssayService`. It is the lowest-latency surface because it runs in-process with no serialization.

### 3.1  Installation and initialization 📋

```python
pip install assay-engine          # 📋 not yet published; install from source (§8.1)

import assay

# Initialize once — reads config from env / project .env (see §7.4).
assay.init()                       # 📋 planned; today: AssayConfig.from_env()
```

### 3.2  Single factor evaluation

```python
# Minimal
report = assay.backtest("ts_returns(close, 20)", universe="NASDAQ100")

# Full options
report = assay.backtest(
    expr       = "ts_corr(close, volume, 20)",
    universe   = "NASDAQ100",
    period     = ("2020-01-01", "2024-12-31"),
    horizons   = [1, 5, 10, 20],
    execution  = "next_open",
    neutralize = ["sector"],
    as_of      = "2024-12-31",        # PIT: only data known by this date
)

# Access results
print(report.rank_ic)                 # 0.047
print(report.rank_icir)               # 0.61
print(report.decay_halflife_days)     # 12
print(report.lookahead_detected)      # False
print(report.suggestion)              # "Consider shorter window..."

# Serialize
report.to_dict()                      # dict — for agent consumption
report.to_json()                      # JSON string
report.to_dataframe()                 # pd.DataFrame of IC time series
```

### 3.3  Batch evaluation

```python
factors = [
    "ts_returns(close, 20)",
    "ts_corr(close, volume, 20)",
    "cs_rank(ts_std(ts_returns(close,1), 20))",
    # ... hundreds of LLM-generated expressions
]

reports = assay.batch_backtest(
    exprs    = factors,
    universe = "NASDAQ100",
    period   = ("2020-01-01", "2024-12-31"),
    n_jobs   = 8,
    sort_by  = "rank_icir",
)

# Reports sorted by rank_icir descending
for r in reports[:5]:
    print(f"{r.expr:<50}  IC={r.rank_ic:.3f}  ICIR={r.rank_icir:.2f}")
```

### 3.4  Session context (amortize setup costs)

```python
# Session pre-loads the data panel and forward returns once.
# All factors in the session share the same loaded data — no repeated I/O.

with assay.Session(
    universe = "NASDAQ100",
    period   = ("2020-01-01", "2024-12-31"),
) as sess:
    r1 = sess.backtest("ts_returns(close, 20)")        # ~350ms (first: loads panel)
    r2 = sess.backtest("ts_corr(close, volume, 20)")   # ~40ms  (panel already loaded)
    r3 = sess.backtest("cs_rank(ts_std(close, 20))")   # ~35ms

    # Batch within a session — shares panel + fwd returns
    reports = sess.batch_backtest(factors, n_jobs=8)
```

### 3.5  Custom Python factor functions

```python
import polars as pl

def my_factor(df: pl.DataFrame) -> pl.Series:
    """df has columns: date, symbol, open, high, low, close, volume, transactions

    (The MASSIVE day-aggregate source provides OHLCV + transaction count; `vwap`
    is not available — derive a proxy from price/volume if you need one.)
    """
    momentum = df["close"] / df["close"].shift(20) - 1
    return momentum * df["volume"] / df["volume"].shift(20)

report = assay.backtest(my_factor, universe="NASDAQ100")   # 📋 custom-fn path planned
```

### 3.6  Factor library access

```python
# Query the library
factors = assay.library.list(
    min_rank_icir  = 0.5,
    max_redundancy = 0.6,
    sort_by        = "rank_icir",
)

# Correlation matrix
corr = assay.library.correlation_matrix(
    factor_ids = [f.factor_id for f in factors[:50]],
    universe   = "NASDAQ100",
)

# Add / remove
assay.library.save(report)
assay.library.delete(factor_id="abc123")
assay.library.prune(redundancy_threshold=0.7, dry_run=True)
```

### 3.7  Streaming (async)

```python
import asyncio

async def watch_evaluation():
    async for event in assay.stream("ts_corr(close, volume, 20)", universe="NASDAQ100"):
        if event.type == "ic_series":
            print(f"IC mean so far: {event.data['ic_mean']:.3f}")
        elif event.type == "complete":
            print(f"Done: RankICIR = {event.report.rank_icir:.2f}")

asyncio.run(watch_evaluation())
```

---

## 4. REST API

> **Status: ✅ Implemented** ([`src/assay/api/`](../../src/assay/api/)). The FastAPI app, all
> `/v1/*` routes, SSE streaming, optional API-key auth (`ASSAY_API_KEYS`; open if unset),
> and the diagnostics-based error schema (§4.6) are shipped and tested. Run it with
> `python -m assay.cli serve-api` (or `uvicorn assay.api.app:app`).

The REST API is a FastAPI application. It exposes the same `AssayService` methods over HTTP. Streaming evaluation uses Server-Sent Events (SSE).

### 4.1  Base URL and versioning

```
Base URL:  https://api.assay.local/v1
Auth:      X-API-Key header (or Bearer token for WebUI sessions)
Format:    application/json (default), text/event-stream (SSE)
```

### 4.2  Evaluation endpoints

#### `POST /v1/factor/evaluate`

Evaluate a single factor. Supports both blocking (returns full report) and streaming (SSE events) modes.

```
Request:
  Content-Type: application/json

  {
    "expr":       "ts_corr(close, volume, 20)",
    "universe":   "NASDAQ100",
    "period":     ["2020-01-01", "2024-12-31"],
    "horizons":   [1, 5, 10, 20],
    "execution":  "next_open",
    "neutralize": ["sector"],
    "as_of":      "2024-12-31",
    "stream":     false
  }

Response (stream=false):
  HTTP 200  application/json
  { ...FactorReport }

Response (stream=true):
  HTTP 200  text/event-stream

  event: eval.started
  data: {"factor_id": "abc123", "expr": "ts_corr(close, volume, 20)"}

  event: eval.ic_series
  data: {"ic": [0.03, 0.05, ...], "dates": ["2020-01-02", ...]}

  event: eval.decay
  data: {"ic_by_horizon": {"1": 0.047, "5": 0.041, "10": 0.035, "20": 0.028}, "halflife": 12}

  event: eval.groups
  data: {"quintile_returns": {"Q1": -0.0012, "Q2": -0.0003, "Q3": 0.0002, "Q4": 0.0008, "Q5": 0.0015}}

  event: eval.complete
  data: { ...full FactorReport }
```

#### `POST /v1/factor/batch`

Evaluate a list of factors in parallel. Returns when all complete.

```
Request:
  {
    "exprs":    ["ts_returns(close, 20)", "ts_corr(close, volume, 20)", ...],
    "universe": "NASDAQ100",
    "period":   ["2020-01-01", "2024-12-31"],
    "n_jobs":   8,
    "sort_by":  "rank_icir"
  }

Response:
  HTTP 200
  {
    "total":   50,
    "elapsed_ms": 5420,
    "reports": [ ...FactorReport[], sorted by rank_icir ]
  }
```

#### `POST /v1/session/create`

Create a session that pre-loads the data panel. Subsequent evaluate calls in the session skip the panel load (~265ms saved per factor).

```
Request:
  { "universe": "NASDAQ100", "period": ["2020-01-01", "2024-12-31"] }

Response:
  { "session_id": "sess_xyz789", "setup_ms": 309, "expires_at": "2025-01-16T02:00:00Z" }

Usage:
  POST /v1/factor/evaluate  with header  X-Session-Id: sess_xyz789
  → panel load skipped, hot path only (~40ms)
```

### 4.3  Library endpoints

```
GET  /v1/library/factors
     ?universe=NASDAQ100&min_rank_icir=0.5&max_redundancy=0.6
      &source=AGENT&sort_by=rank_icir&limit=100&offset=0
  → { "total": 284, "factors": [FactorSummary...] }

GET  /v1/library/factors/{factor_id}
  → FactorReport (full)

POST /v1/library/factors
     body: FactorReport
  → { "factor_id": "abc123", "saved": true }

DELETE /v1/library/factors
       body: { "factor_ids": ["abc123", "def456"] }
  → { "deleted": 2 }

GET  /v1/library/correlation-matrix
     ?factor_ids=abc123,def456,...&universe=NASDAQ100&period=2020-01-01,2024-12-31
  → { "matrix": [[1.0, 0.82, ...], ...], "factor_ids": ["abc123", ...] }

GET  /v1/library/embedding
     ?factor_ids=...&universe=NASDAQ100&period=...
  → { "coords": [{"id": "abc123", "x": 0.43, "y": -0.21, "rank_icir": 0.61}, ...] }

POST /v1/library/prune
     body: { "redundancy_threshold": 0.7, "dry_run": true }
  → { "would_delete": ["abc123", ...], "count": 14 }
```

### 4.4  System endpoints

```
GET  /v1/system/status
  → {
      "engine_version": "0.1.0",
      "data": {
        "market": "US",
        "last_sync": "2024-12-31T21:05:00Z",
        "trading_days_available": 1260,
        "symbols_available": 101
      },
      "cache": {
        "l1_entries": 892,
        "l1_hit_rate_1h": 0.84,
        "l2_entries": 4201,
        "l2_size_gb": 4.2
      },
      "active_sessions": 3
    }

GET  /v1/system/data-calendar?market=US&year=2024
  → [{ "date": "2024-01-02", "coverage_pct": 1.0, "last_sync": "2024-01-03T..." }, ...]

GET  /v1/system/universes
  → [{ "id": "NASDAQ100", "n_symbols": 101, "last_rebalance": "2024-12-20" }, ...]
```

### 4.5  FastAPI application structure

```python
# assay/api/app.py

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="Assay API", version="0.1.0")

app.add_middleware(CORSMiddleware, allow_origins=["*"])

app.include_router(factor_router,  prefix="/v1/factor",  tags=["Factor"])
app.include_router(library_router, prefix="/v1/library", tags=["Library"])
app.include_router(session_router, prefix="/v1/session", tags=["Session"])
app.include_router(system_router,  prefix="/v1/system",  tags=["System"])


# assay/api/routes/factor.py

@router.post("/evaluate")
async def evaluate_factor(req: EvaluateRequest, api_key: APIKey = Depends(auth)):
    svc = AssayService.get()
    if req.stream:
        return StreamingResponse(
            svc.evaluate(stream=True, **req.dict(exclude={"stream"})),
            media_type="text/event-stream"
        )
    report = await svc.evaluate(**req.dict(exclude={"stream"}))
    return report.to_dict()
```

### 4.6  Error schema

Errors mirror the implemented diagnostics system ([`assay.engine.diagnostics`](../../src/assay/engine/diagnostics.py)).
Each problem carries the stable diagnostic `code` (`ASSAY-P###` parse / `ASSAY-E###`
execute / `ASSAY-O###` output), the coarse `failure_mode`, a character `location`, and an
actionable `suggestion`:

```json
{
  "error": {
    "code":       "ASSAY-E007",
    "name":       "LOOKAHEAD_SHIFT",
    "failure_mode": "LOOKAHEAD",
    "severity":   "error",
    "stage":      "execute",
    "message":    "A negative look-back peeks into the future (ts_delay(x, d>=0)).",
    "location":   { "start": 0, "end": 8, "snippet": "ts_delay(close, -5)\n^^^^^^^^" },
    "suggestion": "Use a non-negative window.",
    "factor_id":  "abc123"
  }
}
```

`failure_mode` (FactorReport-level, from the diagnostics catalog): `SYNTAX_ERROR` ·
`LOOKAHEAD` · `CONSTANT` · `ALL_NAN` · `RUNTIME_ERROR`. Transport/data errors not produced
by the engine — `DATA_NOT_FOUND`, `UNIVERSE_NOT_FOUND`, `SESSION_EXPIRED` — are 📋 planned
service-layer additions on top of these.

---

## 5. WebUI Frontend

> **Status: 🔶 A runnable WebUI exists — but not in this React/Vite stack.** Because the
> build environment has no npm access, the shipped UI is a **zero-install vanilla-JS app**
> at [`src/assay/api/static/`](../../src/assay/api/static/), served by FastAPI (`serve-api` →
> `http://localhost:8000`): the three screens, hand-rolled SVG charts, a lightweight editor
> (not Monaco), and SSE-streaming evaluate, all on the real `/v1/*` API. The React 18 + Vite
> + Recharts + Monaco stack below remains the **target rebuild** (📋); see the companion
> **Assay WebUI — Detailed Design** doc ([webui.md](webui.md)).

### 5.1  Tech stack

| Layer | Technology | Reason |
|---|---|---|
| Framework | React 18 | Concurrent rendering for progressive chart streaming |
| Build | Vite | Fast HMR, tree-shaking, < 200ms dev reload |
| Routing | React Router v6 | File-based routing, nested layouts |
| State | Zustand | Lightweight global state; no boilerplate |
| Data fetching | React Query + fetch | SSE streaming support, caching, background refetch |
| Charts | Recharts | React-native, composable, sufficient for all Phase 1 chart types |
| Editor | Monaco Editor | VS Code engine; custom Assay language extension |
| Styling | Tailwind CSS | Utility-first, design tokens, dark mode |
| Icons | Tabler Icons | Consistent outline style, 5800+ icons |
| Types | TypeScript | End-to-end type safety via OpenAPI codegen |

### 5.2  Project structure

```
assay-ui/
├── src/
│   ├── api/                    # Auto-generated OpenAPI client + custom hooks
│   │   ├── client.ts           # Generated from /v1/openapi.json
│   │   ├── hooks/
│   │   │   ├── useEvaluate.ts  # React Query hook wrapping SSE stream
│   │   │   ├── useLibrary.ts
│   │   │   └── useSystem.ts
│   │   └── types.ts            # FactorReport, FactorSummary, etc.
│   │
│   ├── components/
│   │   ├── editor/
│   │   │   ├── ExprEditor.tsx  # Monaco instance with Assay language extension
│   │   │   ├── assay-lang.ts   # Token rules, autocomplete, param hints
│   │   │   └── syntax-bridge.ts # qlib ↔ Python syntax conversion
│   │   ├── charts/
│   │   │   ├── ICTimeSeries.tsx
│   │   │   ├── DecayCurve.tsx
│   │   │   ├── GroupReturns.tsx
│   │   │   ├── FactorHeatmap.tsx
│   │   │   ├── TurnoverPlot.tsx
│   │   │   └── ReturnDistribution.tsx
│   │   ├── library/
│   │   │   ├── FactorList.tsx
│   │   │   ├── CorrelationMatrix.tsx
│   │   │   ├── AlphaSpaceMap.tsx
│   │   │   └── LineageDAG.tsx
│   │   └── shared/
│   │       ├── FactorReport.tsx   # Structured FactorReport display
│   │       ├── MetricCard.tsx
│   │       └── StatusBadge.tsx
│   │
│   ├── pages/
│   │   ├── Dashboard.tsx
│   │   ├── FactorLibrary.tsx
│   │   └── SingleFactorTest.tsx
│   │
│   ├── store/
│   │   ├── global.ts           # universe, period, session_id (shared across pages)
│   │   └── library.ts          # factor library client-side cache
│   │
│   └── main.tsx
│
├── public/
└── vite.config.ts
```

### 5.3  SSE streaming hook

```typescript
// src/api/hooks/useEvaluate.ts

type EvalEvent =
  | { type: "eval.started";   data: { factor_id: string } }
  | { type: "eval.ic_series"; data: { ic: number[]; dates: string[] } }
  | { type: "eval.decay";     data: { ic_by_horizon: Record<number, number>; halflife: number } }
  | { type: "eval.groups";    data: { quintile_returns: Record<string, number> } }
  | { type: "eval.complete";  data: FactorReport }

export function useEvaluate() {
  const [events, setEvents] = useState<EvalEvent[]>([])
  const [isLoading, setIsLoading] = useState(false)

  const evaluate = useCallback(async (req: EvaluateRequest) => {
    setIsLoading(true)
    setEvents([])

    const res = await fetch("/v1/factor/evaluate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...req, stream: true }),
    })

    const reader = res.body!.getReader()
    const decoder = new TextDecoder()

    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      const lines = decoder.decode(value).split("\n")
      for (const line of lines) {
        if (line.startsWith("data: ")) {
          const event = JSON.parse(line.slice(6)) as EvalEvent
          setEvents(prev => [...prev, event])
          if (event.type === "eval.complete") setIsLoading(false)
        }
      }
    }
  }, [])

  return { evaluate, events, isLoading }
}
```

### 5.4  Monaco Assay language extension

```typescript
// src/components/editor/assay-lang.ts

import * as monaco from "monaco-editor"
import { OPERATOR_SCHEMA } from "@/api/types"

export function registerAssayLanguage() {
  monaco.languages.register({ id: "assay" })

  // Syntax highlighting: operators, fields, numbers, operators
  monaco.languages.setMonarchTokensProvider("assay", {
    tokenizer: {
      root: [
        [/\$[a-z_]+/, "variable.predefined"],         // qlib $close
        [/\b(ts_|cs_|calc_)\w+/, "keyword"],           // Assay operators
        [/\b(Ref|Mean|Std|Corr|EMA|Rank|Delta)\b/, "keyword.control"], // qlib ops
        [/\b(open|high|low|close|volume|transactions)\b/, "variable"],  // MASSIVE provides no vwap
        [/[-+*/().,<>?:]/, "operator"],
        [/\d+(\.\d+)?/, "number"],
      ]
    }
  })

  // Autocomplete: ts_ prefix → all time-series operators with signatures
  monaco.languages.registerCompletionItemProvider("assay", {
    provideCompletionItems(model, position) {
      const word = model.getWordUntilPosition(position)
      return {
        suggestions: Object.entries(OPERATOR_SCHEMA).map(([name, schema]) => ({
          label: name,
          kind: monaco.languages.CompletionItemKind.Function,
          insertText: schema.insert_template,  // e.g. "ts_corr(${1:x}, ${2:y}, ${3:d})"
          insertTextRules: monaco.languages.CompletionItemInsertTextRule.InsertAsSnippet,
          documentation: schema.description,
          detail: schema.signature,
        }))
      }
    }
  })

  // Parameter hints inside function calls
  monaco.languages.registerSignatureHelpProvider("assay", {
    signatureHelpTriggerCharacters: ["(", ","],
    provideSignatureHelp(model, position) {
      // Parse which function and argument we're inside
      // Return parameter documentation from OPERATOR_SCHEMA
      ...
    }
  })
}
```

### 5.5  Global state

```typescript
// src/store/global.ts  (Zustand)

interface GlobalState {
  universe:   string
  period:     [string, string]
  sessionId:  string | null
  setUniverse: (u: string) => void
  setPeriod:   (p: [string, string]) => void
  createSession: () => Promise<void>
}

export const useGlobal = create<GlobalState>((set, get) => ({
  universe:  "NASDAQ100",
  period:    ["2020-01-01", "2024-12-31"],
  sessionId: null,

  setUniverse: (universe) => {
    set({ universe, sessionId: null })  // changing universe invalidates session
    get().createSession()
  },

  setPeriod: (period) => {
    set({ period, sessionId: null })
    get().createSession()
  },

  createSession: async () => {
    const { universe, period } = get()
    const res = await fetch("/v1/session/create", {
      method: "POST",
      body: JSON.stringify({ universe, period })
    })
    const { session_id } = await res.json()
    set({ sessionId: session_id })
  }
}))
```

---

## 6. MCP Server for Agents

> **Status: ✅ Implemented** ([`src/assay/mcp/server.py`](../../src/assay/mcp/server.py)). Built
> on the high-level **FastMCP** API (`mcp.server.fastmcp.FastMCP`); all eight tools below
> are registered over `AssayService`, and `assay_evaluate`'s description is enriched with
> the live `operator_schema()` (§6.6). Run it with `python -m assay.mcp.server` (stdio) or
> `python -m assay.cli serve-mcp --transport sse --port 8001`. (Note: the SDK-style
> low-level snippet in §6.4 predates the FastMCP implementation — see the source for the
> shipped decorator-based form.)

The MCP server exposes Assay's evaluation capabilities as tools that LLM agents can call via the Model Context Protocol. This is the primary interface for LLM agent-driven alpha mining.

### 6.1  Protocol

```
Transport:       stdio (default, for Claude Desktop / local agents)
                 SSE over HTTP (for remote agents, e.g. cloud-hosted LLMs)
MCP version:     2024-11-05
Auth (SSE):      X-API-Key header
```

### 6.2  Exposed tools

The MCP server exposes eight tools covering the full evaluation-library loop:

```
assay_evaluate           — evaluate a single factor expression
assay_batch              — evaluate multiple factors in parallel
assay_library_list       — list factors in the library with filters
assay_library_get        — get the full FactorReport for one factor
assay_library_save       — save a FactorReport to the library
assay_library_correlation— compute pairwise correlation between factors
assay_library_prune      — identify and optionally remove redundant factors
assay_system_status      — get data freshness and cache statistics
```

### 6.3  Tool definitions

```json
{
  "name": "assay_evaluate",
  "description": "Evaluate a quantitative alpha factor expression and return a structured FactorReport with IC, RankIC, decay, redundancy score, lookahead detection, and a natural language suggestion. Supports both qlib syntax (Ref($close,5)) and Python syntax (ts_delay(close,5)).",
  "inputSchema": {
    "type": "object",
    "properties": {
      "expr":      { "type": "string", "description": "Factor expression (qlib or Python syntax)" },
      "universe":  { "type": "string", "enum": ["NASDAQ100", "SP500", "Russell2000"], "default": "NASDAQ100", "description": "Only NASDAQ100 is wired up today; SP500/Russell2000 are roadmap." },
      "period":    { "type": "array",  "items": {"type": "string"}, "description": "['YYYY-MM-DD', 'YYYY-MM-DD']" },
      "horizons":  { "type": "array",  "items": {"type": "integer"}, "default": [1, 5, 10, 20] },
      "neutralize":{ "type": "array",  "items": {"type": "string"}, "description": "['sector', 'industry'] or null" }
    },
    "required": ["expr"]
  }
}
```

```json
{
  "name": "assay_batch",
  "description": "Evaluate multiple factor expressions in parallel using DAG-aware common subexpression elimination. Returns results sorted by rank_icir. Prefer this over calling assay_evaluate in a loop — batch evaluation is 10-50x faster due to shared intermediate computations.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "exprs":    { "type": "array", "items": {"type": "string"}, "description": "List of factor expressions" },
      "universe": { "type": "string", "default": "NASDAQ100" },
      "period":   { "type": "array",  "items": {"type": "string"} },
      "n_jobs":   { "type": "integer", "default": 8, "description": "Parallel workers" },
      "sort_by":  { "type": "string", "enum": ["rank_icir", "rank_ic", "decay_halflife"], "default": "rank_icir" }
    },
    "required": ["exprs"]
  }
}
```

```json
{
  "name": "assay_library_list",
  "description": "List factors in the Assay factor library with optional filters. Use this to understand what factors already exist before generating new ones — the redundancy_score field tells you how similar a new factor is to existing ones.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "min_rank_icir":  { "type": "number", "default": 0.0 },
      "max_redundancy": { "type": "number", "default": 1.0, "description": "0.0-1.0; lower = more unique" },
      "source":         { "type": "string", "enum": ["AGENT", "HUMAN", "WQ101", "IMPORTED"] },
      "sort_by":        { "type": "string", "default": "rank_icir" },
      "limit":          { "type": "integer", "default": 20, "maximum": 100 }
    }
  }
}
```

```json
{
  "name": "assay_library_correlation",
  "description": "Compute pairwise rank-correlation between a list of factor IDs. Returns a correlation matrix. Use this to check if a newly generated factor is redundant with existing library factors before submitting it for detailed evaluation.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "factor_ids": { "type": "array", "items": {"type": "string"}, "description": "List of factor_id strings from the library" },
      "universe":   { "type": "string", "default": "NASDAQ100" }
    },
    "required": ["factor_ids"]
  }
}
```

```json
{
  "name": "assay_library_prune",
  "description": "Identify factors in the library with pairwise correlation above a threshold. In dry_run mode, returns the list of dominated factors that would be removed. In non-dry-run mode, removes them.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "redundancy_threshold": { "type": "number", "default": 0.7 },
      "dry_run":              { "type": "boolean", "default": true }
    }
  }
}
```

### 6.4  MCP server implementation

```python
# assay/mcp/server.py

from mcp import Server, Tool
from mcp.server.stdio import stdio_server
from assay.service import AssayService

server = Server("assay")

@server.tool()
async def assay_evaluate(
    expr:       str,
    universe:   str = "NASDAQ100",
    period:     list[str] | None = None,
    horizons:   list[int] = [1, 5, 10, 20],
    neutralize: list[str] | None = None,
) -> dict:
    """Evaluate a factor expression and return a FactorReport."""
    svc = AssayService.get()
    period = period or ["2020-01-01", "2024-12-31"]
    report = await svc.evaluate(
        expr=expr, universe=universe,
        period=tuple(period), horizons=horizons,
        neutralize=neutralize,
    )
    return report.to_dict()


@server.tool()
async def assay_batch(
    exprs:    list[str],
    universe: str = "NASDAQ100",
    period:   list[str] | None = None,
    n_jobs:   int = 8,
    sort_by:  str = "rank_icir",
) -> dict:
    """Batch evaluate factors. Faster than calling assay_evaluate in a loop."""
    svc = AssayService.get()
    period = period or ["2020-01-01", "2024-12-31"]
    reports = await svc.batch(
        exprs=exprs, universe=universe,
        period=tuple(period), n_jobs=n_jobs,
    )
    sorted_reports = sorted(reports, key=lambda r: -getattr(r, sort_by, 0))
    return {
        "total": len(sorted_reports),
        "reports": [r.to_dict() for r in sorted_reports],
    }


@server.tool()
async def assay_library_list(
    min_rank_icir:  float = 0.0,
    max_redundancy: float = 1.0,
    source:         str | None = None,
    sort_by:        str = "rank_icir",
    limit:          int = 20,
) -> dict:
    svc = AssayService.get()
    factors = svc.library_query(
        min_rank_icir=min_rank_icir,
        max_redundancy=max_redundancy,
        source=source, sort_by=sort_by, limit=limit,
    )
    return {"total": len(factors), "factors": [f.to_dict() for f in factors]}


# Run as stdio server
if __name__ == "__main__":
    import asyncio
    asyncio.run(stdio_server(server))
```

### 6.5  MCP server startup

```bash
# stdio mode (Claude Desktop, local agents)
python -m assay.mcp.server

# SSE mode (remote agents)
python -m assay.mcp.server --transport sse --port 8001

# Claude Desktop config (~/.config/claude/claude_desktop_config.json)
{
  "mcpServers": {
    "assay": {
      "command": "python",
      "args": ["-m", "assay.mcp.server"],
      "env": { "ASSAY_CONFIG": "~/.assay/config.toml" }
    }
  }
}
```

### 6.6  Operator schema injection

The MCP server injects the full operator schema into the tool descriptions so agents have documentation inline without making separate calls. The schema source is **implemented today**: `assay.engine.operator_schema()` returns a live `{name: schema}` view of every registered operator (built-in *and* user-registered), so this enrichment works as soon as the server exists:

```python
from assay.engine import operator_schema   # ✅ available now

# Dynamically enrich the assay_evaluate tool description
# with the operator schema so agents know what operators exist
OPERATOR_DOCS = "\n\nAvailable operators (prefix ts_ = time-series, cs_ = cross-sectional):\n"
for name, schema in operator_schema().items():
    sig = schema.get("signature", name)
    OPERATOR_DOCS += f"  {sig}: {schema.get('description', '')}  ({schema.get('output_range', '')})\n"

assay_evaluate.__doc__ += OPERATOR_DOCS
```

### 6.7  Typical agent loop

```
Agent iteration 1:
  call assay_library_list(limit=10) → "what high-quality factors already exist?"
  call assay_evaluate("ts_returns(close, 20)") → RankICIR=0.61

Agent iteration 2:
  call assay_batch(["ts_corr(close,volume,20)", "cs_rank(ts_std(close,20))", ...10 more])
  → sorted results, top factor: RankICIR=0.74, redundancy=0.31

Agent iteration 3:
  call assay_library_correlation(factor_ids=[...top 5 new + 5 existing...])
  → "new factor B is 0.82 correlated with existing factor X — skip saving"

Agent iteration 4:
  call assay_library_save(report=best_report)
  call assay_library_prune(redundancy_threshold=0.7, dry_run=True)
  → "14 factors would be removed; proceed?"
```

---

## 7. Cross-Cutting Concerns

> **Status:** §7.1 auth is ✅ implemented (optional API-key, `ASSAY_API_KEYS`; open if
> unset). §7.2 rate limiting and §7.3 observability are 📋 planned (no slowapi/Prometheus/
> OTel wired yet). §7.4 documents the config mechanism in place today.

### 7.1  Authentication ✅ (optional API key)

```python
# API key for REST + MCP
# Session cookie for WebUI (issued after API key auth)

class APIKeyAuth:
    def __init__(self, keys: set[str]):
        self._keys = keys

    def __call__(self, x_api_key: str = Header(...)):
        if x_api_key not in self._keys:
            raise HTTPException(status_code=401, detail="Invalid API key")
        return x_api_key

# Configured in config.toml
# [auth]
# api_keys = ["sk-assay-abc123", "sk-assay-def456"]
```

### 7.2  Rate limiting 📋

```python
# Per API key, per surface
# REST: 60 evaluate/min, 10 batch/min
# MCP:  120 tool calls/min (agents need higher throughput)

from slowapi import Limiter
limiter = Limiter(key_func=get_api_key)

@router.post("/evaluate")
@limiter.limit("60/minute")
async def evaluate_factor(...): ...
```

### 7.3  Observability 📋

```
Metrics (Prometheus):
  assay_evaluate_duration_ms        histogram  [surface, universe, cache_hit]
  assay_batch_size                  histogram  [surface]
  assay_cache_hit_rate              gauge      [level: l1|l2]
  assay_library_size                gauge
  assay_active_sessions             gauge

Traces (OpenTelemetry):
  span: assay.evaluate → assay.engine.parse → assay.cache.get → assay.operator.execute → assay.ic.compute

Logs:
  structured JSON, level=INFO for every evaluate call
  fields: factor_id, expr_hash, universe, elapsed_ms, cache_hit, rank_icir, surface
```

### 7.4  Configuration ✅ (today: env vars + `.env`)

**What exists today.** Configuration is environment-variable based, loaded by
[`AssayConfig.from_env()`](../../src/assay/config.py). At import the package also reads a
project-root `.env` file with `setdefault` semantics, so the real shell environment always
wins. There is **no** `~/.assay/config.toml` yet. Required and optional variables (see
[`.env.example`](../../.env.example)):

```bash
# Required — MASSIVE credentials
MASSIVE_API_KEY=...                       # REST bearer token (api.massive.com)
MASSIVE_S3_ACCESS_KEY_ID=...              # flat-files S3 access key id
MASSIVE_S3_SECRET_ACCESS_KEY=...          # flat-files S3 secret

# Optional — sensible defaults
MASSIVE_S3_ENDPOINT=https://files.massive.com
MASSIVE_S3_BUCKET=flatfiles
MASSIVE_REST_BASE_URL=https://api.massive.com
ASSAY_DATA_DIR=./data                     # parquet store root; market defaults to "US"
```

```python
from assay.config import AssayConfig
config = AssayConfig.from_env()           # -> AssayConfig(massive=..., data_dir=Path, market="US")
```

**Planned — unified `~/.assay/config.toml` 📋.** The service and surfaces now exist and
are configured today via `AssayConfig` fields + env vars; the *single TOML file* that would
subsume them is still the planned packaging. The `MASSIVE_*` secrets stay in the
environment; the rest would move into structured sections:

```toml
# ~/.assay/config.toml   📋 PLANNED packaging (the layers below already exist in code)

[data]
path       = "~/.assay/data"
market     = "US"
provider   = "massive"

[cache]                                    # 🔶 session + L2 disk cache exist; L1 arena 📋
l1_memory_gb = 4.0
l2_path      = "~/.assay/cache"
l2_max_gb    = 20.0

[api]                                      # ✅ REST layer built (serve-api); these keys map to AssayConfig
host = "0.0.0.0"
port = 8000

[mcp]                                      # ✅ MCP layer built (serve-mcp)
transport = "stdio"

[auth]                                     # ✅ optional API-key auth (env ASSAY_API_KEYS today)
api_keys = ["sk-assay-changeme"]

[engine]
n_workers        = 8
default_universe = "NASDAQ100"
default_period   = ["2020-01-01", "2024-12-31"]
```

---

## 8. Deployment

> **Status: 🔶 Mostly implemented.** The REST API and MCP server now run via
> `python -m assay.cli serve-api` and `serve-mcp` (§8.1a). Still not present: the
> single `assay server` umbrella command, `assay data sync` (use `prepare-nasdaq100`),
> the WebUI image, and the `pip install assay-engine` PyPI package (install from source).
> The Docker Compose stack (§8.3) references those not-yet-published images.

### 8.1a  Local development today ✅

The shipped entry point is the data/engine CLI ([`src/assay/cli.py`](../../src/assay/cli.py)),
invoked as `python -m assay.cli`. There is no console-script `assay` yet and no
`pip install assay-engine` package on PyPI — install from source.

```bash
# Install from source (editable)
pip install -e .                          # uses pyproject.toml
# Provide MASSIVE_* creds + ASSAY_DATA_DIR via ~/.bashrc or a project .env (§7.4)

# Prepare the NASDAQ-100 dataset (universe + corp-actions + prices) for a range
python -m assay.cli prepare-nasdaq100 --start 2023-01-01 --end 2023-12-31

# Inspect what's been ingested
python -m assay.cli status

# Read a PIT panel and print a summary (split-adjusted)
python -m assay.cli verify --start 2023-06-01 --end 2023-06-30 --adj split

# Parse / evaluate a single factor over the PIT panel
python -m assay.cli parse 'cs_rank(ts_corr(close, volume, 20))'
python -m assay.cli eval  'ts_returns(close, 20)' \
    --index NASDAQ100 --start 2023-01-01 --end 2023-12-31 --as-of 2023-12-31

# Full scored FactorReport via the SDK, batch from a file, library, and the servers
python -m assay.cli run   'cs_rank(-1 * ts_returns(close, 5))' --start 2023-01-01 --end 2023-12-31
python -m assay.cli batch factors.txt --start 2023-01-01 --end 2023-12-31 --output results.parquet
python -m assay.cli library list --sort rank_icir --limit 20
python -m assay.cli serve-api --host 0.0.0.0 --port 8000      # FastAPI (REST + SSE)
python -m assay.cli serve-mcp --transport sse --port 8001     # MCP server
```

### 8.1b  Planned researcher workflow 📋

Once the SDK/service/surfaces land, the intended one-liner experience is:

```bash
pip install assay-engine            # 📋 not yet published
assay init                          # 📋 creates ~/.assay/config.toml and data dirs
assay data sync --market US --start 2018-01-01    # 📋 unified data CLI
assay server start                  # 📋 REST :8000, WebUI :3000, MCP stdio

python
>>> import assay; assay.init()      # 📋 planned SDK
>>> assay.backtest("ts_returns(close, 20)", universe="NASDAQ100")
```

### 8.2  Service topology 📋

```
Researcher laptop / server (planned):

  ┌─────────────────────────────────────────────────┐
  │  assay server                                   │
  │                                                 │
  │  :8000  FastAPI (REST API)                      │
  │  :3000  Vite (WebUI dev) / Nginx (production)  │
  │  stdio  MCP server (spawned by agent host)      │
  │                                                 │
  │  All three share one AssayService process       │
  │  (single cache, single DataStore connection)    │
  └─────────────────────────────────────────────────┘
```

### 8.3  Docker Compose 📋

The three images below (`assay-api`, `assay-ui`, `assay-mcp`) reference apps that are not
built yet (`assay.api.app`, the UI bundle, `assay.mcp.server`). This is the target stack.

```yaml
# docker-compose.yml   📋 PLANNED

services:
  assay-api:
    image: assay-engine:0.1
    command: uvicorn assay.api.app:app --host 0.0.0.0 --port 8000
    ports: ["8000:8000"]
    volumes:
      - ./data:/data
      - ./cache:/cache
    environment:
      ASSAY_DATA_PATH: /data
      ASSAY_CACHE_PATH: /cache

  assay-ui:
    image: assay-ui:0.1
    ports: ["3000:80"]
    environment:
      VITE_API_BASE: http://assay-api:8000

  assay-mcp:
    image: assay-engine:0.1
    command: python -m assay.mcp.server --transport sse --port 8001
    ports: ["8001:8001"]
    volumes:
      - ./data:/data
      - ./cache:/cache
```

### 8.4  Package layout

The package lives under `src/assay/` (src-layout, see [`pyproject.toml`](../../pyproject.toml)).
✅ = on disk today, 📋 = planned module.

```
src/assay/
├── __init__.py            ✅ exports AssayConfig, MassiveConfig (SDK facade 📋)
├── config.py              ✅ AssayConfig, MassiveConfig, from_env() + .env loader
├── cli.py                 ✅ data + parse/eval CLI (python -m assay.cli)
├── data/                  ✅ data layer
│   ├── schemas.py         ✅ price_raw / adj_events / universe_snapshots schemas + paths
│   ├── calendar.py        ✅ NYSE trading calendar
│   ├── pipeline.py        ✅ prepare_nasdaq100() orchestrator
│   ├── massive/           ✅ FlatFilesClient (S3) + REST client
│   ├── universe/          ✅ NASDAQ-100 PIT membership (ticker-change history)
│   ├── ingest/            ✅ prices / corporate_actions / universe ingesters
│   └── store/             ✅ DataStore (PIT reads) + adjust.py (forward_adjust)
├── engine/                ✅ factor execution engine
│   ├── parsing.py         ✅ ExprParser, QlibParser, FuncParser, detect_dialect
│   ├── ast.py             ✅ FieldNode / LitNode / OpNode, iter_fields/iter_ops
│   ├── engine.py          ✅ FactorEngine, FactorResult, EvalContext
│   ├── diagnostics.py     ✅ FactorDiagnostics, CATALOG (ASSAY-* codes), lint()
│   └── operators/         ✅ registry + time_series / cross_sectional / math / arithmetic
├── factors/               ✅ alpha101.py (Alpha-101 expression library)
├── evaluator/             ✅ forward_returns, metrics (IC/RankIC), decay, groups, turnover
├── library/               ✅ report (FactorReport/FactorSummary/Lineage), store (FactorLibrary), correlation
├── cache/                 🔶 session (SessionCache/Registry) + l2 (L2FactorCache); L1 Arena/incremental 📋
├── service.py             ✅ AssayService singleton (facade over engine + evaluator + library + cache)
├── api/                   ✅ FastAPI app + auth + models + routers (factor/library/session/system)
│   └── static/            🔶 zero-install vanilla-JS WebUI (index.html, styles.css, js/ + js/pages/) served at /
└── mcp/                   ✅ MCP server (FastMCP; 11 tools; stdio + SSE)

# __init__.py now also exports the SDK facade: init, backtest, batch_backtest, Session, library, stream
# cli.py now also has: run, batch, report, library, serve-api, serve-mcp
# routes/factor.py also exposes POST /v1/factor/lint (data-free AST + diagnostics, for the editor)

assay-ui/                  📋 React/Vite rebuild of the WebUI (documented target — not yet built)
```

---

*— Assay Full-Stack Architecture · AlphaBench Project —*
