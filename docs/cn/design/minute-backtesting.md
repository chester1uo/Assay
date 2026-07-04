# 分钟级（日内）回测 — 工程设计（最终版）

状态：FINAL（对抗性评审后）
作者：factor-platform
范围：在保持日频路径数值不变的前提下，为 Assay 增加分钟/日内回测。

---

## 1. 概述与目标

Assay 目前是一个**纯日频**的因子回测引擎。面板是 `(T 个日期 × N 只标的)` 矩阵，其时间轴是一个 Python `datetime.date`。每次读取都经由 `DataStore.get_panel(...)` 并带一个 `as_of_date`，是点对点（PIT）正确的（只使用在 `as_of` 当天可知的行；公司行为在读取时应用），表达式引擎对因子求值，评估器计算前瞻收益 / IC / 衰减 / 换手率，组合回测器则按成本/执行模型进行再平衡。

我们使用位于 `/data/massive_data/us_stocks_sip/minute_aggs_v1/{YYYY}/{MM}/{YYYY-MM-DD}.parquet` 的本地 1m 镜像（列为 `ticker, volume, open, close, high, low, window_start[ns,UTC], transactions`；`window_start` = bar 起始时刻；约 390 个 RTH bar/天；约 147 万行/天；总计约 54 GB）来新增**分钟级**回测。

### 目标

1. **分钟粒度上的 PIT 正确性** — 无日内前视。一个价格 bar 只有在其*收盘*（`window_start + step_seconds`）时才可知。日粒度事件（拆股、分红、指数成分）只有在该**交易时段收盘**时才可知，绝不在时段中途的某个 `as_of` 可知。
2. **在明确容差内保持日频路径不变** — 现有的 API/SDK/CLI/MCP 及日频黄金基准数值，对于任何不重写共享内核的里程碑都保持不变；唯一重写共享内核的里程碑（M4）携带明确的 `rtol/atol` 并有意识地重新认证夹具（见 §10）。所有日频*恒等标识*（`config_hash`、`factor_id`、报告 JSON 键）在字节层面稳定。
3. **复用现有设计** — parquet 存储、读取时复权、行索引引擎、纯 numpy 评估器、基于索引的回测器在时间轴上是值不透明的。我们拓宽时间*类型*（Date → Datetime）并增加一个时段维度；我们**不**分叉数值计算。
4. **扩展到 54 GB** — 按日分区 IO、谓词下推、惰性读取时向更粗 bar 重采样、按时段分块 + 流式求值，以及一个真正（全新）的内存预算子系统。

### 综合决策

以**统一时间戳轴**作为主干（一套引擎/存储/评估器/组合；`freq` 是*数据*而非分叉的代码路径），并嫁接显式的 **`Frequency` 值对象**（将每个粒度常量集中，使得任何层都不硬编码 252/390）、惰性的**读取时 1m→5m/15m 重采样**、**按时段的物理分区 + 行组剪枝**，以及一个**强制的内存预算子系统**。我们**拒绝**并行的 `DataStore` 类（它会复制 PIT/公司行为逻辑，那是最容易发生静默偏离的危险之处）——一套存储在少数几个命名清晰的地方按 `freq` 分支。

---

## 2. 数据模型与存储

### 2.1 `Frequency` 值对象 — `src/assay/data/frequency.py`（新增）

```python
@dataclass(frozen=True)
class Frequency:
    code: str            # "1d" | "1m" | "5m" | "15m"
    base_unit: str       # "day" | "minute"
    multiple: int        # 1, 5, 15
    is_intraday: bool
    time_col: str        # "date" | "ts"
    partition_grain: str # "month" | "day"

    @property
    def step_seconds(self) -> int: ...          # 0(daily)/60/300/900
    @property
    def nominal_bars_per_day(self) -> int: ...  # 1/390/78/26 — sizing & default-horizon HINTS ONLY
    def polars_every(self) -> str: ...          # "5m"/"15m" for group_by_dynamic

DAILY     = Frequency("1d", "day",    1,  False, "date", "month")
MINUTE_1  = Frequency("1m", "minute", 1,  True,  "ts",   "day")
MINUTE_5  = Frequency("5m", "minute", 5,  True,  "ts",   "day")
MINUTE_15 = Frequency("15m","minute", 15, True,  "ts",   "day")

def parse_frequency(code: str | Frequency | None) -> Frequency: ...  # None/"1d"/"daily"->DAILY; else map; ValueError otherwise
```

`nominal_bars_per_day` **仅**用于容量估算和默认前瞻期提示。每一个承载正确性的计数（分段、调度、年化）都从日历（§3）导出**每个时段**的 bar 数，因此半日交易/夏令时永远不会移动边界。

### 2.2 Schema — `src/assay/data/schemas.py`

日频 `PRICE_RAW_SCHEMA`（第 35–46 行）**保持不动**（任何改动都会强制日频存储重建，因为 `upsert_parquet` 在 schema 不匹配时抛异常）。新增一个并行的分钟 schema：

```python
PRICE_RAW_MINUTE_SCHEMA: dict[str, pl.DataType] = {
    "ts":           pl.Datetime("ns", "UTC"),  # event_time = bar OPEN (window_start), stored UTC (DST-unambiguous)
    "session_id":   pl.Int32,                   # ET trading date YYYYMMDD; segmentation + corp-action join key
    "symbol":       pl.Utf8,
    "open":         pl.Float32,
    "high":         pl.Float32,
    "low":          pl.Float32,
    "close":        pl.Float32,                  # unadjusted
    "volume":       pl.Float32,
    "transactions": pl.Int64,
    "as_of_ts":     pl.Datetime("ns", "UTC"),    # knowledge_time = bar CLOSE = ts + step_seconds — THE PIT line
    "session_close_ts": pl.Datetime("ns","UTC"), # ET session close as UTC; the EOD-knowability instant for day-grained events (§4.4)
    "session_type": pl.UInt8,                    # 0=RTH, 1=pre, 2=post
    "source_id":    pl.Utf8,                     # provenance: per-day flat-file key
}
```

- `ts`/`as_of_ts`/`session_close_ts` 在**磁盘上是带时区的 UTC**；ET 转换只在导出以及呈现给引擎的面板轴时发生。
- `session_id`（Int32 `YYYYMMDD`）是廉价的分段键，作为 `(T,)` 向量带入引擎，也是与日粒度存储连接的键。**不**存储 `date: pl.Date`；它仅在读取时为公司行为连接而导出。
- `session_close_ts` **按每个 bar 存储**，正是为了让日粒度事件的可知性切割（§4.4）成为一次纯列比较，而无需每次读取都调用日历。
- `adj_events` 和 `universe_snapshots` 的 schema **保持不变**。

### 2.3 分区布局

日频 `price_partition_path`（schemas.py:74）**保持不变**（零迁移）。为分钟增加一个感知频率的路径和一个**按日**的分区：

```python
def price_partition_path(data_dir, market, year, month, *, freq=DAILY, day=None) -> Path:
    if not freq.is_intraday:                      # EXISTING daily path, byte-identical (no "freq=" level)
        return data_dir/"price_raw"/f"market={market}"/f"year={year:04d}"/f"month={month:02d}"/"price_raw.parquet"
    return (data_dir/"price_raw_minute"/f"market={market}"/f"year={year:04d}"
            /f"month={month:02d}"/f"day={day:02d}"/"price_raw_minute.parquet")
```

