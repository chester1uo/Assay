# ASSAY — Engineering Documentation
### A High-Performance Factor Backtesting Engine for Agent-Driven Alpha Mining

**Version:** 0.1 (Draft)  
**Status:** Internal Engineering Reference  
**Data Source:** Massive (US equities, Phase 1)  
**Markets:** US (Phase 1) · HK · A-share (roadmap)  
**Project:** AlphaBench — OpenReview d97Q8r7ZKZ  
**License:** Apache 2.0

---

## Table of Contents

1. [Introduction](#1-introduction)
2. [System Architecture](#2-system-architecture)
3. [Data Layer](#3-data-layer)
4. [Factor Execution Engine](#4-factor-execution-engine)
5. [Cache System](#5-cache-system)
6. [Backtest and Evaluation Layer](#6-backtest-and-evaluation-layer)
7. [User Interface](#7-user-interface)
8. [Performance](#8-performance)
9. [Implementation Roadmap](#9-implementation-roadmap)
10. [Appendix](#10-appendix)

---

## 1. Introduction

> Assay is a point-in-time correct, agent-native factor backtesting engine designed to serve as the high-throughput evaluation backbone for LLM agent systems that autonomously generate, evaluate, and refine quantitative alpha factors at scale.

### 1.1  Motivation

Traditional backtesting frameworks—qlib, Zipline, VectorBT—were architected for human-in-the-loop research: a researcher manually crafts a small number of factor hypotheses and carefully validates each one. The evaluation step is a terminal operation performed once per factor.

LLM agent-driven alpha mining inverts this model. An agent generates hundreds of candidate factors per session, requiring the evaluation engine to function as a low-latency feedback signal within a tight generation-evaluation-refinement loop. This fundamentally changes the system requirements:

| Dimension | Traditional workflow | Agent mining workflow |
|---|---|---|
| Factors per session | 1–10 | 50–500 |
| Evaluation role | Terminal validation | Iterative feedback signal |
| Latency tolerance | Minutes to hours | Sub-second |
| Data correctness | Checked manually | Guaranteed by system |
| Factor diversity | Human-managed | Automatically tracked |
| Batch parallelism | Not required | Core requirement |

### 1.2  Design Goals

- Sub-500ms end-to-end latency for single-factor evaluation (cold path)
- 100-factor batch evaluation in under 60 seconds via DAG-aware parallel execution
- Point-in-time correctness enforced at the data layer, not as a user responsibility
- Structured `FactorReport` output designed for direct LLM agent consumption
- Multi-market support: US (Phase 1 via Massive), HK and A-share (roadmap)
- Dual syntax: qlib expression strings and Python functions share one execution backend

### 1.3  Scope of This Document

This document covers the engineering design of Assay at the component level. It is intended as the authoritative internal reference for contributors and the technical foundation for academic publication. The following areas are addressed:

1. System architecture and layer boundaries
2. Data layer design (Massive integration, PIT model, corporate actions)
3. Factor execution engine (parser, AST, operator registry)
4. Cache system (L1 operator cache, L2 factor string cache, incremental maintenance)
5. Backtest and evaluation layer (IC, RankIC, decay, batch evaluation)
6. User interface (Python SDK, CLI, FactorReport schema)
7. Performance model and benchmarks

---

## 2. System Architecture

### 2.1  Layer Overview

Assay is decomposed into five fully decoupled layers. Each layer exposes a well-defined interface and can be used independently. Dependencies flow strictly downward; no lower layer has knowledge of the layer above it.

| Layer | Module(s) | Primary responsibility |
|---|---|---|
| Data | `DataStore`, `AdjFactorStore`, `UniverseStore`, `EventScheduler` | PIT-correct market data, corporate action snapshots, universe membership history |
| Engine | `FactorEngine`, `OperatorRegistry`, `ExprParser` | Expression parsing, AST compilation, operator execution, two-level cache |
| Backtest | `SingleFactorBT`, `BatchFactorBT`, `ForwardReturns`, `CostModel` | IC/RankIC/decay evaluation, parallel batch scheduling, transaction cost modeling |
| Analysis | `ICAnalyzer`, `DecayAnalyzer`, `LineageStore`, `CorrelationAnalyzer` | Factor diagnostics, redundancy detection, lineage and reproducibility |
| Interface | Python SDK, CLI, AgentAPI, FactorReport | User-facing API, structured output for LLM agent consumption |

### 2.2  Data Flow

A single factor evaluation request traverses the following path:

```
User / Agent
    │  factor_expr: str
    ▼
ExprParser          ← parses qlib or Python syntax into unified AST
    │  AST: OpNode tree
    ▼
FactorEngine         ← checks L2 factor string cache
    │  cache miss → walk AST, check L1 per-operator cache
    ▼
OperatorRegistry     ← executes uncached nodes via Polars / Numba
    │  raw factor DataFrame (T × N)
    ▼
BatchFactorBT        ← aligns factor with ForwardReturns
    │  aligned (factor, returns) pair
    ▼
ICAnalyzer           ← computes IC, RankIC, ICIR, decay (Numba parallel)
    │
    ▼
FactorReport         ← assembles structured JSON result
    │  FactorReport JSON
    ▼
User / Agent
```

### 2.3  Key Design Decisions

#### Strict PIT enforcement
All `DataStore` queries require an explicit `as_of_date` parameter. The store raises an error if called without one, making look-ahead bias a compile-time rather than runtime concern. Adjustment factors and universe membership are stored with both `event_time` and `knowledge_time` columns (bi-temporal model).

#### Immutable append-only writes
No historical record is ever modified. All data revisions (e.g. a restated earnings figure, a corrected corporate action) are appended as new rows with a new `knowledge_time`. This guarantees that any historical backtest can be reproduced exactly by querying `as_of` the original run date.

#### Separation of scheduling from computation
The DAG execution planner (scheduling) is fully separated from the operator compute kernels (execution). This allows the scheduler to be optimized independently—changing the parallelism strategy or materialization policy does not require changes to operator implementations.

#### Agent feedback as a first-class output
The `FactorReport` is not a dashboard artifact—it is a machine-readable protocol designed for direct agent consumption. Every field is chosen to provide actionable signal for the next generation step.

---

## 3. Data Layer

> The data layer is the correctness foundation of the entire system. Performance optimizations in higher layers are only valid if the data they operate on is point-in-time correct. Getting this layer right is the single most important engineering task in the project.

### 3.1  Massive Integration (US Equities)

Phase 1 uses Massive as the primary data provider for US equities. Massive provides daily OHLCV, adjustment factors, corporate action events, and index constituent history. The integration layer normalizes Massive's delivery format into Assay's internal storage schema.

#### Ingestion pipeline

```
MassiveConnector
    ├── fetch_ohlcv(symbols, start, end)      → raw price DataFrame
    ├── fetch_adj_events(symbols, start, end) → corporate action log
    ├── fetch_universe(index, date)            → constituent snapshot
    └── fetch_financials(symbols, as_of)      → PIT financial data

DataIngester
    ├── validate_schema()    ← reject malformed rows, log to quarantine
    ├── deduplicate()        ← exact duplicate rows
    ├── normalize_timezone() ← all timestamps to NYSE calendar
    └── write_parquet()      ← append to DataStore partition
```

#### Field mapping

| Massive field | Assay internal name | Notes |
|---|---|---|
| `adj_close` | `close_adj` | Massive provides split + dividend adjusted |
| `close` | `close_raw` | Unadjusted closing price |
| `volume` | `volume` | Shares traded |
| `adj_factor` | `adj_factor` | Cumulative adjustment factor, stored with `as_of_date` |
| `div_amount` | `dividend_raw` | Pre-split dividend amount, must be ratio-adjusted |
| `split_ratio` | `split_ratio` | Forward split ratio (e.g. 2.0 = 1:2 split) |

### 3.2  Storage Schema

#### DataStore (`price_raw`)

```
Parquet partition layout:
  data/
  └── market=US/
      └── year=2024/
          └── month=12/
              └── price_raw.parquet

Schema: price_raw
  date          DATE          -- trading date (event_time)
  symbol        STRING        -- ticker (e.g. AAPL)
  open          FLOAT32
  high          FLOAT32
  low           FLOAT32
  close         FLOAT32       -- unadjusted
  volume        FLOAT32
  as_of_date    DATE          -- knowledge_time (when row was ingested)
  source_id     STRING        -- Massive batch ID for audit trail
```

#### AdjFactorStore (`adj_events`)

```
Schema: adj_events
  symbol        STRING
  ex_date       DATE          -- event_time: date the action takes effect
  as_of_date    DATE          -- knowledge_time: when Assay learned of this event
  event_type    ENUM          -- SPLIT | DIVIDEND | MERGER | SPINOFF
  adj_factor    FLOAT32       -- cumulative factor on this date
  split_ratio   FLOAT32       -- 1.0 if not a split event
  dividend_adj  FLOAT32       -- 0.0 if not a dividend event

Query pattern (PIT-correct):
  SELECT adj_factor
  FROM adj_events
  WHERE symbol = :sym
    AND ex_date <= :backtest_date
    AND as_of_date <= :backtest_date   -- only use info known at backtest_date
  ORDER BY ex_date DESC
  LIMIT 1
```

#### UniverseStore (`universe_snapshots`)

```
Schema: universe_snapshots
  index_id       STRING        -- e.g. SP500, NASDAQ100
  effective_date DATE          -- date this composition became active
  symbols        LIST<STRING>  -- constituent tickers on that date
  as_of_date     DATE          -- when Assay received this snapshot

Usage: universe for backtest as-of 2022-06-15
  SELECT symbols FROM universe_snapshots
  WHERE index_id = 'SP500'
    AND effective_date <= '2022-06-15'
    AND as_of_date <= '2022-06-15'
  ORDER BY effective_date DESC LIMIT 1
```

### 3.3  Corporate Action Handling

Incorrect corporate action handling is the most common source of look-ahead bias in production systems. Assay enforces two invariants:

- **Invariant 1:** Adjustment factors applied to a backtest as-of date `t` must have `as_of_date ≤ t`. A split occurring after `t` must not affect the adjusted prices used in the backtest.
- **Invariant 2:** Pre-split dividend amounts must be ratio-adjusted before being used to compute adjusted prices. Using the raw dividend amount against a split-adjusted price over-adjusts historical values.

#### Adjustment calculation

```python
def get_adj_price(symbol: str, date_range: tuple,
                  as_of_date: str) -> pd.DataFrame:
    """
    Returns PIT-correct forward-adjusted prices.
    Only uses corporate actions known as_of as_of_date.
    """
    raw = store.get_raw_prices(symbol, *date_range)

    # Fetch only events known at backtest time
    events = adj_store.query(
        symbol=symbol,
        ex_date_lte=date_range[1],
        as_of_date_lte=as_of_date   # <-- PIT constraint
    )

    # Cumulative forward-adjustment factor
    ref_factor = events.iloc[-1]['adj_factor']
    raw['adj_factor'] = raw['date'].map(
        lambda d: events[events['ex_date'] <= d]['adj_factor'].iloc[-1]
        if len(events[events['ex_date'] <= d]) > 0 else 1.0
    )
    raw['close_adj'] = raw['close'] * raw['adj_factor'] / ref_factor
    return raw
```

### 3.4  Point-in-Time Query Interface

All `DataStore` methods enforce PIT correctness through their interface design. The `as_of_date` parameter is mandatory and has no default value.

```python
class DataStore:
    def get_panel(
        self,
        fields:     list[str],
        symbols:    list[str],
        start_date: str,
        end_date:   str,
        as_of_date: str,            # REQUIRED — no default
        adj:        str = 'forward' # forward | backward | none
    ) -> pl.DataFrame: ...

    def get_universe(
        self,
        index_id:   str,
        date:       str,
        as_of_date: str             # REQUIRED
    ) -> list[str]: ...

    def get_forward_returns(
        self,
        symbols:    list[str],
        start_date: str,
        end_date:   str,
        horizons:   list[int],      # [1, 5, 10, 20]
        as_of_date: str,
        execution:  str = 'next_open'  # next_open | next_close | vwap
    ) -> dict[int, pl.DataFrame]: ...
```

### 3.5  EventScheduler

The `EventScheduler` automates data ingestion by mapping market events to ingestion tasks.

| Event type | Trigger | Tasks dispatched |
|---|---|---|
| `market_close` | NYSE 4:00 PM ET (US Phase 1) | `ingest_ohlcv`, `update_adj_factor`, `refresh_universe` |
| `earnings_release` | SEC filing timestamp | `ingest_financials`, `update_pit_snapshot` |
| `index_rebalance` | Index provider announcement | `update_universe_snapshot` |
| `dividend_ex_date` | NYSE ex-dividend date | `update_adj_factor_snapshot`, `invalidate_l2_cache` |
| `split_effective` | Split effective date | `update_adj_factor_snapshot`, `invalidate_l2_cache` |

---

## 4. Factor Execution Engine

### 4.1  Dual-Syntax Parser

Assay accepts factor expressions in two syntaxes. Both are parsed into an identical intermediate representation (unified AST) before execution. The execution backend has no knowledge of which syntax produced the AST.

#### Syntax A — qlib expression strings

```
Ref($close, 5) / $close - 1
Corr($close, $volume, 20) - Mean(Ref($close, 1), 10)
Rank(EMA($close, 12) - EMA($close, 26))
```

#### Syntax B — Python function calls

```
ts_returns(close, 5)
ts_corr(close, volume, 20) - ts_mean(ts_delay(close, 1), 10)
cs_rank(ts_ema(close, 12) - ts_ema(close, 26))
```

#### Unified AST node types

```python
@dataclass(frozen=True)
class FieldNode:             # leaf: raw data field
    name: str                # 'close', 'volume', 'open', ...

@dataclass(frozen=True)
class LitNode:               # leaf: literal value
    value: float | int | str # 20, 0.5, 'sector'

@dataclass(frozen=True)
class OpNode:                # internal: operator application
    op:       str            # 'ts_mean', 'cs_rank', ...
    children: tuple          # tuple of child node hashes (after interning)

    def struct_hash(self) -> str:
        raw = f"op:{self.op}({','.join(self.children)})"
        return hashlib.sha256(raw.encode()).hexdigest()[:12]
```

#### Parser auto-detection

```python
class ExprParser:
    def parse(self, expr: str) -> OpNode:
        if re.search(r'\$\w+', expr) or self._has_qlib_ops(expr):
            return QlibParser().parse(expr)
        return PythonParser().parse(expr)

    def _has_qlib_ops(self, expr: str) -> bool:
        qlib_ops = {'Ref','Mean','Std','Corr','EMA','Rank','Delta','Resi'}
        return bool(re.search(r'\b(' + '|'.join(qlib_ops) + r')\b', expr))
```

### 4.2  Operator Registry

All operators are registered in a central `OperatorRegistry`. The registry provides both the execution implementation and a machine-readable schema used for LLM agent prompt injection.

#### Operator categories

| Category | Prefix | Examples | Complexity |
|---|---|---|---|
| Time-series | `ts_` | `ts_mean`, `ts_std`, `ts_corr`, `ts_rank`, `ts_decay_linear` | O(T × N × d) |
| Cross-sectional | `cs_` | `cs_rank`, `cs_zscore`, `cs_neutralize`, `cs_group_rank` | O(T × N log N) |
| Mathematical | (none) | `log`, `sign`, `abs`, `pow`, `sqrt`, `clip`, `where`, `safe_div` | O(T × N) |
| Composite | `calc_` | `calc_vwap`, `calc_adv`, `calc_returns` | O(T × N) |

#### Operator schema (machine-readable)

```python
OPERATOR_SCHEMA = {
  'ts_corr': {
    'signature':    'ts_corr(x, y, d)',
    'params':       {'d': {'type': 'int', 'min': 2, 'max': 250}},
    'inputs':       {'x': 'price_or_factor', 'y': 'price_or_factor'},
    'output_range': '[-1, 1]',
    'incremental':  True,       # supports O(1) daily update
    'lookahead_safe': True,
    'common_errors': [
        'd=1 produces all-NaN output (zero variance)',
        'x and y must share the same (date, symbol) index',
    ],
    'example': 'ts_corr(close, volume, 20)',
  },
  # ... all operators follow this schema
}
```

### 4.3  DAG Construction and CSE

When F factor expressions are submitted for batch evaluation, the engine merges their ASTs into a single shared DAG using structural hashing. Nodes with identical structure (same operator, same children, same parameters) are merged into a single node computed once and referenced many times.

```python
class DAGBuilder:
    def build(self, exprs: list[str]) -> SharedDAG:
        asts = [ExprParser().parse(e) for e in exprs]
        dag_nodes: dict[str, OpNode] = {}
        factor_roots: list[str] = []

        for ast in asts:
            root_hash = self._intern(ast, dag_nodes)
            factor_roots.append(root_hash)

        return SharedDAG(dag_nodes, factor_roots)

    def _intern(self, node, dag: dict) -> str:
        if isinstance(node, (FieldNode, LitNode)):
            h = node.struct_hash()
            dag.setdefault(h, node)
            return h
        # Recurse first (post-order)
        child_hashes = tuple(
            self._intern(c, dag) for c in node.children
        )
        interned = OpNode(op=node.op, children=child_hashes)
        h = interned.struct_hash()
        dag.setdefault(h, interned)
        return h
```

#### CSE impact (K=6 avg depth, R=0.6 overlap rate)

| Batch size (F) | Naive nodes (F×K) | After CSE | Reduction |
|---|---|---|---|
| 10 | 60 | 24 | 60% |
| 50 | 300 | 120 | 60% |
| 100 | 600 | 240 | 60% |
| 200 | 1,200 | 480 | 60% |
| 500 | 3,000 | 1,200 | 60% |

### 4.4  Execution Planning

```python
class ExecutionPlanner:
    def plan(self, dag: SharedDAG) -> ExecutionPlan:
        # Step 1: Kahn topological layering
        layers = self._kahn_layers(dag)

        # Step 2: Within each layer, sort by priority:
        #   (1) high fan-out first (many dependents)
        #   (2) expensive ops first (critical path)
        #   (3) small memory footprint first (peak memory control)
        layers = self._prioritize(layers, dag)

        # Step 3: Check memory budget; split layers if needed
        stages = self._memory_constrained_stages(layers)

        # Step 4: Compute critical path length
        critical_ms = self._critical_path(dag)

        return ExecutionPlan(stages, critical_ms)
```

The **critical path** is the longest chain of sequentially-dependent nodes in the DAG. It defines the theoretical lower bound on execution time regardless of parallelism. For typical factor expressions with depth K=6, the critical path is approximately **44ms** (N=1000, T=250).

### 4.5  Parallel Execution

```python
class ParallelExecutor:
    def execute(self, plan: ExecutionPlan,
                dag: SharedDAG,
                data: dict) -> dict[str, np.ndarray]:
        results = {}
        for stage in plan.stages:
            if len(stage) == 1:
                results[stage[0]] = self._run_node(stage[0], dag, data, results)
            else:
                # ThreadPoolExecutor: numpy releases GIL during computation
                # No serialization cost vs ProcessPoolExecutor
                with ThreadPoolExecutor(self.n_workers) as ex:
                    futs = {ex.submit(self._run_node, h, dag, data, results): h
                            for h in stage}
                    for fut in as_completed(futs):
                        results[futs[fut]] = fut.result()
            # GC: release intermediate results no longer needed
            self._gc(stage, plan, results)
        return results
```

> **Note:** Thread-based (not process-based) parallelism is used for intra-batch execution. Because Polars and Numba operations release the Python GIL during computation, threads achieve true parallelism without inter-process communication overhead. For large batch jobs (F > 200), the outer loop across factor batches uses `ProcessPoolExecutor`.

---

## 5. Cache System

The cache system is Assay's primary performance mechanism. It operates at two levels and exploits the structural properties of append-only temporal panel data to maintain cached results with O(1) daily updates.

### 5.1  Architecture Overview

```
┌─────────────────────────────────────────────────────────────┐
│  L2 Factor String Cache (disk — Parquet, ~1MB/factor)       │
│  key: hash(expr + universe + date_range + adj_version)      │
│  stores: complete (T × N) factor result matrix              │
│  invalidation: on adj_factor version bump                   │
├─────────────────────────────────────────────────────────────┤
│  L1 Operator Cache (RAM — Arena blocks, float32)            │
│  key: (op, field, window, market, universe, date_range)     │
│  stores: intermediate operator results, e.g. ts_mean(c,20)  │
│  eviction: LFU with EMA heat scoring                        │
├─────────────────────────────────────────────────────────────┤
│  Incremental Maintenance Layer                              │
│  daily append: O(1) update for linear sliding window ops    │
│  topo scheduler: update dependents in correct order         │
└─────────────────────────────────────────────────────────────┘
```

### 5.2  L1 Operator Cache

#### Key design

L1 cache keys are frozen dataclasses. Python caches the `__hash__` result after the first call, making repeated dict lookups O(1) with no serialization overhead.

```python
@dataclass(frozen=True)
class OpKey:
    op:       str
    field:    str
    window:   int
    market:   str
    univ_id:  str
    t0:       str
    t1:       str
    field2:   str = ''  # second input for binary ops (ts_corr)

# frozen=True auto-generates __hash__ and __eq__
# Python caches hash value after first computation
# dict[OpKey, CachedArray] lookups run at C speed
```

#### Arena memory layout

Cached arrays are stored in pre-allocated Arena blocks rather than individual heap allocations. This eliminates memory fragmentation and improves cache locality.

```python
class ArenaBlock:
    def __init__(self, n_dates, n_symbols, n_slots=256):
        # Single contiguous allocation: (slots, dates, symbols)
        # C-order: slot dimension outermost for sequential slot access
        self._buf = np.empty(
            (n_slots, n_dates, n_symbols),
            dtype=np.float32,   # float32: half bandwidth vs float64
            order='C'
        )
        self._slot_map: dict[OpKey, int] = {}
        self._heat:     dict[int, float]  = {}

    def get(self, key: OpKey) -> np.ndarray | None:
        slot = self._slot_map.get(key)
        if slot is None: return None
        self._heat[slot] = self._heat[slot] * 0.9 + 1.0  # EMA heat
        return self._buf[slot]   # zero-copy view

    def put(self, key: OpKey, arr: np.ndarray):
        if self._next_slot >= len(self._buf):
            self._evict_lfu()
        slot = self._next_slot
        self._buf[slot] = arr    # direct write into pre-allocated block
        self._slot_map[key] = slot
        self._heat[slot] = 1.0
        self._next_slot += 1
```

### 5.3  Incremental O(1) Daily Update

For operators whose results satisfy a sliding window recurrence relation, appending one day of new data requires O(1) work per symbol rather than O(d) recomputation over the full window.

| Operator | Incremental? | Update formula | State maintained |
|---|---|---|---|
| `ts_mean` | Yes | `new = old + (x_new - x_expired) / d` | `sum_x` |
| `ts_sum` | Yes | `new = old + x_new - x_expired` | `sum_x` |
| `ts_std` | Yes | Welford sliding window | `sum_x`, `sum_x2` |
| `ts_corr` | Yes | Update 5 sufficient statistics | `sx, sy, sx2, sy2, sxy` |
| `ts_ema` | Yes | `new = alpha * x_new + (1-alpha) * old` | `last_ema` |
| `ts_rank` | **No** | Requires full-window sort O(d log d) | — |
| `ts_argmax` | **No** | Requires full-window scan O(d) | — |
| `cs_rank` | **No** | Cross-sectional, no time-state | — |

#### `ts_std` incremental implementation

```python
@numba.jit(nopython=True, fastmath=True)
def _increment_ts_std(
    sum_x:  np.ndarray,   # shape (N,)
    sum_x2: np.ndarray,   # shape (N,)
    x_new:  np.ndarray,   # today's values
    x_exp:  np.ndarray,   # expiring values (d days ago)
    window: int
) -> tuple:
    new_sum_x  = sum_x  + x_new - x_exp
    new_sum_x2 = sum_x2 + x_new**2 - x_exp**2
    variance = (new_sum_x2 - new_sum_x**2 / window) / (window - 1)
    std = np.sqrt(np.maximum(variance, 0.0))  # clip numerical errors
    return new_sum_x, new_sum_x2, std
```

### 5.4  L2 Factor String Cache

L2 caches complete factor expression results to disk. It is used across sessions: if an agent submits an expression evaluated in a previous session on the same universe and date range, the result is served from disk without recomputation.

```python
@dataclass(frozen=True)
class FactorCacheKey:
    expr_hash:   str   # SHA-256[:16] of canonicalized expression
    universe_id: str
    market:      str
    date_start:  str
    date_end:    str
    adj_version: str   # bumped when adj_factor snapshot changes
                       # → automatic cache invalidation on splits/divs

# Storage:  ~/.assay/factor_cache/<hash[:2]>/<hash>.parquet
# Compression: zstd
# Index: in-memory metadata dict for O(1) existence check
# Size:  ~1MB per factor for N=1000, T=250, float32
```

### 5.5  Cache Invalidation Policy

| Event | L1 effect | L2 effect |
|---|---|---|
| New trading day appended | Incremental update (O(1) ops) | No invalidation |
| Dividend ex-date | Invalidate affected symbol's price ops | Invalidate by `adj_version` bump |
| Stock split | Invalidate all price-derived ops for symbol | Invalidate by `adj_version` bump |
| Universe rebalance | No effect (keyed by `universe_id`) | Invalidate by `universe_id` update |
| Data correction / restatement | Invalidate affected date range | Full invalidation for symbol |

---

## 6. Backtest and Evaluation Layer

### 6.1  Forward Returns

Forward returns are precomputed once per evaluation session and reused across all factor evaluations. This eliminates redundant return computation from the per-factor hot path.

```python
class ForwardReturns:
    def precompute(
        self,
        symbols:    list[str],
        date_range: tuple[str, str],
        horizons:   list[int] = [1, 5, 10, 20],
        execution:  str = 'next_open',  # price used for entry/exit
        as_of_date: str = None
    ) -> dict[int, pl.DataFrame]:
        """
        Returns dict mapping horizon → (T, N) returns DataFrame.
        Aligned to the same (date, symbol) index as factor outputs.
        Session-level cache: reused for all factors in the session.
        """
```

#### Execution price conventions

| Convention | Description | Recommended for |
|---|---|---|
| `next_open` | Entry at T+1 open price | Most factor strategies (avoids closing price impact) |
| `next_close` | Entry at T+1 close price | MOC orders, index-tracking strategies |
| `vwap` | T+1 VWAP (requires intraday data) | Larger AUM, impact-sensitive strategies |

### 6.2  IC and RankIC Computation

Information Coefficient (IC) measures cross-sectional linear correlation between factor values and subsequent returns. Rank IC (RankIC) measures Spearman rank correlation, which is more robust to outliers and distribution assumptions.

#### Mathematical definition

```
For each date t in [t_start, t_end]:

  IC_t     = Pearson(factor_vals[t], fwd_returns[t])
           = Cov(f, r) / (Std(f) × Std(r))

  RankIC_t = Pearson(rank(factor_vals[t]), rank(fwd_returns[t]))
           = Spearman(factor_vals[t], fwd_returns[t])

  IC       = mean(IC_t for t in T)
  ICIR     = mean(IC_t) / std(IC_t)
  RankIC   = mean(RankIC_t for t in T)
  RankICIR = mean(RankIC_t) / std(RankIC_t)
```

#### High-performance RankIC via Numba

The production implementation uses `numba.prange` for data-parallel execution across all T dates simultaneously. Theoretical speedup vs `scipy.spearmanr` loop: ~200-275x for N=1000, T=250.

```python
@numba.jit(nopython=True, parallel=True, fastmath=True, cache=True)
def rank_ic_parallel(
    factor:  np.ndarray,   # (T, N) float32
    returns: np.ndarray,   # (T, N) float32
) -> tuple:
    T, N = factor.shape
    ic_series = np.empty(T, dtype=np.float32)

    for t in numba.prange(T):    # parallel: T dates across all cores
        f_rank = _rank_1d(factor[t])
        r_rank = _rank_1d(returns[t])
        ic_series[t] = _pearson_1d(f_rank, r_rank)

    return ic_series.mean(), ic_series.std(), ic_series


@numba.jit(nopython=True, fastmath=True)
def _rank_1d(x: np.ndarray) -> np.ndarray:
    # Average-rank tie handling, O(N log N)
    idx = np.argsort(x)
    rank = np.empty(len(x), dtype=np.float32)
    i = 0
    while i < len(x):
        j = i + 1
        while j < len(x) and x[idx[j]] == x[idx[i]]: j += 1
        avg = (i + j - 1) / 2.0
        for k in range(i, j): rank[idx[k]] = avg
        i = j
    return rank
```

#### Multi-horizon fusion

Factor rank is computed once and reused across all H horizons, saving (H-1)/H of the factor ranking work. For H=4 horizons, this saves **75%** of factor sort operations.

```python
@numba.jit(nopython=True, parallel=True, fastmath=True)
def rank_ic_multi_horizon(
    factor:      np.ndarray,  # (T, N)
    fwd_returns: np.ndarray,  # (H, T, N)
) -> np.ndarray:              # (H, T)
    H, T, N = fwd_returns.shape
    result = np.empty((H, T), dtype=np.float32)
    for t in numba.prange(T):
        f_rank = _rank_1d(factor[t])  # computed once per date
        for h in range(H):
            r_rank = _rank_1d(fwd_returns[h, t])
            result[h, t] = _pearson_1d(f_rank, r_rank)
    return result
```

### 6.3  Decay Analysis

Factor decay measures how quickly the predictive signal attenuates over longer holding periods.

```python
class DecayAnalyzer:
    def compute(
        self,
        factor_vals: np.ndarray,
        fwd_returns: dict[int, np.ndarray],  # horizon → (T, N)
    ) -> DecayResult:
        horizons = sorted(fwd_returns.keys())
        ic_by_horizon = {
            h: rank_ic_parallel(factor_vals, fwd_returns[h])[0]
            for h in horizons
        }
        halflife = self._estimate_halflife(horizons, ic_by_horizon)
        return DecayResult(ic_by_horizon, halflife)

    def _estimate_halflife(self, horizons, ic_vals) -> float:
        # Fit: IC(h) = IC(1) * exp(-lambda * h)
        # halflife = log(2) / lambda
        ...
```

### 6.4  Batch Evaluation

```python
class BatchFactorBT:
    def run(
        self,
        exprs:     list[str],
        universe:  str,
        period:    tuple[str, str],
        as_of:     str,
        horizons:  list[int] = [1, 5, 10, 20],
        n_workers: int = 8,
    ) -> list[FactorReport]:

        # 1. Precompute forward returns (once for all factors)
        fwd = ForwardReturns().precompute(
            symbols=store.get_universe(universe, period[1], as_of),
            date_range=period, horizons=horizons, as_of_date=as_of
        )

        # 2. Build shared DAG (CSE across all F factors)
        dag = DAGBuilder().build(exprs)

        # 3. Schedule and execute
        plan    = ExecutionPlanner(dag, n_workers=n_workers).plan()
        results = ParallelExecutor(plan, dag, data).execute()

        # 4. Compute IC for each factor root
        reports = []
        for i, root_h in enumerate(dag.factor_roots):
            factor_vals = results[root_h]
            report = self._evaluate(exprs[i], factor_vals, fwd)
            reports.append(report)

        return sorted(reports, key=lambda r: -r.rank_icir)
```

### 6.5  Cost Model

| Parameter | US (Phase 1) | HK (roadmap) | A-share (roadmap) |
|---|---|---|---|
| Commission | 0.05% | 0.03% | 0.03% |
| Stamp duty | 0% | 0.13% | 0.10% (sell only) |
| Market impact k | 0.10 | 0.15 | 0.20 |
| Price limit | None | None | ±10% daily |
| Settlement | T+2 | T+2 | T+1 |

*Market impact model: `impact = k × sqrt(order_size / adv20)` where `adv20` is 20-day average daily volume.*

---

## 7. User Interface

### 7.1  Python SDK

#### Single factor evaluation

```python
import assay

# Minimal usage
report = assay.backtest(
    expr     = 'ts_returns(close, 20)',
    universe = 'SP500',
    period   = ('2020-01-01', '2024-12-31'),
)

print(report.rank_ic)        # 0.047
print(report.rank_icir)      # 0.61
print(report.decay_halflife) # 12 days

# Full options
report = assay.backtest(
    expr       = 'ts_corr(close, volume, 20)',
    universe   = 'SP500',
    period     = ('2018-01-01', '2024-12-31'),
    horizons   = [1, 5, 10, 20],
    execution  = 'next_open',
    market     = 'US',
    neutralize = ['sector'],   # industry-neutral IC
)
```

#### Batch evaluation

```python
factors = [
    'ts_returns(close, 20)',
    'ts_corr(close, volume, 20)',
    'cs_rank(ts_std(log(close/ts_delay(close,1)), 20))',
    # ... up to hundreds
]

reports = assay.batch_backtest(
    exprs    = factors,
    universe = 'SP500',
    period   = ('2020-01-01', '2024-12-31'),
    n_jobs   = 8,
    sort_by  = 'rank_icir',
)

for r in reports[:5]:
    print(f'{r.expr:<50}  RankIC={r.rank_ic:.3f}  ICIR={r.rank_icir:.2f}')
```

#### Session context (amortize setup costs)

```python
# Panel loaded once: ~265ms
# Each subsequent factor: ~30-50ms (hot path only)
with assay.Session(universe='SP500',
                   period=('2020-01-01', '2024-12-31')) as sess:
    r1 = sess.backtest('ts_returns(close, 20)')
    r2 = sess.backtest('ts_corr(close, volume, 20)')
    r3 = sess.backtest(custom_factor_fn)
```

#### Custom Python factor functions

```python
def my_factor(df: pl.DataFrame) -> pl.Series:
    """df has columns: date, symbol, close, volume, ..."""
    return (df['close'] / df['close'].shift(20) - 1) * df['volume']

report = assay.backtest(
    expr     = my_factor,
    universe = 'SP500',
    period   = ('2020-01-01', '2024-12-31'),
)
```

### 7.2  FactorReport Schema

Every field is chosen to provide actionable information for the next step in the agent loop.

| Field | Type | Description |
|---|---|---|
| `factor_id` | str | SHA-256[:16] of canonical expression |
| `expr` | str | Original expression string |
| `expr_canonical` | str | Normalized form (for deduplication) |
| `ic` | float | Mean IC across evaluation period |
| `icir` | float | IC / std(IC) |
| `rank_ic` | float | Mean RankIC (Spearman) across period |
| `rank_icir` | float | RankIC / std(RankIC) |
| `ic_by_horizon` | dict[int, float] | IC at each holding period: `{1: 0.04, 5: 0.035, ...}` |
| `decay_halflife_days` | int \| null | Estimated signal half-life in trading days |
| `turnover_1d` | float | Average 1-day factor rank autocorrelation |
| `redundancy_score` | float | Max rank-corr with any factor in library [0, 1] |
| `most_similar_factor` | str \| null | `factor_id` of closest library match |
| `lookahead_detected` | bool | True if shift error or global normalization found |
| `failure_mode` | str \| null | `SYNTAX_ERROR` \| `LOOKAHEAD` \| `CONSTANT` \| `ALL_NAN` \| `RUNTIME_ERROR` (from the diagnostics system — see `assay.engine.diagnostics`) |
| `suggestion` | str \| null | Natural language improvement hint (the diagnostic's actionable suggestion) |
| `eval_period` | tuple[str, str] | Actual evaluation date range used |
| `universe_id` | str | Universe identifier |
| `n_dates` | int | Number of trading days evaluated |
| `n_symbols` | int | Average universe size |
| `lineage.prompt_hash` | str \| null | Hash of LLM prompt that generated this factor |
| `lineage.data_snapshot_id` | str | DataStore snapshot ID used for evaluation |
| `lineage.eval_timestamp` | str | ISO 8601 timestamp of evaluation |
| `lineage.adj_version` | str | Adjustment factor version at eval time |

### 7.3  Agent API

```python
class AgentAPI:
    def eval_factor(
        self,
        expr:           str,
        context:        AgentContext,
        include_schema: bool = True,  # inject operator docs into response
    ) -> dict:
        """
        Returns FactorReport as dict, with optional operator schema appended.
        Designed to be directly serializable to JSON for agent consumption.
        """
        report = self._bt.run(expr, context)
        result = report.to_dict()
        if include_schema:
            ops_used = extract_ops(expr)
            result['operator_schemas'] = {
                op: OPERATOR_SCHEMA[op]
                for op in ops_used if op in OPERATOR_SCHEMA
            }
        return result
```

### 7.4  CLI

```bash
# Single factor evaluation
assay run 'ts_returns(close, 20)' --universe SP500 --period 2020-2024

# Batch evaluation from file
assay batch factors.txt --universe SP500 --jobs 8 --output results.parquet

# Factor report
assay report momentum_20 --market US --period 2020-2024

# Factor library management
assay library list --sort rank_icir
assay library search 'ts_corr' --min-icir 0.5
assay library prune --redundancy-threshold 0.7

# Cache management
assay cache stats
assay cache clear --market US --before 2023-01-01

# Data management
assay data ingest --market US --date 2024-12-31
assay data status --market US
```

---

## 8. Performance

### 8.1  Benchmark Configuration

| Parameter | Value |
|---|---|
| Universe size (N) | 1,000 symbols |
| Time period (T) | 250 trading days |
| Total rows | 248,179 |
| Horizons | 4 (1, 5, 10, 20 days) |
| Workers | 8 (default `ThreadPoolExecutor`) |

### 8.2  Measured Performance (v0.1, cold run)

| Step | Time (ms) | % of total | Type |
|---|---|---|---|
| DataStore init + load adj events | 32.5 | 4.8% | Setup (once per session) |
| Universe load | 2.2 | 0.3% | Setup |
| DataStore.get_panel (PIT query + adj) | 222.2 | 33.1% | Setup |
| Forward returns precompute (4 horizons) | 42.7 | 6.4% | Setup |
| FactorEngine init (sort + context) | 9.9 | 1.5% | Setup |
| Factor evaluate (expression) | 29.9 | 4.5% | **Hot path** |
| IC / RankIC / ICIR / decay | 331.7 | 49.4% | **Hot path** |
| Redundancy + FactorReport assembly | 0.1 | 0.0% | Hot path |
| **TOTAL (cold)** | **671** | 100% | |

| Metric | Value | Target | Status |
|---|---|---|---|
| Session setup (one-time) | 309 ms | < 500 ms | ✅ PASS |
| Per-factor hot path | 362 ms | < 500 ms | ✅ PASS |
| Batch 50 factors (warm) | 361 ms/factor | < 720 ms/factor | ✅ PASS |
| 100-factor projection | ~36 s | < 60 s | ✅ PASS |

### 8.3  Hot Path Breakdown

> **Key finding:** IC/RankIC computation accounts for **92%** of the per-factor hot path (331.7ms of 362ms). Factor expression evaluation itself is only 8%. The primary optimization target is the IC computation kernel, not the factor engine.

### 8.4  Planned Optimizations

| Optimization | Target | Expected gain | Priority |
|---|---|---|---|
| Numba parallel RankIC (T dates parallel) | IC kernel | ~200x vs scipy loop | P0 |
| Multi-horizon rank fusion (factor rank once) | IC kernel | 75% fewer factor sorts | P0 |
| Session-level panel cache | `get_panel` | 222ms → 0ms (2nd+ factor) | P1 |
| DAG CSE + topological parallel execution | Batch throughput | 10-50x batch speedup | P1 |
| Approximate rank (200-bucket, error < 0.003) | IC kernel | O(N) vs O(N log N) | P2 |

### 8.5  Theoretical Performance Bounds

| Scenario | Memory bandwidth limit | Target (P0+P1) | Current |
|---|---|---|---|
| Single factor (hot path) | 0.12 ms | 30–50 ms | 362 ms |
| 100 factor batch | 6 ms | 5–10 s | ~36 s |
| Daily incremental update | ~1 ms | < 500 ms | full recompute |

*Memory bandwidth limit assumes 50 GB/s RAM, float32, no data reuse. These represent absolute physical lower bounds.*

---

## 9. Implementation Roadmap

| Phase | Scope | Key deliverables | Status |
|---|---|---|---|
| **Phase 1 — MVP** | DataStore (Massive US) + FactorEngine (Python syntax) + SingleFactorBT + IC/RankIC | End-to-end runnable on US equities. Cold-path < 700ms. | DataStore + FactorEngine (parser/AST/operators/evaluator) landed — see [`src/assay/engine/`](../../../src/assay/engine/); SingleFactorBT + IC/RankIC next |
| **Phase 2 — Performance** | Numba IC kernel + session panel cache + L1 Arena + DAG CSE + BatchFactorBT | Hot-path < 50ms. 100 factors < 10s. | Planned |
| **Phase 3 — Agent native** | FactorReport JSON + LineageStore + SandboxExecutor + qlib syntax + redundancy | AlphaBench v2 integration complete. | Planned |
| **Phase 4 — Multi-market** | HK + A-share calendars + cost models + universe snapshots | Cross-market factor research. | Roadmap |
| **Phase 5 — Portfolio** | PortfolioLayer + WeightOptimizer + RiskModel + FactorComposer | Full portfolio backtest capability. | Roadmap |

---

## 10. Appendix

### A.  Operator Quick Reference

| Operator | Signature | Output range | Incremental |
|---|---|---|---|
| `ts_delay` | `ts_delay(x, d)` | same as x | Yes |
| `ts_delta` | `ts_delta(x, d)` | same as x | Yes |
| `ts_returns` | `ts_returns(x, d)` | (-1, ∞) | Yes |
| `ts_log_returns` | `ts_log_returns(x, d)` | (-∞, ∞) | Yes |
| `ts_mean` | `ts_mean(x, d)` | same as x | Yes |
| `ts_sum` | `ts_sum(x, d)` | same as x | Yes |
| `ts_std` | `ts_std(x, d)` | [0, ∞) | Yes |
| `ts_corr` | `ts_corr(x, y, d)` | [-1, 1] | Yes |
| `ts_rank` | `ts_rank(x, d)` | [0, 1] | No |
| `ts_argmax` | `ts_argmax(x, d)` | [0, d-1] | No |
| `ts_argmin` | `ts_argmin(x, d)` | [0, d-1] | No |
| `ts_ema` | `ts_ema(x, d)` | same as x | Yes |
| `ts_decay_linear` | `ts_decay_linear(x, d)` | same as x | Yes |
| `ts_regression` | `ts_regression(y, x, d)` | returns beta, resid | No |
| `ts_skew` | `ts_skew(x, d)` | (-∞, ∞) | No |
| `ts_kurt` | `ts_kurt(x, d)` | (-∞, ∞) | No |
| `cs_rank` | `cs_rank(x)` | [0, 1] | N/A |
| `cs_zscore` | `cs_zscore(x)` | (-∞, ∞) | N/A |
| `cs_demean` | `cs_demean(x)` | (-∞, ∞) | N/A |
| `cs_winsorize` | `cs_winsorize(x, p)` | same as x | N/A |
| `cs_neutralize` | `cs_neutralize(x, group)` | (-∞, ∞) | N/A |
| `cs_group_rank` | `cs_group_rank(x, g)` | [0, 1] | N/A |
| `log` | `log(x)` | (-∞, ∞) | Yes |
| `sign` | `sign(x)` | {-1, 0, 1} | Yes |
| `abs` | `abs(x)` | [0, ∞) | Yes |
| `pow` | `pow(x, e)` | (-∞, ∞) | Yes |
| `clip` | `clip(x, lo, hi)` | [lo, hi] | Yes |
| `where` | `where(cond, a, b)` | same as a/b | Yes |
| `safe_div` | `safe_div(a, b, fill=0)` | (-∞, ∞) | Yes |
| `fillna` | `fillna(x, method)` | same as x | Yes |

### B.  qlib Expression Mapping

| qlib syntax | Assay equivalent |
|---|---|
| `Ref($close, 5)` | `ts_delay(close, 5)` |
| `Mean($close, 20)` | `ts_mean(close, 20)` |
| `Std($close, 20)` | `ts_std(close, 20)` |
| `Corr($close, $volume, 20)` | `ts_corr(close, volume, 20)` |
| `Rank($close)` | `cs_rank(close)` |
| `EMA($close, 12)` | `ts_ema(close, 12)` |
| `Delta($close, 1)` | `ts_delta(close, 1)` |
| `Resi($close, $open, 5)` | `ts_regression(close, open, 5).resid` |
| `Less($close, $open)` | `where(close < open, 1, 0)` |
| `Abs($close - $open)` | `abs(close - open)` |
| `Log($close)` | `log(close)` |
| `Sign($close - $open)` | `sign(close - open)` |

---

*— Assay · AlphaBench Project —*
