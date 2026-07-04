# ASSAY — 工程文档
### 面向 Agent 驱动的 Alpha 挖掘的高性能因子回测引擎

**版本:** 0.1（草稿）  
**状态:** 内部工程参考  
**数据源:** Massive（美股，第一阶段）  
**市场:** 美股（第一阶段） · 港股 · A股（路线图）  
**项目:** AlphaBench — OpenReview d97Q8r7ZKZ  
**许可证:** Apache 2.0

---

## 目录

1. [引言](#1-introduction)
2. [系统架构](#2-system-architecture)
3. [数据层](#3-data-layer)
4. [因子执行引擎](#4-factor-execution-engine)
5. [缓存系统](#5-cache-system)
6. [回测与评估层](#6-backtest-and-evaluation-layer)
7. [用户界面](#7-user-interface)
8. [性能](#8-performance)
9. [实现路线图](#9-implementation-roadmap)
10. [附录](#10-appendix)

---

## 1. 引言

> Assay 是一个点对点（PIT）正确、Agent 原生的因子回测引擎,旨在作为高吞吐的评估中枢,服务于那些能大规模自主生成、评估并优化量化 Alpha 因子的 LLM Agent 系统。

### 1.1  动机

传统回测框架——qlib、Zipline、VectorBT——是为人在回路（human-in-the-loop）的研究流程设计的:研究员手工构造少量因子假设,并对每一个进行仔细验证。评估步骤是每个因子只执行一次的终端操作。

LLM Agent 驱动的 Alpha 挖掘颠覆了这一模式。一个 Agent 在单次会话中生成数百个候选因子,要求评估引擎在紧凑的“生成-评估-优化”回路中充当低延迟的反馈信号。这从根本上改变了系统需求:

| 维度 | 传统工作流 | Agent 挖掘工作流 |
|---|---|---|
| 每次会话的因子数 | 1–10 | 50–500 |
| 评估角色 | 终端验证 | 迭代反馈信号 |
| 延迟容忍度 | 数分钟到数小时 | 亚秒级 |
| 数据正确性 | 人工检查 | 由系统保证 |
| 因子多样性 | 人工管理 | 自动追踪 |
| 批量并行 | 非必需 | 核心需求 |

### 1.2  设计目标

- 单因子评估的端到端延迟低于 500ms(冷路径)
- 通过 DAG 感知的并行执行,在 60 秒内完成 100 个因子的批量评估
- 点对点（PIT）正确性在数据层强制保证,而非用户的责任
- 结构化的 `FactorReport` 输出,专为直接供 LLM Agent 消费而设计
- 多市场支持:美股(第一阶段,经由 Massive)、港股与 A股(路线图)
- 双语法:qlib 表达式字符串与 Python 函数共享同一个执行后端

### 1.3  本文档的范围

本文档在组件层面覆盖 Assay 的工程设计。它旨在作为贡献者的权威内部参考,以及学术发表的技术基础。以下各方面均有涉及:

1. 系统架构与层边界
2. 数据层设计（Massive 集成、PIT 模型、公司行为）
3. 因子执行引擎(解析器、AST、算子注册表)
4. 缓存系统(L1 算子缓存、L2 因子字符串缓存、增量维护)
5. 回测与评估层(IC、RankIC、衰减、批量评估)
6. 用户界面(Python SDK、CLI、FactorReport 模式)
7. 性能模型与基准测试

---

## 2. 系统架构

### 2.1  分层概览

Assay 被拆解为五个完全解耦的层。每一层暴露一个定义良好的接口,并可独立使用。依赖严格向下流动;没有任何下层知晓其上层的存在。

| 层 | 模块 | 主要职责 |
|---|---|---|
| 数据层 | `DataStore`、`AdjFactorStore`、`UniverseStore`、`EventScheduler` | PIT 正确的市场数据、公司行为快照、股票池成员历史 |
| 引擎层 | `FactorEngine`、`OperatorRegistry`、`ExprParser` | 表达式解析、AST 编译、算子执行、两级缓存 |
| 回测层 | `SingleFactorBT`、`BatchFactorBT`、`ForwardReturns`、`CostModel` | IC/RankIC/衰减评估、并行批量调度、交易成本建模 |
| 分析层 | `ICAnalyzer`、`DecayAnalyzer`、`LineageStore`、`CorrelationAnalyzer` | 因子诊断、冗余度检测、血缘与可复现性 |
| 接口层 | Python SDK、CLI、AgentAPI、FactorReport | 面向用户的 API、供 LLM Agent 消费的结构化输出 |

### 2.2  数据流

单次因子评估请求会经过以下路径:

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

### 2.3  关键设计决策

#### 严格的 PIT 强制约束
所有 `DataStore` 查询都要求显式的 `as_of_date` 参数。若未提供该参数,存储会抛出错误,从而使前视偏差(look-ahead bias)成为编译期而非运行期的问题。复权因子与股票池成员同时以 `event_time` 和 `knowledge_time` 两列存储(双时态模型)。

#### 不可变的仅追加写入
任何历史记录都不会被修改。所有数据修订(例如重述的盈利数字、修正后的公司行为)都作为带有新 `knowledge_time` 的新行追加。这保证了任何历史回测都可以通过按原始运行日期 `as_of` 查询而被精确复现。

#### 调度与计算的分离
DAG 执行规划器(调度)与算子计算内核(执行)完全分离。这使得调度器可以独立优化——改变并行策略或物化策略不需要改动算子实现。

#### Agent 反馈作为一等输出
`FactorReport` 不是仪表盘产物——它是一个机器可读的协议,专为直接供 Agent 消费而设计。每个字段的选择都是为了为下一步生成提供可操作的信号。

---

## 3. 数据层

> 数据层是整个系统的正确性基础。只有当上层所操作的数据是点对点(PIT)正确的时,上层的性能优化才是有效的。把这一层做对,是本项目中最重要的单项工程任务。

### 3.1  Massive 集成（美股）

第一阶段使用 Massive 作为美股的主要数据提供方。Massive 提供日频 OHLCV、复权因子、公司行为事件以及指数成分股历史。集成层将 Massive 的交付格式规范化为 Assay 内部的存储模式。

#### 导入流水线

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

#### 字段映射

| Massive 字段 | Assay 内部名称 | 说明 |
|---|---|---|
| `adj_close` | `close_adj` | Massive 提供拆股 + 分红复权后的价格 |
| `close` | `close_raw` | 未复权收盘价 |
| `volume` | `volume` | 成交股数 |
| `adj_factor` | `adj_factor` | 累计复权因子,随 `as_of_date` 存储 |
| `div_amount` | `dividend_raw` | 拆股前的股息金额,必须按比例复权 |
| `split_ratio` | `split_ratio` | 前向拆股比例(例如 2.0 = 1:2 拆股) |

### 3.2  存储模式

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

### 3.3  公司行为处理

不正确的公司行为处理是生产系统中前视偏差最常见的来源。Assay 强制执行两个不变式:

- **不变式 1:** 应用于回测 as-of 日期 `t` 的复权因子必须满足 `as_of_date ≤ t`。发生在 `t` 之后的拆股不得影响回测中使用的复权价格。
- **不变式 2:** 拆股前的股息金额必须先按比例复权,才能用于计算复权价格。用原始股息金额对拆股复权后的价格进行处理会导致历史值被过度复权。

#### 复权计算

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

### 3.4  点对点（PIT）查询接口

所有 `DataStore` 方法都通过其接口设计强制保证 PIT 正确性。`as_of_date` 参数是必填的,且没有默认值。

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

`EventScheduler` 通过将市场事件映射为导入任务来自动化数据导入。

| 事件类型 | 触发条件 | 派发的任务 |
|---|---|---|
| `market_close` | NYSE 美东时间下午 4:00(美股第一阶段) | `ingest_ohlcv`、`update_adj_factor`、`refresh_universe` |
| `earnings_release` | SEC 备案时间戳 | `ingest_financials`、`update_pit_snapshot` |
| `index_rebalance` | 指数提供方公告 | `update_universe_snapshot` |
| `dividend_ex_date` | NYSE 除息日 | `update_adj_factor_snapshot`、`invalidate_l2_cache` |
| `split_effective` | 拆股生效日 | `update_adj_factor_snapshot`、`invalidate_l2_cache` |

---

## 4. 因子执行引擎

### 4.1  双语法解析器

Assay 接受两种语法的因子表达式。两者在执行前都被解析为完全相同的中间表示(统一 AST)。执行后端并不知晓 AST 是由哪种语法产生的。

#### 语法 A — qlib 表达式字符串

```
Ref($close, 5) / $close - 1
Corr($close, $volume, 20) - Mean(Ref($close, 1), 10)
Rank(EMA($close, 12) - EMA($close, 26))
```

#### 语法 B — Python 函数调用

```
ts_returns(close, 5)
ts_corr(close, volume, 20) - ts_mean(ts_delay(close, 1), 10)
cs_rank(ts_ema(close, 12) - ts_ema(close, 26))
```

#### 统一 AST 节点类型

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

#### 解析器自动检测

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

### 4.2  算子注册表

所有算子都注册在一个中央 `OperatorRegistry` 中。注册表既提供执行实现,也提供一个用于 LLM Agent 提示注入的机器可读模式。

#### 算子类别

| 类别 | 前缀 | 示例 | 复杂度 |
|---|---|---|---|
| 时间序列 | `ts_` | `ts_mean`、`ts_std`、`ts_corr`、`ts_rank`、`ts_decay_linear` | O(T × N × d) |
| 截面 | `cs_` | `cs_rank`、`cs_zscore`、`cs_neutralize`、`cs_group_rank` | O(T × N log N) |
| 数学 | (无) | `log`、`sign`、`abs`、`pow`、`sqrt`、`clip`、`where`、`safe_div` | O(T × N) |
| 复合 | `calc_` | `calc_vwap`、`calc_adv`、`calc_returns` | O(T × N) |

#### 算子模式（机器可读）

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

### 4.3  DAG 构建与 CSE

当提交 F 个因子表达式进行批量评估时,引擎使用结构化哈希将它们的 AST 合并为一个共享的 DAG。结构完全相同的节点(相同算子、相同子节点、相同参数)会被合并为单个节点,只计算一次并被多次引用。

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

#### CSE 影响（K=6 平均深度,R=0.6 重叠率）

| 批量大小 (F) | 朴素节点数 (F×K) | CSE 之后 | 缩减 |
|---|---|---|---|
| 10 | 60 | 24 | 60% |
| 50 | 300 | 120 | 60% |
| 100 | 600 | 240 | 60% |
| 200 | 1,200 | 480 | 60% |
| 500 | 3,000 | 1,200 | 60% |

### 4.4  执行规划

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

**关键路径(critical path)** 是 DAG 中最长的一条顺序依赖节点链。它定义了执行时间的理论下界,与并行度无关。对于深度 K=6 的典型因子表达式,关键路径约为 **44ms**(N=1000,T=250)。

### 4.5  并行执行

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

> **注意:** 批内执行采用基于线程(而非基于进程)的并行。由于 Polars 和 Numba 操作在计算期间释放 Python GIL,线程可以在没有跨进程通信开销的情况下实现真正的并行。对于大批量作业(F > 200),跨因子批次的外层循环使用 `ProcessPoolExecutor`。

---

## 5. 缓存系统

缓存系统是 Assay 的主要性能机制。它在两个层级运作,并利用仅追加时态面板数据的结构特性,以 O(1) 的日度更新来维护缓存结果。

### 5.1  架构概览

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

### 5.2  L1 算子缓存

#### 键设计

L1 缓存键是冻结的 dataclass。Python 在首次调用后会缓存 `__hash__` 结果,使得重复的 dict 查找为 O(1) 且无序列化开销。

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

#### Arena 内存布局

缓存数组存储在预分配的 Arena 块中,而非各自独立的堆分配。这消除了内存碎片并改善了缓存局部性。

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

### 5.3  增量 O(1) 日度更新

对于结果满足滑动窗口递推关系的算子,追加一天的新数据只需每个标的 O(1) 的工作量,而非对整个窗口进行 O(d) 的重算。

| 算子 | 增量? | 更新公式 | 维护的状态 |
|---|---|---|---|
| `ts_mean` | 是 | `new = old + (x_new - x_expired) / d` | `sum_x` |
| `ts_sum` | 是 | `new = old + x_new - x_expired` | `sum_x` |
| `ts_std` | 是 | Welford 滑动窗口 | `sum_x`、`sum_x2` |
| `ts_corr` | 是 | 更新 5 个充分统计量 | `sx, sy, sx2, sy2, sxy` |
| `ts_ema` | 是 | `new = alpha * x_new + (1-alpha) * old` | `last_ema` |
| `ts_rank` | **否** | 需要整窗排序 O(d log d) | — |
| `ts_argmax` | **否** | 需要整窗扫描 O(d) | — |
| `cs_rank` | **否** | 截面运算,无时间状态 | — |

#### `ts_std` 增量实现

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

### 5.4  L2 因子字符串缓存

L2 将完整的因子表达式结果缓存到磁盘。它跨会话使用:如果某个 Agent 提交了一个在此前会话中已在相同股票池和日期范围上评估过的表达式,结果将直接从磁盘提供,无需重算。

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

### 5.5  缓存失效策略

| 事件 | L1 影响 | L2 影响 |
|---|---|---|
| 追加新的交易日 | 增量更新(O(1) 算子) | 不失效 |
| 分红除息日 | 使受影响标的的价格算子失效 | 通过 `adj_version` 递增失效 |
| 股票拆股 | 使该标的所有价格衍生算子失效 | 通过 `adj_version` 递增失效 |
| 股票池再平衡 | 无影响(按 `universe_id` 作键) | 通过 `universe_id` 更新失效 |
| 数据修正 / 重述 | 使受影响的日期范围失效 | 该标的的完全失效 |

---

## 6. 回测与评估层

### 6.1  前向收益

前向收益在每次评估会话中预计算一次,并在所有因子评估间复用。这消除了每个因子热路径中冗余的收益计算。

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

#### 执行价格约定

| 约定 | 描述 | 推荐用于 |
|---|---|---|
| `next_open` | 在 T+1 开盘价进场 | 大多数因子策略(避免收盘价冲击) |
| `next_close` | 在 T+1 收盘价进场 | MOC 订单、指数跟踪策略 |
| `vwap` | T+1 VWAP(需要日内数据) | 更大 AUM、对冲击敏感的策略 |

### 6.2  IC 与 RankIC 计算

信息系数(IC)衡量因子值与后续收益之间的截面线性相关性。秩 IC(RankIC)衡量 Spearman 秩相关,它对离群值和分布假设更为稳健。

#### 数学定义

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

#### 通过 Numba 实现的高性能 RankIC

生产实现使用 `numba.prange` 在所有 T 个日期上同时进行数据并行执行。相对 `scipy.spearmanr` 循环的理论加速比:N=1000、T=250 时约为 ~200-275 倍。

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

#### 多期融合

因子秩只计算一次,并在所有 H 个持有期间复用,节省了 (H-1)/H 的因子排序工作。对于 H=4 个持有期,这节省了因子排序操作的 **75%**。

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

### 6.3  衰减分析

因子衰减衡量预测信号在更长持有期内衰减的速度。

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

### 6.4  批量评估

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

### 6.5  成本模型

| 参数 | 美股(第一阶段) | 港股(路线图) | A股(路线图) |
|---|---|---|---|
| 佣金 | 0.05% | 0.03% | 0.03% |
| 印花税 | 0% | 0.13% | 0.10%(仅卖出) |
| 市场冲击 k | 0.10 | 0.15 | 0.20 |
| 涨跌停限制 | 无 | 无 | 每日 ±10% |
| 结算 | T+2 | T+2 | T+1 |

*市场冲击模型:`impact = k × sqrt(order_size / adv20)`,其中 `adv20` 为 20 日平均日成交量。*

---

## 7. 用户界面

### 7.1  Python SDK

#### 单因子评估

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

#### 批量评估

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

#### 会话上下文（摊销初始化成本）

```python
# Panel loaded once: ~265ms
# Each subsequent factor: ~30-50ms (hot path only)
with assay.Session(universe='SP500',
                   period=('2020-01-01', '2024-12-31')) as sess:
    r1 = sess.backtest('ts_returns(close, 20)')
    r2 = sess.backtest('ts_corr(close, volume, 20)')
    r3 = sess.backtest(custom_factor_fn)
```

#### 自定义 Python 因子函数

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

### 7.2  FactorReport 模式

每个字段的选择都是为了给 Agent 回路的下一步提供可操作的信息。

| 字段 | 类型 | 描述 |
|---|---|---|
| `factor_id` | str | 规范化表达式的 SHA-256[:16] |
| `expr` | str | 原始表达式字符串 |
| `expr_canonical` | str | 规范化形式(用于去重) |
| `ic` | float | 评估期内的平均 IC |
| `icir` | float | IC / std(IC) |
| `rank_ic` | float | 评估期内的平均 RankIC(Spearman) |
| `rank_icir` | float | RankIC / std(RankIC) |
| `ic_by_horizon` | dict[int, float] | 各持有期的 IC:`{1: 0.04, 5: 0.035, ...}` |
| `decay_halflife_days` | int \| null | 估计的信号半衰期(交易日) |
| `turnover_1d` | float | 平均 1 日因子秩自相关 |
| `redundancy_score` | float | 与因子库中任一因子的最大秩相关 [0, 1] |
| `most_similar_factor` | str \| null | 最相近的因子库匹配的 `factor_id` |
| `lookahead_detected` | bool | 若发现 shift 错误或全局归一化则为 True |
| `failure_mode` | str \| null | `SYNTAX_ERROR` \| `LOOKAHEAD` \| `CONSTANT` \| `ALL_NAN` \| `RUNTIME_ERROR`(来自诊断系统——参见 `assay.engine.diagnostics`) |
| `suggestion` | str \| null | 自然语言改进提示(诊断给出的可操作建议) |
| `eval_period` | tuple[str, str] | 实际使用的评估日期范围 |
| `universe_id` | str | 股票池标识符 |
| `n_dates` | int | 评估的交易日数 |
| `n_symbols` | int | 平均股票池规模 |
| `lineage.prompt_hash` | str \| null | 生成该因子的 LLM 提示的哈希 |
| `lineage.data_snapshot_id` | str | 用于评估的 DataStore 快照 ID |
| `lineage.eval_timestamp` | str | 评估的 ISO 8601 时间戳 |
| `lineage.adj_version` | str | 评估时的复权因子版本 |

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

## 8. 性能

### 8.1  基准测试配置

| 参数 | 值 |
|---|---|
| 股票池规模 (N) | 1,000 只标的 |
| 时间区间 (T) | 250 个交易日 |
| 总行数 | 248,179 |
| 持有期 | 4(1、5、10、20 天) |
| Worker 数 | 8(默认 `ThreadPoolExecutor`) |

### 8.2  实测性能（v0.1,冷运行）

| 步骤 | 时间 (ms) | 占总量 % | 类型 |
|---|---|---|---|
| DataStore 初始化 + 加载复权事件 | 32.5 | 4.8% | 初始化(每会话一次) |
| 股票池加载 | 2.2 | 0.3% | 初始化 |
| DataStore.get_panel(PIT 查询 + 复权) | 222.2 | 33.1% | 初始化 |
| 前向收益预计算(4 个持有期) | 42.7 | 6.4% | 初始化 |
| FactorEngine 初始化(排序 + 上下文) | 9.9 | 1.5% | 初始化 |
| 因子评估(表达式) | 29.9 | 4.5% | **热路径** |
| IC / RankIC / ICIR / 衰减 | 331.7 | 49.4% | **热路径** |
| 冗余度 + FactorReport 组装 | 0.1 | 0.0% | 热路径 |
| **总计（冷）** | **671** | 100% | |

| 指标 | 值 | 目标 | 状态 |
|---|---|---|---|
| 会话初始化(一次性) | 309 ms | < 500 ms | ✅ 通过 |
| 每因子热路径 | 362 ms | < 500 ms | ✅ 通过 |
| 50 因子批量(热) | 361 ms/因子 | < 720 ms/因子 | ✅ 通过 |
| 100 因子预估 | ~36 s | < 60 s | ✅ 通过 |

### 8.3  热路径拆解

> **关键发现:** IC/RankIC 计算占每因子热路径的 **92%**(362ms 中的 331.7ms)。因子表达式评估本身仅占 8%。首要优化目标是 IC 计算内核,而非因子引擎。

### 8.4  计划中的优化

| 优化 | 目标 | 预期收益 | 优先级 |
|---|---|---|---|
| Numba 并行 RankIC(T 个日期并行) | IC 内核 | 相对 scipy 循环约 200 倍 | P0 |
| 多期秩融合(因子秩只算一次) | IC 内核 | 因子排序减少 75% | P0 |
| 会话级面板缓存 | `get_panel` | 222ms → 0ms(第 2 个及以后因子) | P1 |
| DAG CSE + 拓扑并行执行 | 批量吞吐 | 批量加速 10-50 倍 | P1 |
| 近似秩(200 桶,误差 < 0.003) | IC 内核 | O(N) 对比 O(N log N) | P2 |

### 8.5  理论性能界限

| 场景 | 内存带宽上限 | 目标 (P0+P1) | 当前 |
|---|---|---|---|
| 单因子(热路径) | 0.12 ms | 30–50 ms | 362 ms |
| 100 因子批量 | 6 ms | 5–10 s | ~36 s |
| 日度增量更新 | ~1 ms | < 500 ms | 完全重算 |

*内存带宽上限假设 50 GB/s 内存、float32、无数据复用。这些代表绝对的物理下界。*

---

## 9. 实现路线图

| 阶段 | 范围 | 关键交付物 | 状态 |
|---|---|---|---|
| **第一阶段 — MVP** | DataStore(Massive 美股) + FactorEngine(Python 语法) + SingleFactorBT + IC/RankIC | 在美股上端到端可运行。冷路径 < 700ms。 | DataStore + FactorEngine(解析器/AST/算子/求值器)已落地——参见 [`src/assay/engine/`](../../../src/assay/engine/);SingleFactorBT + IC/RankIC 为下一步 |
| **第二阶段 — 性能** | Numba IC 内核 + 会话面板缓存 + L1 Arena + DAG CSE + BatchFactorBT | 热路径 < 50ms。100 因子 < 10s。 | 计划中 |
| **第三阶段 — Agent 原生** | FactorReport JSON + LineageStore + SandboxExecutor + qlib 语法 + 冗余度 | AlphaBench v2 集成完成。 | 计划中 |
| **第四阶段 — 多市场** | 港股 + A股日历 + 成本模型 + 股票池快照 | 跨市场因子研究。 | 路线图 |
| **第五阶段 — 组合** | PortfolioLayer + WeightOptimizer + RiskModel + FactorComposer | 完整的组合回测能力。 | 路线图 |

---

## 10. 附录

### A.  算子速查表

| 算子 | 签名 | 输出范围 | 增量 |
|---|---|---|---|
| `ts_delay` | `ts_delay(x, d)` | 同 x | 是 |
| `ts_delta` | `ts_delta(x, d)` | 同 x | 是 |
| `ts_returns` | `ts_returns(x, d)` | (-1, ∞) | 是 |
| `ts_log_returns` | `ts_log_returns(x, d)` | (-∞, ∞) | 是 |
| `ts_mean` | `ts_mean(x, d)` | 同 x | 是 |
| `ts_sum` | `ts_sum(x, d)` | 同 x | 是 |
| `ts_std` | `ts_std(x, d)` | [0, ∞) | 是 |
| `ts_corr` | `ts_corr(x, y, d)` | [-1, 1] | 是 |
| `ts_rank` | `ts_rank(x, d)` | [0, 1] | 否 |
| `ts_argmax` | `ts_argmax(x, d)` | [0, d-1] | 否 |
| `ts_argmin` | `ts_argmin(x, d)` | [0, d-1] | 否 |
| `ts_ema` | `ts_ema(x, d)` | 同 x | 是 |
| `ts_decay_linear` | `ts_decay_linear(x, d)` | 同 x | 是 |
| `ts_regression` | `ts_regression(y, x, d)` | 返回 beta、resid | 否 |
| `ts_skew` | `ts_skew(x, d)` | (-∞, ∞) | 否 |
| `ts_kurt` | `ts_kurt(x, d)` | (-∞, ∞) | 否 |
| `cs_rank` | `cs_rank(x)` | [0, 1] | 不适用 |
| `cs_zscore` | `cs_zscore(x)` | (-∞, ∞) | 不适用 |
| `cs_demean` | `cs_demean(x)` | (-∞, ∞) | 不适用 |
| `cs_winsorize` | `cs_winsorize(x, p)` | 同 x | 不适用 |
| `cs_neutralize` | `cs_neutralize(x, group)` | (-∞, ∞) | 不适用 |
| `cs_group_rank` | `cs_group_rank(x, g)` | [0, 1] | 不适用 |
| `log` | `log(x)` | (-∞, ∞) | 是 |
| `sign` | `sign(x)` | {-1, 0, 1} | 是 |
| `abs` | `abs(x)` | [0, ∞) | 是 |
| `pow` | `pow(x, e)` | (-∞, ∞) | 是 |
| `clip` | `clip(x, lo, hi)` | [lo, hi] | 是 |
| `where` | `where(cond, a, b)` | 同 a/b | 是 |
| `safe_div` | `safe_div(a, b, fill=0)` | (-∞, ∞) | 是 |
| `fillna` | `fillna(x, method)` | 同 x | 是 |

### B.  qlib 表达式映射

| qlib 语法 | Assay 等价形式 |
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

*— Assay · AlphaBench 项目 —*