只物化**规范的 1m 存储**；5m/15m 是**惰性的读取时重采样**（§4.3）。每个日文件以 `pl.write_parquet(..., row_group_size=R)` 写入，其中 **R 被调优到让每个行组容纳一整块连续的完整标的**（文件预先按 `["symbol","ts"]` 排序），因此对 10,144 个标的中 100 个的 `is_in` 查询可以借助 `symbol` 的行组统计进行剪枝，而不必扫描每个行组。R 是*经验验证*（§8.1）的，而非假定。压缩：zstd。

### 2.4 `minute_aggs` 的摄取

`MassiveConfig`（`src/assay/config.py`）：`minute_aggs_subdir: str = "us_stocks_sip/minute_aggs_v1"` + `minute_aggs_dir` 属性。

`LocalFlatFiles`（`src/assay/data/massive/flatfiles.py`）按 `freq` 参数化：

```python
class LocalFlatFiles:
    def __init__(self, config, freq: Frequency = DAILY):
        self.root = config.minute_aggs_dir if freq.is_intraday else config.day_aggs_dir
    def list_aggs(self, start, end) -> list[AggFile]: ...      # generalizes list_day_aggs (file-listing handles holidays)
    def read_minute_agg(self, date, symbols=None) -> pl.DataFrame | None:
        # reuse existing window_start->ET conversion but DROP the trailing .dt.date(); keep tz-aware ts
```

规范化器 + 摄取器（`src/assay/data/ingest/prices.py`）：

```python
def normalize_minute_agg(df, source_id, freq, session) -> pl.DataFrame:
    # session_id, session_type from ET wall-clock vs calendar open/close;
    # as_of_ts = ts + freq.step_seconds  (BAR CLOSE) — the single PIT-critical line;
    # session_close_ts = session.close (UTC)         — the EOD-knowability instant.
    ...

class MinutePriceIngester:                 # per-DAY atomic write, NOT month-wide upsert
    def run(self, start, end, symbols=None) -> dict:
        for f in self.client.list_aggs(start, end):
            norm = normalize_minute_agg(self.client.read_minute_agg(f.date, symbols),
                                        f.key, self.freq, session_open_close(f.date))
            path = price_partition_path(self.config.data_dir, self.config.market,
                                        f.date.year, f.date.month, freq=MINUTE_1, day=f.date.day)
            write_parquet_atomic(norm.sort(["symbol","ts"]), path)   # idempotent: re-ingest overwrites the day
```

**不**使用 `upsert_parquet`（一个月约 3000 万行；读-改-写代价过高）。文件内去重按 `["ts","symbol"]`（绝不用 `["date","symbol"]`，那会把 390 个 bar 折叠成一行）。盘前/盘后 bar 通过 `session_type` 标记并保留在磁盘上，但**在读取时默认排除**。一个摄取期断言检查导出的 RTH bar 数等于 `bars_per_session(day)`（可捕获日历/数据漂移）。

---

## 3. 时间轴与日历

### 3.1 Bar 网格
日内面板的时间轴 `T` 是存在的 bar 时间戳的有序集合，呈现为名为 `ts` 的 `pl.Datetime("ns","America/New_York")`。日频是 `time_col=="date"`（`pl.Date`）的特例。引擎的轴构建（`np.unique`/`np.searchsorted`，engine.py:112–115）对 `datetime64[ns]` 是 dtype 无关的——已验证，零内核改动。

### 3.2 日历新增 — `src/assay/data/calendar.py`
保留 `trading_days`/`is_trading_day`。新增（全部包装 `exchange_calendars` XNYS，已感知夏令时/半日交易）：

```python
def session_open_close(day, calendar="XNYS") -> tuple[datetime, datetime]: ...  # ET; half-days -> 13:00 close
def session_bars(day, *, freq=MINUTE_1, include_extended=False) -> list[datetime]: ...  # authoritative bar starts; half-day -> ~210/42/14
def bars_per_session(day, *, freq=MINUTE_1, include_extended=False) -> int: ...
def session_type(ts_et, day) -> int: ...           # 0/1/2
def session_ids(time_index_et) -> np.ndarray: ...  # (T,) Int32 YYYYMMDD — the engine segment vector
def session_count(start, end) -> int: ...          # distinct trading sessions in span
```

半日交易、盘前/盘后以及夏令时全部由 `session_open_close` + UTC↔ET 转换自然得出；没有任何地方假定 390。

### 3.3 `periods_per_year`（年化）

```python
def periods_per_year(start, end, *, freq) -> float:
    sessions = trading_days(start, end)
    total_bars = sum(bars_per_session(d, freq=freq) for d in sessions)
    span_years = max((end - start).days, 1) / 365.25     # ACTUAL calendar span, not 252-nominal
    return total_bars / span_years
```

分母是**实际的日历跨度**（无 252 硬编码），且与所测的分子在单位上一致（解决了 §3.3 中的 252 硬编码问题）。这个 `"bar"` 基准是**可选加入**的；**默认**年化把逐 bar 的 NAV 聚合为每个时段一个点，并使用惯用的 `ppy=252`（§7.1），其中 252 被记录为一个日频约定并被限定在该路径中。

---

## 4. 日内点对点语义

日频 PIT 保证是结构性的：每次存储读取都过滤 `as_of_date <= as_of`（datastore.py:95,106），且 `_as_date` 截断 datetime（datastore.py:33–38）。我们拓宽时间类型，增加一个**逐 bar 的知识时刻**，并为日粒度存储增加一个**日终可知性切割**。

### 4.1 逐 bar 的知识时刻
摄取时设置 `as_of_ts = ts + freq.step_seconds`（bar 收盘）。10:30 的 bar 在 10:31 变得可知；`as_of=10:30:30` 排除它，`as_of=10:31:00` 包含它。**这一行就是整个日内价格无前视的保证**，并有专门的排除测试（§11）。一个摄取断言对每一行强制 `as_of_ts > ts`。

### 4.2 `get_panel` 拓宽 — `日内 as_of 必须支配 end`

```python
def _as_time(value, *, freq):
    return _as_date(value) if not freq.is_intraday else _parse_et_datetime(value)  # daily byte-identical
```

`DataStore.get_panel(..., *, freq=DAILY)`：
- **日频分支不变** — 过滤 `date`、`as_of_date`；返回 `["date","symbol",*fields]`。
- **分钟分支**：解析 ISO datetime；然后**钳制有效 end**：

  ```python
  effective_end = min(end, as_of)        # RESOLVES the "adjustment basis = end > as_of" finding
  if as_of < end: log/optionally raise   # no bar later than as_of may be returned OR used as the adjustment basis
  ```

  过滤条件变为 `pl.col("ts").is_between(start, effective_end)`、`pl.col("as_of_ts") <= as_of`、`session_type == 0`（除非包含扩展时段）。前向复权基准朝 `effective_end` 计算，绝不用 `end`。返回 `["ts","session_id","symbol",*fields]`。

  **不变量（新增，在代码与测试中陈述）：** `effective_end = min(end, as_of)`；带 `end > as_of` 的读取返回与带 `end == as_of` 的读取*完全相同*的复权值。测试对此进行断言。

日频的 10 个日历日分红引入期（`_DIV_LOOKBACK_DAYS`，datastore.py:30,144）在**分钟场景下被替换**为只读取**前一时段最后一个 RTH bar**（对 `start` 之前的那个时段调用 `session_open_close`）。这个引入 bar **只在 `forward_adjust` 内部**被消费以提供 `close_prev`；它**绝不会作为因子行呈现**（因此它不能作为某个窗口的种子——见 §5.2 / §8.2）。

