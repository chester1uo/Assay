# Assay — 全栈架构设计

**版本：** 0.1 · 草案  
**范围：** 函数式 API · REST API · WebUI · 面向 Agent 的 MCP

> **实现状态（2026-06）。** **整个 Python 后端现已实现并
> 通过测试**（662 项离线测试全部通过）：数据层、因子引擎、IC/RankIC/衰减/分组/
> 换手率**评估器**、**因子库**（持久化 + 相关性 + 冗余度 +
> 剪枝）、**`AssayService`** 单例（evaluate / batch / sessions / streaming）、
> **Python SDK**（`assay.backtest/batch_backtest/Session/library/stream`）、**REST API**
> （FastAPI + SSE）、**MCP 服务器**（8 个 FastMCP 工具，stdio + SSE/HTTP），以及 **CLI**
> （`run`/`batch`/`report`/`library`/`serve-api`/`serve-mcp`）。参见 [`src/assay/`](../../../src/assay/)。
>
> 仍在**规划中**：React **WebUI**（§5，尚无 `assay-ui/`）；以及两项性能
> 优化，后端在缺少它们的情况下仍可正确工作——**L1 Arena / 增量 O(1)
> 缓存**（目前仅有会话缓存 + L2 磁盘缓存）和 **DAG/CSE 批量合并**
> （`batch()` 目前独立评估各因子）。相关章节均已相应标注。
>
> 状态图例——✅ **已实现** · 🔶 **已实现，相较规范有所简化** · 📋 **规划中**
>
> 当前唯一端到端打通的股票池是 **NASDAQ-100**（来自 MASSIVE 的点对点(PIT) 成分股
> 历史）；SP500 / Russell 2000 在路线图中。数据源为 **MASSIVE**
> 美股日聚合数据，提供 OHLCV + 成交笔数，但**没有 `vwap`**。

---

## 目录