### 4.3 重采样保持 PIT — 惰性、以时段为锚、日历完整

1m→5m/15m 发生在 **`DataStore` 内部，在 `.collect()` 之前被推入 LazyFrame**，因此引擎只会看到粗粒度面板，并且（依 §8）1m frame **绝不会被完整物化**：

```python
(lf.filter(pl.col("as_of_ts") <= as_of)                      # PIT cut first
   .group_by_dynamic("ts", every=freq.polars_every(),
                     closed="left", label="left",
                     group_by=["symbol","session_id"],         # anchor WITHIN a session — never straddle the open / mix pre-market
                     start_by="datapoint")                      # first bin starts at the session's first RTH bar
   .agg(open=first, high=max, low=min, close=last,
        volume=sum, transactions=sum,
        as_of_ts=max("as_of_ts"),
        n_constituents=count())
   .join(expected_bin_counts(freq, sessions), on=["session_id","ts"])  # calendar-expected count per SPECIFIC bin
   .filter(pl.col("n_constituents") == pl.col("n_expected"))   # emit ONLY calendar-complete bins; drops partial frontier bin entirely
   .collect())
```

PIT 规则（解决了“部分 bar 完整性”和“对齐到时段开盘”两个问题）：
- 分组**在 `session_id` 内以 `start_by="datapoint"` 为锚**，因此粗 bar 是 `[09:30,09:35),...`，绝不跨越开盘或混入盘前 bar。
- 只有当**其日历预期的全部 1m 构成 bar 在 `as_of` 时都可知**时才发出一个粗 bar——完整性是针对*该特定 bin* 的 `n_expected` 检查的（来自粗频率下的 `session_bars`，因此真正偏短的末尾/半日 bin 有正确的更小预期计数），绝不用名义的 5/15。粗 bin 中途的 `as_of` 会丢弃整个 bin（无部分收盘），保证粗 `close` 是确定且可复现的。
- 粗 `as_of_ts = max(构成 bar 的 as_of_ts) = bin_end`；仅对完整 bin 断言等于 `bin_end`。

### 4.4 公司行为与股票池 — 日终可知性切割（解决了那个 CRITICAL 前视问题）

日粒度事件（`adj_events`、`universe_snapshots`）只有在发布**时段收盘**时才可知，*绝不*在时段中途的 `as_of` 可知。我们**不**把日内 `as_of` 强制降到它的日期。相反，我们针对日终可知性时刻进行过滤：

```python
# adj_events / get_universe, intraday:
known = ((pl.col("as_of_date") <  as_of.date())                                  # earlier session: fully knowable
       | ((pl.col("as_of_date") == as_of.date()) & (as_of >= session_close(as_of.date()))))  # same session: only after close
events = df.filter(known)
```

等价地：为日粒度行导出 `as_of_known_ts = session_close(as_of_date)` 并过滤 `as_of_known_ts <= as_of`。日频退化为现有的 `as_of_date <= as_of`（日频 `as_of` *就是*日终）。

结果：一个 `as_of_date == D` 的拆股/分红/成分变更在 `D 10:00` 的分钟读取中**不可见**，而在 `D 16:00` **可见**。对拆股和分红比率都有专门的测试。

复权数学（`src/assay/data/store/adjust.py`）变为感知时段（解决了日粒度 bisect 缺陷）：
- 构建每标的的**唯一时段数组**（排序的 `session_id`），对*该数组*（而非逐 bar 数组）`bisect` `ex_date` 的时段，在**除权时段的第一个 bar** 处切割，把 `close_prev` 设为**前一时段最后一个 RTH bar**。
- 用**前一时段是否存在**的检查替换 `_MAX_PRIOR_GAP_DAYS`。
- 逐时段因子（`split_factor * div_factor`）通过一个 `session_id`→因子的连接**广播到该时段的所有 bar**（向量化重写见 §7-perf / §8）。日频退化为今天的行为（一个时段 = 一行），因此日频代码路径完全相同。

日内合格性 = `membership-known-by(as_of) AND finite-bar`。一个日内首次上市或停牌的标的会以 NaN bar 呈现（而非静默缺失的行——那会移动横截面）；一条备注 + 测试覆盖此情形。

---

## 5. 引擎改动

核心数学是行索引且值不透明的。三处附加的、按频率门控的改动，加上两处性能重写。**强制门（落在 M3，在任何表面暴露分钟之前）：** 若 `freq.is_intraday and session_ids is None`，`FactorEngine.__init__` **抛异常**——日内面板在没有分段的情况下无法求值。

### 5.1 时间轴名称/dtype（那一个硬性数据损坏缺陷）
`FactorResult.to_frame`（engine.py:79–93）把时间列硬转为 `pl.Date`（第 86 行），把每天约 390 个 bar 折叠成重复的 `(date,symbol)` 行。修复：记录源 dtype + 时间列名并原样发出：

```python
class FactorEngine:
    def __init__(self, panel, group_data=None, *, time_col="date", session_ids=None, freq=DAILY):
        ...
        self._time_col, self._time_dtype = time_col, panel.schema[time_col]   # Date | Datetime
        self._session_ids, self._freq = session_ids, freq
        if freq.is_intraday and session_ids is None:
            raise ValueError("intraday engine requires a session_ids segment vector")
```
`to_frame` 发出转为 `self._time_dtype` 的 `self._time_col`（日频→`date:Date` 不变；分钟→`ts:Datetime`）。`EvalContext` 增加 `session_ids`。

### 5.2 隔夜跳空 / 时段边界处理，与结转协调一致

`operators/_base.windows(x, d, session_ids=None)`：给定 `session_ids` 时，行 `t` 处的窗口仅当 `rows_since_session_start[t] >= d-1` 才有效（与预热同一 NaN 机制，_base.py:33–35）。`ts_delay/ts_delta/ts_returns/ts_log_returns`（time_series.py:18–44）：跨越时段边界的移位得到 NaN。**`ts_ema`/`ts_dema` 在每个时段开始处重置 `prev = NaN`**（当前内核在 time_series.py:121–122 *跨 NaN 跳空结转 `prev`*——跨隔夜边界那就是回看，一个正确性漏洞）。

**结转协调（解决了“缓冲区重新引入泄漏”问题）：** 对于按时段分块的求值（§8.2），来自前一时段的 `max(window)-1` 个 bar 的结转缓冲区**携带其真实的 `session_ids`**，且分段（NaN 填充 / EMA 重置）在**拼接 缓冲区+分块 之后**才应用，因此一个被缓冲的前一时段 bar 恰好是一个不同的分段，与整面板情形完全一致。分红引入 bar（§4.2）被完全排除在引擎面板之外。一个**分块 vs 整面板黄金测试**断言因子值在字节层面相同。

**跨时段 vs 时段内窗口（解决了那个 CRITICAL 全 NaN 问题）：** 分段**仅是时段内窗口**的策略。在逐时段分段下，一个多日窗口（例如 `'20d'` = 7800 bar > 一个 390-bar 时段）会在结构上全为 NaN。因此每个带窗口的算子接受一个显式的 `segment: "session" | "continuous"` 模式：
- **`segment="session"`**（窗口 `< bars_per_session` 时的默认）：在时段开始处 NaN 填充；窗口是时段局部的。
- **`segment="continuous"`**（当降级后的窗口 `>= bars_per_session`，例如多日窗口时自动选中）：窗口通过结转缓冲区合法地跨越前面的时段；流式内核（§8.2）在分块边界间对状态做检查点。这里的时段内渗透是刻意的，因为前瞻期本就是明确的多日。

这是根据解析后（经 `Nd` 降级）的窗口长度**逐算子决定**的，因此 `ts_mean(close,'20d')` 在多日分钟面板上**不会**全为 NaN（由测试断言）。预热成本（每个连续序列的前 `d-1` 个 bar）被记录在案。

**跨时段的 EMA（解决了“长 EMA 无意义”问题）：** bar 级的 `ts_ema(x,d)` 只对时段内的 `d`（逐时段重置）有意义。对于真正的**多日**平滑，我们提供 `ts_ema_daily(x, d_days)`：在**按时段聚合**的序列（每时段最后一个 RTH bar）上用一个以日为单位的时间常数计算 EMA，然后按 `session_id` 广播回各个 bar。当 `segment_overnight=True` 且 `d >= bars_per_session` 时，bar 级 `ts_ema` **抛异常**，引导调用者转向 `ts_ema_daily`；`segment_overnight=False` 仍然可用，但被记录为跨跳空结转状态。

### 5.3 窗口单位 — 运行时感知时段，而非解析期标量（解决了半日交易问题）

`_coerce_window`（parsing.py:354）和日频整数路径保持不动。`'Nd'` 约定**不**在解析期被烘焙成单一的 `LitNode(N*390)`。相反，`'Nd'` 字面量降级为一个 `DayWindowNode(N)`，引擎针对 `session_ids` **逐行解析**它：“回看到 `N` 个时段边界之前的那个 bar。”这需要一个接受逐行跨度的窗口算子变体（`segment="continuous"` 路径本就按时段行走）。对于无需精确跨度的常见情形，使用名义的 `N * nominal_bars_per_day` 作为近似值，**并附带一条量化半日/夏令时误差的日志备注**——我们**不**声称“永不移动”。`adv{d}`（parsing.py:301–303）和裸 `returns`→`ts_returns(close,1)`（parsing.py:346–347）绑定到 `'{d}d'`/`'1d'`，使 Alpha-101 保持日语义；在没有日内上下文时，`d` 后缀等于一个原始整数（日频不变）。

**极值位置 / rank 的输出单位（解决了静默单位变更问题）：** `ts_argmax`/`ts_argmin` 返回“距今多少个 bar”，`ts_rank` 是窗口内的分位。对于以日为单位的窗口，这些被明确记录为 **bar 计数**；注册表的 `output_range` 与诊断标签更新为标注“bars”，而当调用者想要日单位时，一个 `*_days` 辅助函数除以 `bars_per_day`。这些被加入 Alpha-101 一致性测试，以免下游阈值被静默地重新缩放约 390 倍。

**时段开盘处的 returns（解决了那个次要问题）：** 裸 `returns` → `ts_returns(close,1)` 在分段下于每个时段的第一个 bar 为 NaN（正确的无隔夜收益行为）。这被记录在案；09:30 的横截面对于收益衍生因子是已知退化的。为想要日等价收益而非依赖 bar 级 `ts_returns` 的调用者，提供了一个感知时段的 `overnight_return` 算子（前一时段收盘→开盘）。

### 5.4 截面算子 — 向量化 `cs_rank`（真正的 Alpha-101 热点）
`cs_rank`（cross_sectional.py）是 `np.vstack([_rank01_row(x[t]) for t in range(T)])`——一个逐行 Python 循环带一个内层平局 while 循环；`rank` 是最常见的 Alpha-101 算子，在 `T≈98k` 时占主导。通过在 `axis=1` 上双 `argsort` 并带 NaN 掩码和平均平局处理（`scipy.stats.rankdata` 语义）来向量化，若 `scipy` 缺失则回退到逐行路径。`_group_apply` 的 `rank` 分支同理。**这是一个共享内核**——日频容差/门控决策见 §10。

### 5.5 诊断
`diagnostics.output_diagnostics` 的阈值（`warmup_frac`、`min_coverage`、252 行假设）按 `freq` 参数化；`n_dates` → `n_periods`（附加式；日频保留 `n_dates`）。AST、解析器文法、注册表、算术/数学内核：**零改动**。

---

## 6. 分钟前瞻期的前瞻收益与评估

`metrics.py` 的 IC/RankIC、`decay.py`、`turnover.py`、`groups.py` 是逐行数学——**零代码改动**。单位存在于调用边界（`service.py`）。`forward_returns.py` 有两处改动。

### 6.1 `forward_returns` — bar 三元组掩码 + 显式策略枚举（解决了掩码绕过问题）

```python
def forward_returns(close, open_, horizons, execution="next_open", *,
                    session_ids=None, entry_lag=1,
                    cross_session: str = "mask_any_crossing"):  # | "allow_whole_day"
```

掩码基于**每行的实际三 bar 三元组**——信号 `t`、进场 `t+entry_lag`、出场 `t+entry_lag+h`——使用 `session_ids`，绝不基于 `h == nominal_bars_per_day` 的比较：
- **`mask_any_crossing`**（默认）：仅当 `session_id[t+entry_lag] == session_id[t+entry_lag+h]` 时才有效（信号→进场的跨越被记录在案；对于日等价进场约定，即 `entry_lag = bars_to_next_session_open`，信号→进场的跨越是刻意的且不被掩码，但出场必须与进场同一时段）。
- **`allow_whole_day`**：整日前瞻期由**时段计数**定义——通过 `session_ids` 得 `session_index(exit) == session_index(entry) + k`——因此半日交易是正确的（绝不用 bar 计数相等）。

`entry_lag` 泛化了硬编码的 `t+1` 跳过（第 90 行；默认 1 = 日频）。陈旧的 `execution=="vwap"` `ValueError`（第 63–67 行）随着日内 bar 的存在，变成一个真正的逐 bar 典型价 `(h+l+c)/3`（或按成交笔数加权）分支。掩码发生在函数**内部**，因此不会被遗忘。

### 6.2 服务边界处的单位
- `decay_halflife` 以前瞻期单位（分钟时为 bar）返回半衰期。服务存储 `decay_halflife` + `granularity`（带标签），**并且**存储一个导出的 `decay_halflife_days`（`÷ bars_per_day`，被记录为可能对小值取整的近似）。
- `turnover` 滞后在日频默认为 1；分钟使用 `lag = bars_per_day`（每日一次的换手；`lag=1` 是无意义的分钟自相关）。
- 分钟默认前瞻期 `default_horizons_minute = (1, 5, 30, 390)`，位于 `AssayConfig`。
- 时段前瞻收益的备忘键（service.py:180）变为 `f"fwd::{freq.code}::{execution}"`，因此一个共享时段绝不会为分钟请求提供日频矩阵（有断言测试）。

---

## 7. 日内组合回测

所有数值阶段（`accounting.py`、`weights.py`、`constraints.py`、`signal.py`、`execution.py`、`costs.py`）都是粒度中立的并被复用。改动集中在年化、调度、单位、标签。

### 7.1 年化（默认 = 聚合到日频）
`_PPY = 252` 存在于**两处**（metrics.py:34、backtester.py:72）。用一个由配置导出的、穿入 `compute_metrics` 的 `periods_per_year` 替换二者；每个度量函数都已接受一个 `ppy`/`periods_per_year` 参数。**日内默认：** `compute_metrics` 先把逐 bar 的 NAV 聚合为每个 `session_id` 一个点（在每个时段内复利），然后以 `ppy=252` 年化——避免了 `sqrt(390)` 的微观结构偏差，并复用了经过验证的 252 路径。`annualization_basis: "daily"|"bar"` 默认为 `"daily"`；`"bar"` 使用 §3.3。无风险利率的去年化（backtester.py:495）使用相同的逐期除数。