1. [架构概览](#1-架构概览)
2. [核心引擎（共享后端）](#2-核心引擎共享后端)
3. [函数式 Python API](#3-函数式-python-api)
4. [REST API](#4-rest-api)
5. [WebUI 前端](#5-webui-前端)
6. [面向 Agent 的 MCP 服务器](#6-面向-agent-的-mcp-服务器)
7. [横切关注点](#7-横切关注点)
8. [部署](#8-部署)

---

## 1. 架构概览

### 1.1  设计理念

Assay 在单一共享引擎之上暴露四个消费界面。每个界面都调用同一套底层计算、缓存和数据层——不存在结果的"API 版本"与"SDK 版本"之分。这意味着：

- 通过 Python SDK、REST API、WebUI 或 MCP agent 调用评估的因子会产生完全相同的 `FactorReport`
- 缓存在所有界面之间共享——一个预热了 L1 算子缓存的 REST 调用会让后续的 SDK 调用受益
- 无论从哪个入口进入，认证、限流和血缘追踪都统一适用

### 1.2  分层图

方框标注了其构建状态：✅ 已实现，🔶 部分实现，📋 规划中。

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

今日已构建：除 React WebUI 外的完整栈——`DataStore`、`FactorEngine` +
双语法解析器 + 算子 + 诊断、IC/衰减/分组/换手率 `Evaluator`、
`FactorLibrary`、`AssayService` 外观类，以及 SDK / REST / MCP / CLI 界面。
缓存是会话 + L2 磁盘缓存（L1 Arena 和增量 O(1) 更新层
仍是一项性能优化），且 `batch()` 独立运行各因子（DAG/CSE 属于未来工作）。

### 1.3  界面对比

延迟目标仍是设计目标（尚无 L1 缓存 / DAG-CSE），但下面除 WebUI 外的
四个代码界面均已实现并通过测试。

| 界面 | 协议 | 认证 | 延迟目标 | 主要用户 | 状态 |
|---|---|---|---|---|---|
| Python SDK | 进程内 | 无（本地） | 热态 < 400ms | 研究员、notebook | ✅ `assay.backtest/batch_backtest/Session/library/stream` |
| REST API | HTTP/SSE | API key（可选） | 热态 < 500ms | 外部应用、CI | ✅ FastAPI + SSE，所有 `/v1/*` 路由 |
| WebUI | HTTP → REST | 同源 | 热态 < 600ms | 人类研究员 | 🔶 零安装 vanilla-JS 应用（`api/static/`，由 FastAPI 提供服务）；React 栈是目标重建方案 |
| MCP 服务器 | JSON-RPC (stdio/SSE) | API key | 热态 < 600ms | LLM agent | ✅ FastMCP，11 个工具，stdio + SSE |

---

## 2. 核心引擎（共享后端）

> **状态：✅ 已实现**（[`src/assay/service.py`](../../../src/assay/service.py)）。
> `AssayService` 单例、会话、流式和库接线均已存在。下方的草图
> 与已发布的 API 高度吻合；实际构造函数会（惰性地）接入 `DataStore`、
> `FactorLibrary`、`L2FactorCache` 和 `SessionRegistry`。`batch()` 通过
> 线程池实现（DAG/CSE 合并仍是未来的优化）。关于绕过 service 的最小化
> 直连引擎路径，参见 §2.1。

所有四个界面都路由经过 `AssayService`，它持有引擎、缓存和数据访问。每个进程中它绝不会被实例化超过一次。

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

### 2.1  今日已有——引擎入口点 ✅

在 `AssayService` 落地之前，引擎是被直接驱动的。`DataStore` 读取一个
点对点面板；`FactorEngine.from_store` 将其透视为对齐的 `(T, N)` 矩阵
并评估一个已解析的表达式。这是真实、可运行的冷路径
（[`src/assay/engine/engine.py`](../../../src/assay/engine/engine.py)、
[`src/assay/data/store/datastore.py`](../../../src/assay/data/store/datastore.py)）：

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

IC/RankIC 评估器、前向收益、缓存和 `FactorReport` 组装**现已
实现**——`AssayService.evaluate()` 返回一个已评分的 `FactorReport`（这里 §2.1 的引擎
路径只是最底层的原语）。批量 DAG/CSE 合并仍是唯一
未构建的性能项；`service.batch()` 可以工作，但独立评估各因子。

---

## 3. 函数式 Python API

> **状态：✅ 已实现**（[`src/assay/__init__.py`](../../../src/assay/__init__.py)）。下方完整的
> SDK——`assay.init`、`assay.backtest`、`assay.batch_backtest`、`assay.Session`、
> `assay.library`、`assay.stream`——均已发布并测试。`assay.init()` 从
> 环境 / 项目 `.env` 读取配置；`backtest()` 若你跳过它会自动初始化。

SDK 是 `AssayService` 之上一个轻薄、符合人体工学的封装。它是延迟最低的界面，因为它进程内运行，没有序列化开销。

### 3.1  安装与初始化 📋

```python
pip install assay-engine          # 📋 not yet published; install from source (§8.1)

import assay

# Initialize once — reads config from env / project .env (see §7.4).
assay.init()                       # 📋 planned; today: AssayConfig.from_env()
```

### 3.2  单因子评估

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

### 3.3  批量评估

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

### 3.4  会话上下文（摊薄设置成本）

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

### 3.5  自定义 Python 因子函数

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

### 3.6  因子库访问

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

### 3.7  流式（异步）

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

> **状态：✅ 已实现**（[`src/assay/api/`](../../../src/assay/api/)）。FastAPI 应用、所有
> `/v1/*` 路由、SSE 流式、可选的 API-key 认证（`ASSAY_API_KEYS`；未设置则开放），
> 以及基于诊断的错误 schema（§4.6）均已发布并测试。以
> `python -m assay.cli serve-api`（或 `uvicorn assay.api.app:app`）运行它。

REST API 是一个 FastAPI 应用。它在 HTTP 之上暴露相同的 `AssayService` 方法。流式评估使用 Server-Sent Events (SSE)。

### 4.1  基础 URL 与版本控制

```
Base URL:  https://api.assay.local/v1
Auth:      X-API-Key header (or Bearer token for WebUI sessions)
Format:    application/json (default), text/event-stream (SSE)
```

### 4.2  评估端点

#### `POST /v1/factor/evaluate`

评估单个因子。同时支持阻塞（返回完整报告）和流式（SSE 事件）两种模式。

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

并行评估一列表因子。全部完成时返回。

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

创建一个预加载数据面板的会话。会话中后续的 evaluate 调用会跳过面板加载（每个因子节省约 265ms）。

```
Request:
  { "universe": "NASDAQ100", "period": ["2020-01-01", "2024-12-31"] }

Response:
  { "session_id": "sess_xyz789", "setup_ms": 309, "expires_at": "2025-01-16T02:00:00Z" }

Usage:
  POST /v1/factor/evaluate  with header  X-Session-Id: sess_xyz789
  → panel load skipped, hot path only (~40ms)
```

### 4.3  库端点

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

### 4.4  系统端点

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

### 4.5  FastAPI 应用结构

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

### 4.6  错误 schema

错误反映了已实现的诊断系统（[`assay.engine.diagnostics`](../../../src/assay/engine/diagnostics.py)）。
每个问题都携带稳定的诊断 `code`（`ASSAY-P###` 解析 / `ASSAY-E###`
执行 / `ASSAY-O###` 输出）、粗粒度的 `failure_mode`、字符 `location`，以及一条
可操作的 `suggestion`：

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

`failure_mode`（FactorReport 级别，来自诊断目录）：`SYNTAX_ERROR` ·
`LOOKAHEAD` · `CONSTANT` · `ALL_NAN` · `RUNTIME_ERROR`。并非由引擎产生的传输/数据
错误——`DATA_NOT_FOUND`、`UNIVERSE_NOT_FOUND`、`SESSION_EXPIRED`——是 📋 规划中的
在这些之上的 service 层补充。

---

## 5. WebUI 前端

> **状态：🔶 一个可运行的 WebUI 已存在——但不在此 React/Vite 栈中。** 由于
> 构建环境无 npm 访问权限，已发布的 UI 是一个**零安装 vanilla-JS 应用**，
> 位于 [`src/assay/api/static/`](../../../src/assay/api/static/)，由 FastAPI 提供服务（`serve-api` →
> `http://localhost:8000`）：三个屏幕、手写的 SVG 图表、一个轻量编辑器
> （非 Monaco），以及 SSE 流式 evaluate，全部基于真实的 `/v1/*` API。下方的 React 18 + Vite
> + Recharts + Monaco 栈仍是**目标重建方案**（📋）；参见配套的
> **Assay WebUI — 详细设计**文档（[webui.md](webui.md)）。

### 5.1  技术栈

| 层 | 技术 | 理由 |
|---|---|---|
| 框架 | React 18 | 用于渐进式图表流式的并发渲染 |
| 构建 | Vite | 快速 HMR、tree-shaking、< 200ms 开发重载 |
| 路由 | React Router v6 | 基于文件的路由、嵌套布局 |
| 状态 | Zustand | 轻量全局状态；无样板代码 |
| 数据获取 | React Query + fetch | SSE 流式支持、缓存、后台重新获取 |
| 图表 | Recharts | React 原生、可组合，足以应对所有 Phase 1 图表类型 |
| 编辑器 | Monaco Editor | VS Code 引擎；自定义 Assay 语言扩展 |
| 样式 | Tailwind CSS | 实用优先、设计令牌、暗色模式 |
| 图标 | Tabler Icons | 一致的描边风格，5800+ 图标 |
| 类型 | TypeScript | 通过 OpenAPI 代码生成实现端到端类型安全 |

### 5.2  项目结构

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

### 5.3  SSE 流式 hook

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

### 5.4  Monaco Assay 语言扩展

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

### 5.5  全局状态

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

## 6. 面向 Agent 的 MCP 服务器

> **状态：✅ 已实现**（[`src/assay/mcp/server.py`](../../../src/assay/mcp/server.py)）。构建于
> 高层 **FastMCP** API（`mcp.server.fastmcp.FastMCP`）之上；下方全部八个工具
> 均注册在 `AssayService` 之上，且 `assay_evaluate` 的描述由
> 实时的 `operator_schema()`（§6.6）丰富。以 `python -m assay.mcp.server`（stdio）或
> `python -m assay.cli serve-mcp --transport sse --port 8001` 运行它。（注意：§6.4 中
> SDK 风格的低层代码片段早于 FastMCP 实现——已发布的基于装饰器的形式请
> 参见源码。）

MCP 服务器将 Assay 的评估能力暴露为 LLM agent 可通过 Model Context Protocol 调用的工具。这是 LLM agent 驱动的 alpha 挖掘的主要接口。

### 6.1  协议

```
Transport:       stdio (default, for Claude Desktop / local agents)
                 SSE over HTTP (for remote agents, e.g. cloud-hosted LLMs)
MCP version:     2024-11-05
Auth (SSE):      X-API-Key header
```

### 6.2  暴露的工具

MCP 服务器暴露了八个工具，覆盖完整的评估-库循环：

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

### 6.3  工具定义

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

### 6.4  MCP 服务器实现

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

### 6.5  MCP 服务器启动

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

### 6.6  算子 schema 注入

MCP 服务器将完整的算子 schema 注入工具描述，让 agent 无需单独调用即可获得内联文档。schema 来源**今日已实现**：`assay.engine.operator_schema()` 返回每个已注册算子（内置*和*用户注册的）的实时 `{name: schema}` 视图，因此只要服务器存在，这种丰富机制便可工作：

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

### 6.7  典型的 agent 循环

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

## 7. 横切关注点

> **状态：** §7.1 认证 ✅ 已实现（可选 API-key，`ASSAY_API_KEYS`；未设置则
> 开放）。§7.2 限流和 §7.3 可观测性为 📋 规划中（尚未接入 slowapi/Prometheus/
> OTel）。§7.4 记录了今日已就位的配置机制。

### 7.1  认证 ✅（可选 API key）

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

### 7.2  限流 📋

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

### 7.3  可观测性 📋

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

### 7.4  配置 ✅（今日：环境变量 + `.env`）

**今日已有。** 配置基于环境变量，由
[`AssayConfig.from_env()`](../../../src/assay/config.py) 加载。在导入时，包还会以
`setdefault` 语义读取项目根目录的 `.env` 文件，因此真实的 shell 环境始终
优先。目前**尚无** `~/.assay/config.toml`。必需和可选变量（参见
[`.env.example`](../../../.env.example)）：

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

**规划中——统一的 `~/.assay/config.toml` 📋。** service 和各界面现已存在，
今日通过 `AssayConfig` 字段 + 环境变量配置；那个将它们纳入一体的
*单一 TOML 文件*仍是规划中的打包形式。`MASSIVE_*` 密钥保留在
环境中；其余部分将迁移到结构化的小节中：

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

## 8. 部署

> **状态：🔶 大部分已实现。** REST API 和 MCP 服务器现可通过
> `python -m assay.cli serve-api` 和 `serve-mcp`（§8.1a）运行。仍未提供：
> 单一的 `assay server` 总控命令、`assay data sync`（请用 `prepare-nasdaq100`）、
> WebUI 镜像，以及 `pip install assay-engine` PyPI 包（请从源码安装）。
> Docker Compose 栈（§8.3）引用了那些尚未发布的镜像。

### 8.1a  今日的本地开发 ✅

已发布的入口点是数据/引擎 CLI（[`src/assay/cli.py`](../../../src/assay/cli.py)），
以 `python -m assay.cli` 调用。目前尚无 `assay` 控制台脚本，PyPI 上也无
`pip install assay-engine` 包——请从源码安装。

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

### 8.1b  规划中的研究员工作流 📋

一旦 SDK/service/各界面落地，预期的一行命令体验是：

```bash
pip install assay-engine            # 📋 not yet published
assay init                          # 📋 creates ~/.assay/config.toml and data dirs
assay data sync --market US --start 2018-01-01    # 📋 unified data CLI
assay server start                  # 📋 REST :8000, WebUI :3000, MCP stdio

python
>>> import assay; assay.init()      # 📋 planned SDK
>>> assay.backtest("ts_returns(close, 20)", universe="NASDAQ100")
```

### 8.2  服务拓扑 📋

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

下面的三个镜像（`assay-api`、`assay-ui`、`assay-mcp`）引用了尚未
构建的应用（`assay.api.app`、UI 包、`assay.mcp.server`）。这是目标栈。

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

### 8.4  包布局

包位于 `src/assay/` 下（src-layout，参见 [`pyproject.toml`](../../../pyproject.toml)）。
✅ = 今日已在磁盘上，📋 = 规划中的模块。

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

*— Assay 全栈架构 · AlphaBench 项目 —*