### 7.2 再平衡调度器（`rebalance.py`）
`_REBALANCE_TYPES`（config.py:40）以日历字段为键。增加日内族 + 分派 + **枚举成员 + 校验器更新**（视为一次*破坏性枚举扩展*，见 §10）：`every_n_bars`（在行索引上跨步）、`at_open`/`at_close`（每时段第一个/最后一个 RTH bar——为分钟**重定义 `daily`** 为每时段一次，而非每行）、`at_time HH:MM`（每时段的 ET 挂钟 bar）。分组器以 `session_id` 为键；周/月按每个 bar 的**时段日期**分组，因此 390 个 bar 不会折叠。

### 7.3 执行、成本、账务 — 带显式优先级的双偏移字段（解决了偏移问题）
- 在 `execution_offset_days`（config.py:83，范围 [1,3]，config.py:206 的校验器保留）旁增加 `execution_offset_bars`（面向日内，日频中立默认值）。**优先级：** 当 `bar_interval != "day"` 时 `execution_offset_bars` 权威；否则 `execution_offset_days` 权威。校验器更新为仅在日内路径上对 bar 字段做范围检查。无前视不变量（`offset >= 1` 个 bar）得以保留。
- `cov_window`/`adv_window`（config.py:94,136）接受一个 `'Nd'` 单位，在消费时转换为 bar；**风险感知的权重方法在按时段聚合的收益上运行**（限定 Ledoit-Wolf 的 `O(W)` 循环和 SLSQP QP）。为使持久化载荷的 `from_dict` 仍然有效，这些字段保持为 **int**（bar 计数），可选的字符串形式在 `__post_init__` 中解析；磁盘上的类型不变。
- `_exec_price_matrix`（backtester.py:417–423）：`vwap`/`arrival` 使用真实的逐 bar 价格；`next_open` 是下一个 *bar* 的开盘价。
- ADV/容量基准 = 每时段的日成交量，使得参与率上限保持校准。
- `output_frequency` 增加一个由分钟到日频的降采样；`_sample_series`（backtester.py:616–653）按 `session_id` 对分钟 NAV 分组。`n_trading_days` 报告**不同的时段数**；增加 `n_bars`。
- `accounting.py` 的逐行循环（accounting.py:141）和 `_benchmark_nav` 循环（backtester.py:498,505）被向量化（再平衡之间的累积乘积 NAV），因此它们不会遍历 98k 个 bar。

---

## 8. 规模与性能

修正后的内存计算：一个 `(T,N)` 字段透视在 `98k×100×8` 约为 **78 MB**（而非 8 GB）。**窗口张量**与**并发的内存中副本**才是真正的风险。

### 8.1 IO
按日分区 + 一个感知频率的枚举器（通过 `trading_days` 得逐日文件，跳过节假日）。把**所有**过滤器推入 `pl.scan_parquet`（symbol `is_in`、`ts` 范围、`as_of_ts <= as_of`、`session_type==0`）。分红引入读取**一个**前一时段文件。**经验验证剪枝：** `scripts/bench_alpha101.py` 的分钟变体测量一次 100 标的读取的 读取行数 vs 返回行数，是固定 `row_group_size` 的门。量化 252 文件/年扫描的固定开销；如果多月读取在打开/stat 开销上代价过高，评估更粗（周级）的物理分区。

### 8.2 内存 — 窗口张量 + 分块求值器
`windows()` 返回一个 `(T,N,d)` 的 `sliding_window_view`，在归约将其物化之前是零拷贝的。在 `T=98k,N=100,d=390` 时那对一个算子约为 30 GB；`'20d'`（7800）约 600 GB。修复：

1. **流式/累积内核**（M4）：`ts_mean/sum/std/cov/corr` → 累积和 / Welford 滚动；`ts_min/max/argmax/argmin` → 单调双端队列；`ts_rank` → 滚动次序统计；`ts_decay_linear` → 增量加权和。绝不物化 `(T,N,d)`。
2. **按时段分块的求值器（已明确规定，解决了规定不足问题）：** **仅沿 axis-0（时间）分块**——截面算子（`cs_rank`、`cs_demean`、中性化）每行都需要完整横截面，但它们逐行独立于 T，因此分块不影响它们（显式陈述）。`max_window` 从**解析后的 AST**（以 bar 计，经 `Nd` 降级后）计算；要求 `chunk_sessions × bars >= max_window`，否则**回退到整面板**。对于 `segment="continuous"` 窗口，流式内核通过带时段标签的结转缓冲区（§5.2）**在分块边界间检查点/恢复状态**。测试：分钟数据上跨越分块边界的 `'20d'` 窗口等于未分块结果。
3. **Float32 — 默认仅磁盘（解决了矛盾）：** Float32 在磁盘上省磁盘，而非省 RAM，因为每个透视/内核都硬编码 float64（engine.py:131–132、session.py:58–59、_base.py:30、l2.py:130/139）。因此我们对默认路径**放弃 float32-in-RAM 的说法**。一个真正的 float32 计算路径是*单独的、可选加入的*工作（把 `windows()/_matrix()/_panel_to_matrices`/流式内核按 dtype 参数化并重新验证 NaN/ddof/精度）——不作为“免费、门控”出售。

**消除三重内存副本（解决了 双/三重拷贝 问题）：** 今天一次时段运行持有 长格式面板 + `FactorEngine._matrix_cache`（engine.py:119,133）+ 一份*第二独立*的 `SessionCache._panel_to_matrices` 透视（session.py:111,49–61），在任何张量之前约为 1 GB+。修复：`SessionCache` **按引用借用引擎的 `_matrix_cache`** 而非重新透视，且引擎在矩阵缓存后丢弃 `self._panel`。真实的每时段占用被陈述为 `n_fields × T × N × 8 × ~1.x` + 长格式 frame，而 `batch()` 的 ThreadPoolExecutor 并发会将其倍增——计入内存预算（§8.4）。

### 8.3 缓存
- `SessionCache` 在 `time_col` 上透视（默认 `date`；分钟为 `ts`）——修复了在 `date` 上 `np.unique` 折叠 390 个 bar 的问题（session.py:49–56）。
- `L2FactorCache` 键（l2.py:78–105）当前以日期串 `period` + 股票池/复权/市场为键——**无当日时刻、无 `as_of`**。两次跨相同日历日期但不同日内窗口/`as_of` 的日内运行会碰撞，为一个 16:00 请求提供一个 14:00 矩阵。**修复（解决了 L2 键问题）：** 以完整的日内区间 `(start_ts, end_ts)` 和 `as_of_ts` 扩展原像；对日频这些退化为现有的日期串，因此**日频键在字节层面不变**。命名空间 `assay-l2-v1`→`v2` 升级。增加一个**真正的按字节计的 LRU**（今天只有 `clear()`，l2.py:178）。**只有在键携带 as_of 轴且 LRU 存在之后**，L2 才接入 `evaluate()`。
- **缓存键基数（解决了爆炸问题）：** 日内 `as_of` 是逐 bar 的（约 390/天），使 L2 条目和 SessionRegistry 时段成倍增加。策略：**默认仅在时段收盘 `as_of` 处缓存**（或把 `as_of` 量化到一个粗桶），限定键空间；`SessionRegistry` 获得一个**带 LRU 逐出的 字节/大小上限**以及一个记录在案的分钟预期时段预算（今天过期是手动的，session.py:236）。容量算术连同 `as_of` 和 `freq` 乘子一并重做。

### 8.4 内存预算子系统（全新——独立的里程碑，解决了那个“空气产品”CRITICAL）
`l1_memory_gb=4.0`/`l2_max_gb=20.0`（config.py:120–121）目前**无处**被读取。守卫从零构建：
- **(a) 预检估算**：由分区文件大小 × 标的/行组剪枝比 × `freq.step` × dtype 得出——独立于 `.collect()`。
- **(b) 在 `DataStore.get_panel` 于 `.collect()` 之前强制**（datastore.py:99）：若估算 > 预算，抛出一个可操作的错误（建议更粗的 `freq` / 更短的窗口 / 分块模式）或自动切换到分块/流式路径。
- **(c)** `L2FactorCache` 中按字节计的 LRU + `SessionRegistry` 中带逐出的每时段字节上限。
- **(d)** 为分钟重新调优 `l1_memory_gb`/`l2_max_gb` 默认值（原为 10k 行日频面板设定），由基准测试喂养。

### 8.5 挂钟门
`scripts/bench_alpha101.py` 的分钟变体是一个性能门（目标在流式内核落地后设定，§12）。流式内核 + 向量化 `cs_rank` + 5m 默认，使得 1 年 NASDAQ-100 单因子求值可行。

---

## 9. API / SDK / CLI / config 表面

一个可选的 `frequency`（别名 `freq`/`granularity`，默认 `"1d"`）贯穿各处；**省略它即复现今天的行为**——由一个表面契约测试（如下）验证，而非仅仅断言。编辑集横跨约 15 个文件；其中三个承载行为并有各自的测试。

- **`AssayConfig`**：`default_frequency="1d"`、`default_horizons_minute=(1,5,30,390)`、`annualization_basis="daily"`；`MassiveConfig.minute_aggs_subdir`/`minute_aggs_dir`。
- **`DataStore.get_panel(fields, symbols, start, end, as_of, adj, *, freq=DAILY)`** — 分钟接受 ISO datetime；`effective_end=min(end,as_of)`（§4.2）。
- **`FactorEngine.__init__(panel, group_data=None, *, time_col="date", session_ids=None, freq=DAILY)`**；`from_store(..., freq="1d")` 构建时段向量并路由 存储/schema/布局；日内无时段向量时抛异常。
- **`forward_returns(..., *, session_ids=None, entry_lag=1, cross_session="mask_any_crossing")`**。
- **`AssayService.evaluate/batch/create_session/correlation_matrix(..., frequency="1d")`**。非透传之处被列为带测试的显式子任务：**(1)** 折入 freq 的**时段备忘键**（断言日频+分钟在一个时段中不碰撞）；**(2)** `_resolve`（service.py:106–123）按 freq 分支处理**区间/as_of 解析 以及 前瞻期/换手滞后/组收益前瞻期默认值**；**(3)** 每个表面的 `service_kwargs()` 对 `frequency` 做 drop-None。
- **`as_of`/区间解析（解决了重载问题）：** 在 **CLI** 中，当 `--frequency` 为日内时用 `datetime.fromisoformat` 解析 `--start/--end/--as-of`（接受日期*或* datetime）；日频时保留 `_date`（对 datetime 抛异常）。在**服务边界，若在 `freq="1d"` 下提供了日内时间分量则抛异常**（否则会静默截断）。REST 字段描述/校验在日内时接受 ISO datetime。测试：`datetime as_of + freq=1d` 被拒绝；`datetime as_of + freq=1m` 被采纳。
- **组合 config**：`bar_interval`/`annualization_basis`；日内 `rebalance_type` 值；`execution_offset_bars`（优先级 §7.3）；日内 `output_frequency`。
- **REST**（`api/models.py`、`routes/portfolio.py:79`）：可选 `frequency`；`service_kwargs()` drop-None 转发它；`build_config` 拒绝未知的日内枚举值并给出可操作消息，指明所需的 `schema_version`（§10）。
- **CLI**（`cli.py`）：`--frequency`；日内模式下 ISO-datetime 解析；`--horizons` 接受 `Nd`/`Nm`；新增 `assay ingest-minute`。
- **MCP**（`mcp/server.py`）：evaluate/batch/correlation 上的 `frequency`（默认 `"day"`）；`assay_system_status` 报告解析后的频率 + 分钟默认值。
- **表面契约测试：** 以省略 `frequency` 的方式调用每个 REST/CLI/MCP/SDK 入口，断言解析后的服务 kwargs 与改动前完全相同。

---

## 10. 向后兼容与迁移

零日频迁移；结构性的，而非愿景性的。

- **存储**：日频 `PRICE_RAW_SCHEMA`、`price_partition_path`（月）、`adj_events`、`universe_snapshots` 保持不动。分钟是一个**新存储**（`price_raw_minute`）；无重建。
- **读取**：`_as_date` 对 `freq=DAILY` 仍然截断 datetime；日频 `get_panel` 返回完全相同的 `date`/`pl.Date` frame。
- **引擎**：对共享代码的编辑仅有 `to_frame` 转回记录的 dtype（对日频是空操作）以及新的 `time_col`/`session_ids`/`freq` 参数默认为今天的行为。分段路径以 `session_ids is not None` 门控 → 日频字节层面相同。

- **`config_hash`/`run_id`（解决了那个 CRITICAL）：** `config_hash()` 对 `json.dumps(self.to_dict())` 求哈希，而 `to_dict()` 是一个被 `from_dict`、REST `build_config`、报告和 WebUI 消费的普通 `asdict()`。我们**不触碰 `to_dict()`**。相反，引入一个冻结的 `_HASH_FIELDS_V1` 白名单（今天存在的字段），并**仅在 `bar_interval=="day"` 时对这些字段**计算 `config_hash()`；新的日内字段被完全排除在日频原像之外。一个回归测试断言默认 US config 的 `config_hash()` 与已提交的改动前值**字节层面相同**。在 M0/M6 决定（不是开放问题）。

- **`compute_factor_id`（解决了那个 major）：** 签名变为 `compute_factor_id(expr_canonical, granularity="1d")`，带**承载分量的条件**：`return hash(expr) if granularity=="1d" else hash(f"{expr}::{granularity}")`。两个日频调用点（service.py:313,374）都传入解析后的频率。一个测试固定 `compute_factor_id(expr) == compute_factor_id(expr,"1d") == <已提交的遗留摘要>`，因此 `library.save()`（store.py:84）绝不会为现有的日频 `.json` 文件重新设键。

- **枚举扩展的 schema 版本化（解决了那个跨版本 major）：** `from_dict` 忽略未知键（保护新代码读取旧载荷），但旧代码拒绝新枚举值（`rebalance_type="at_open"` 使 `_enum` 失败）。因此枚举扩展是一次**破坏性 schema 变更**：把 `schema_version` 升级并放入 `config.to_dict()` 和报告 JSON；旧读者**响亮地**失败并给出指明所需版本的消息。`build_config`/`from_dict` 拒绝未知日内枚举并给出可操作错误。

- **共享内核重写 vs “字节层面相同”（解决了那个 major）：** M4 的流式 `ts_*` 和 §5.4 的 `cs_rank` 向量化是**共享**内核；累积和滚动方差/协方差与窗口化计算**不是**位相同的，且向量化的平局处理可能有差异。我们通过**把流式内核门控在 `freq.is_intraday` 之后并为日频保留物化路径**来解决（因此日频保持字节层面相同）——除非有意识地决定为日频大 `d` 采用流式，这时**有意识地**在明确的 `rtol/atol` 下重新认证黄金夹具。本文档的“字节层面相同”承诺，**仅对任何改变共享日频内核的里程碑降级为“在明确容差内”**，且逐算子门控被显式陈述。`cs_rank` 用平均平局/NaN 一致性测试对照当前实现验证；若仍有任何偏离，则以日内门控发布。

- **报告 JSON 字段别名（解决了那个 minor）：** 新的带标签字段（`granularity`、`decay_halflife`、`n_periods`、`turnover`）是附加式的。对**分钟**报告，`n_dates` = **不同时段计数**（与其名称匹配），`decay_halflife_days` 被记录为导出/近似；WebUI/agent 在 `granularity!="1d"` 时读取带标签字段。一个序列化测试断言对 `granularity="1d"`，JSON 键/值与改动前字节层面相同。

- **L2 命名空间**升级仅影响（当前未使用的）L2 缓存；日频键字节层面不变（§8.3）。

---

## 11. 分阶段实施计划

**框架（解决了增量性问题）：** 每个里程碑都是**日频安全的**（改动按 freq 门控）。**分钟路径在 M3+M5 落地前是静默错误的**，因此公共表面（REST/CLI/MCP/SDK）上的 `frequency != "1d"` 在 M5 完成前抛 `NotImplementedError`；§5.1 的日内需要时段向量守卫落在 M3。下列每个里程碑都可独立发布**且**可独立测试。

**M0 — `Frequency` + config + 日历 + 恒等守卫（无行为变更）。**
`frequency.py`；`MassiveConfig.minute_aggs_dir`；`AssayConfig` 分钟默认值；日历辅助函数（`session_open_close`、`session_bars`、`bars_per_session`、`session_ids`、`session_type`、`session_count`）；**`config_hash` `_HASH_FIELDS_V1` 白名单** + 回归测试；`compute_factor_id(expr, granularity="1d")` + 遗留摘要固定；`schema_version` 字段。
测试：半日 bar 计数、夏令时边界、`config_hash`/`factor_id` 对照已提交摘要的字节稳定性。

**M1 — 分钟摄取 + 分钟存储 schema/布局。**
`PRICE_RAW_MINUTE_SCHEMA`（含 `session_close_ts`）、`price_partition_path(freq=...)`、按 freq 参数化的 `read_minute_agg`、`MinutePriceIngester`（按日原子写、调优的 `row_group_size`）、`assay ingest-minute`。
测试：往返（`as_of_ts=ts+step`、`session_close_ts`、`session_type`、盘前/盘后排除计数）、幂等再摄取、行组剪枝比。

**M2 — `DataStore` 中的日内 PIT 读取路径。**
`_as_time`、带 `effective_end=min(end,as_of)` 的 `get_panel(freq=...)` 分钟分支、按日文件枚举器、前一时段分红引入（排除在面板外）、**`adj_events`/`get_universe` 的日终可知性切割**、感知时段的 `forward_adjust`（向量化的 `session_id`→因子广播，无逐 bar Python 列表）。
测试：日内 `as_of` 排除（10:30:30 排除 10:31）；**`as_of_date==D` 的拆股在 D 10:00 不可见、在 D 16:00 可见**；`end>as_of` ⇒ 与 `end==as_of` 相同的值；日频黄金不变。

**M3 — 引擎日内语义（门控分钟暴露）。**
`to_frame` dtype 修复；`time_col`/`session_ids`/`freq` 管线 + 日内需要时段向量守卫；带 `segment="session"|"continuous"` 自动选择的分段 `windows`/`ts_*`；`ts_ema`/`ts_dema` 逐时段重置 + `ts_ema_daily`；运行时 `'Nd'`/`DayWindowNode` 降级；极值位置/rank 单位重标；向量化 `cs_rank`（门控到日内，或依 §10 为日频容差认证）。
测试：隔夜跳空 NaN；EMA 时段独立；`ts_mean(close,'20d')` 在多日分钟面板上**不**全 NaN；分块==整面板黄金；所有 `ts_*` 在日频上字节层面相同。

**M4 — 流式窗口内核 + 内存预算子系统（§8.4）。**
带跨块状态检查点的流式 `ts_mean/sum/std/min/max/argmax/argmin/rank/decay_linear/cov/corr`；按时段分块的求值器（AST 最大窗口、axis-0 分块、整面板回退）；**预检大小估算 + `get_panel` 预算守卫 + L2/SessionRegistry LRU**；重新调优的预算；基准门。
测试：流式==物化黄金（日频在容差内 / 日内）；`'20d'` 跨块边界 == 未分块；峰值 RAM 断言；预算守卫以可操作消息抛异常。

**M5 — 分钟前瞻期的评估器（解锁公共分钟表面）。**
`forward_returns` bar 前瞻期 + bar 三元组掩码 + `cross_session` 枚举 + `entry_lag` + 真实 `vwap`；服务单位转换 + 折入 freq 的备忘键；分钟前瞻期默认值；解除 `NotImplementedError` 表面门。
测试：跨时段收益被掩码；`allow_whole_day` 按时段计数（半日正确）；半衰期单位转换；日频评估器不变。

**M6 — 日内组合。**
`periods_per_year` + 日聚合默认；日内再平衡类型 + 分派 + 校验器 + `schema_version`；`execution_offset_bars` 优先级；以 bar 为单位的 cov/adv 窗口；日内执行价格；由分钟到日频输出；`n_bars`/`granularity`；向量化账务遍历。
测试：日聚合下 Sharpe/波动稳定；`at_open`/`every_n_bars`；`offset>=1`；默认日频 config 的 `config_hash` 字节稳定；日频组合黄金不变。

**M7 — 表面 + 缓存 + 库 + L2 接线。**
REST/CLI/MCP/SDK 上的 `frequency`，带 ISO-datetime 解析 + 日频截断拒绝；`SessionCache` 时间轴泛化 + **借用引擎矩阵**；**L2 键携带 `(start_ts,end_ts,as_of_ts)` + 命名空间升级 + LRU，然后接入 `evaluate()`**，带仅时段收盘 `as_of` 缓存策略；`factor_id`/报告 `granularity` 共存。
测试：表面契约（省略 frequency ⇒ 相同 kwargs）；日频+分钟报告共存；L2 无日频/分钟且无 as_of 别名；`granularity="1d"` 时报告 JSON 字节层面相同。

---

## 12. 开放问题与残余风险

**首要残余风险。** (1) `as_of_ts = bar close` 和日终可知性切割就是整个日内 PIT 故事——一行写错就静默地重新引入前视；M2 中的专门排除测试 + 摄取不变量 `as_of_ts > ts`。(2) 流式内核（M4）必须在明确容差内匹配物化的 NaN/`ddof`/平局语义；为日频安全而门控到日内。(3) 运行时 `'Nd'` 降级除非使用精确的逐行时段行走，否则是相对名义 bar 计数的*近似*——半日/夏令时误差被记录而非隐藏。

**开放问题（非阻塞）。** 哪个 bar 标记时段 NAV 用于聚合（16:00 RTH vs 竞价代理）？是否要暴露逐 bar（`"bar"`）年化。分钟尺度上的 `adv{d}`——`d` 个时段的加总 bar 成交量 vs 一个日聚合的伴随成交量字段。M4 基准目标数值。扩展时段研究模式及其与 `bars_per_session`/年化的交互。日频大 `d` 是否应采用流式内核（容差认证）或保持物化。

---

## 已解决的评审问题

- **公司行为/股票池日内 as_of 强制（critical）：** §4.4 — 无日期强制；经存储的 `session_close_ts` 做日终可知性切割（`as_of_date < as_of.date()` 或 同日在 `session_close` 之后）；拆股时段中途不可见测试。
- **前向复权基准 = end > as_of（major）：** §4.2 — `effective_end = min(end, as_of)` 同时钳制 bar 过滤器与复权基准；不变量 + 测试。
- **重采样部分 bar 完整性（major）：** §4.3 — 完整性针对每个特定 bin 的日历预期构成集检查；部分前沿 bin 被整体丢弃；粗 `as_of_ts = bin_end`。
- **`forward_returns` 掩码绕过（major）：** §6.1 — 通过 `session_ids` 对 信号/进场/出场 bar 三元组掩码；显式 `cross_session` 枚举；整日由时段计数定义。
- **生存者/股票池日内（minor）：** §4.4 — 同一日终可知性规则；日内上市/停牌以 NaN 呈现；测试有备注。
- **结转重新引入泄漏（minor）：** §5.2 — 缓冲区携带真实 `session_ids`；分段在 缓冲区+分块 拼接后应用；分红引入排除在面板外；分块==整面板黄金。
- **内存预算守卫是空气产品（critical）：** §8.4 — 全新子系统：预检估算、在 `get_panel` 强制、L2 + SessionRegistry 中按字节计的 LRU、重新调优预算、其自身里程碑（M4）。
- **双/三重内存副本（major）：** §8.2 — `SessionCache` 借用引擎矩阵；引擎在缓存后丢弃 `self._panel`；真实占用连同 `batch()` 并发一并陈述。
- **Float32-in-RAM 矛盾（major）：** §8.2 — 降级为仅磁盘的收益；float32 计算路径是单独可选加入，而非“免费/门控”。
- **`forward_adjust` 大规模下的逐标的 Python 循环（major）：** §4.4 — 向量化的逐时段因子 `session_id`→因子 连接/广播；针对唯一时段数组 bisect；无逐 bar `.to_list()`/DataFrame 重建。
- **重采样先收集完整 1m（major）：** §4.3 — 重采样被推入 LazyFrame，以粗粒度收集；峰值 RAM 有界，而非完整 1m 切片。
- **L2 键遗漏日内窗口/as_of（major）：** §8.3 — 原像扩展 `(start_ts,end_ts,as_of_ts)`（日频退化不变）；L2 仅在此 + LRU 之后接线。
- **缓存键基数爆炸（major）：** §8.3 — 仅时段收盘 as_of 缓存（或量化）；SessionRegistry 字节上限 + LRU；算术重做。
- **流式内核承载分量却被推迟（major）：** §11 — 分钟暴露门控在 M3+M5 之后；预算守卫（M4）抛异常而非 OOM；分块路径限定 RAM。
- **分块求值器规定不足（minor）：** §8.2 — 仅 axis-0；cs 算子不受影响；AST 最大窗口；整面板回退；跨块状态检查点。
- **行组剪枝未验证（minor）：** §8.1 — 调优的 `row_group_size`，经基准经验验证；252 文件开销量化。
- **分段下窗口 NaN = 多日全 NaN（critical）：** §5.2 — `segment="session"|"continuous"`；多日窗口经结转跨时段；`ts_mean(close,'20d')` 非全 NaN 测试。
- **`'Nd'` 降级 vs 半日（major）：** §5.3 — 运行时 `DayWindowNode` 针对 `session_ids` 逐行解析；名义仅作为记录的近似使用；放弃“永不移动”的说法。
- **重采样对齐到时段开盘（major）：** §4.3 — `group_by_dynamic` 在 `session_id` 内以锚（`start_by="datapoint"`）；首/末/半日 bin 由完整性测试覆盖。
- **长 EMA 日内无意义（major）：** §5.2 — `ts_ema_daily`（在按时段聚合序列上以日为时间常数）；bar 级 `ts_ema` 在 `d >= bars_per_session` 时抛异常。
- **极值位置/rank 单位变更（major）：** §5.3 — 记录为 bar 计数；注册表/诊断重标；`*_days` 辅助函数；一致性测试。
- **`returns` 在时段开盘处 NaN（minor）：** §5.3 — 记录在案；提供 `overnight_return` 算子。
- **`periods_per_year` 硬编码 252（minor）：** §3.3 — 年数由实际日历跨度得出；252 限定在日聚合约定中。
- **`config_hash`/`run_id` 稳定性（critical）：** §10 — `to_dict()` 不动；冻结的 `_HASH_FIELDS_V1` 白名单在日频时排除日内字段；字节稳定回归测试；在 M0 决定。
- **`compute_factor_id` 签名（major）：** §10 — `(expr, granularity="1d")`，带显式的 `"1d"` 保留遗留 条件；遗留摘要固定；无日频文件重设键。
- **日频“字节层面相同” vs M4 共享内核重写（major）：** §10 — 流式/`cs_rank` 门控到日内（日频字节层面相同）或有意识地容差认证；承诺仅在共享日频内核改变处降级为“在容差内”。
- **跨版本 config 序列化 / 枚举扩展（major）：** §10 — config + 报告 JSON 中的 `schema_version`；旧读者响亮失败；日内枚举被拒绝并给出可操作错误。
- **`as_of` 日频 vs 日内重载（major）：** §9 — 感知 freq 的 CLI/REST 解析；服务在 `freq=1d` 下拒绝 datetime as_of；两向都测试。
- **表面波及范围 vs “一个 kwarg”（major）：** §9 — 枚举非透传之处（折入 freq 的备忘键、`_resolve` 默认值、每表面 drop-None），带表面契约测试。
- **分阶段增量性（major）：** §11 — “始终日频安全；仅在标记里程碑分钟可用”；M5 前 `NotImplementedError` 表面门；M3 中的引擎守卫。
- **FactorReport 日单位别名（minor）：** §10 — 分钟 `n_dates` = 不同时段计数；消费者读取带标签字段；日频 JSON 字节层面相同。
- **`execution_offset_days` 双字段（minor）：** §7.3 — 显式优先级（日内 `bars` 胜出）、日频中立默认、排除在哈希外；cov/adv 保持 int 以使 `from_dict` 有效。

## 推荐的首个里程碑

**先构建 M0。** 它是最小的、自包含的、有真实价值且零行为变更的增量：`Frequency` 值对象、日历辅助函数（`session_open_close`/`session_bars`/`bars_per_session`/`session_ids`）、config 管线，以及——关键地——两个**恒等守卫**（`config_hash` `_HASH_FIELDS_V1` 白名单 和 带遗留摘要固定的 `compute_factor_id(expr, granularity="1d")`）加上 `schema_version` 字段。它完全在默认值之后发布，不触碰任何 读取/求值/组合 数值，完全可单元测试（半日计数、夏令时偏移、对照已提交摘要的字节稳定 `config_hash`/`factor_id`），并且**在任何分钟代码存在之前就为三个 CRITICAL 向后兼容发现去风险**——因此每个后续里程碑都建立在一个冻结的、已验证的日频恒等契约之上。
