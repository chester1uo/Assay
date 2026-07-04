# 数据与复权

[English](data-and-adjustment.md) · **简体中文**

这是「能否相信一次回测」最重要的文档：**Assay 存了什么数据，以及因子在公司行为（拆股、分红）
下究竟看到什么价格。** 这里错了，所有 IC 数字都没有意义。Assay 的设计目标是：因子 *永远* 看不到
查询时点尚不可知的信息，且复权价格能从第一性原理复现。

---

## 1. 两层：RAW → ASSAY

| 层 | 是什么 | 位置 |
|---|---|---|
| **RAW** | 供应商原样数据，就地读取 | `MASSIVE_DATA_DIR`（美股）、`TUSHARE_DATA_DIR`（A 股） |
| **ASSAY** | 引擎读取的、归一化的点对点 parquet 存储 | `ASSAY_DATA_DIR` / `ASSAY_DATA_DIR_CN` |

**导入** 步骤（`prepare_us` / `prepare_cn`）把 RAW 归一化为固定 schema 并写出 ASSAY 存储。
下游不再读取 RAW——引擎、IC 评估、组合回测和 WebUI 都通过同一个接口
[`DataStore`](../../src/assay/data/store/datastore.py) 读取 ASSAY 存储。

### ASSAY 存储

| 存储 | 粒度 | 关键列 |
|---|---|---|
| `price_raw` | 每 (date, symbol) 一行 | `date, symbol, open, high, low, close, volume, transactions, as_of_date, source_id` |
| `adj_events` | 每个公司行为一行 | `symbol, ex_date, event_type, split_ratio, dividend_cash, as_of_date, provider_adj_factor` |
| `universe_snapshots` | 每次成分变更一行 | `index_id, effective_date, symbols[], as_of_date` |
| `trade_status`（A 股） | 每 (date, symbol) 一行 | `date, symbol, up_limit, down_limit, limit_up_locked, limit_down_locked, close` |
| `security_groups`（A 股） | 每 symbol 一行 | `symbol, group, as_of_date` |

价格以 **原始 / 未复权** 存储。公司行为 *单独* 存在 `adj_events`，复权在 **读取时** 施加——
从不写进存储。正是这一点让任意点对点切片可复现：相同的原始价格 + 只有查询时点已知的事件，
唯一地得到一个复权面板。

---

## 2. 双时间：`date` 与 `as_of_date`

每一行都带两个时间：

- **`date`** —— *事件时间*：日线 / 行为发生的那一天。
- **`as_of_date`** —— *可知时间*：这一行第一次「可知」的那一天。

每次读取都要求一个 **必填的 `as_of_date`**，存储只返回 `as_of_date <= as_of_date` 的行。
因此前瞻偏差在结构上不可能——你无法查询到当时尚不存在的数据。各来源的可知时间：

| 来源 | `as_of_date` 是…… |
|---|---|
| 日线（EOD） | 交易日（当天收盘时可知） |
| 拆股 / 股本变更 | 除权 / 执行日 |
| 现金分红 | 公告日（缺失时回退到除息日） |
| 成分归属 | 生效日 |

```python
panel = store.get_panel(
    fields=["close", "volume"], symbols=universe,
    start_date="2024-01-01", end_date="2024-06-28",
    as_of_date="2024-06-28",   # 必填——此后的一切不可见
    adj="split",
)
```

---

## 3. 因子在拆股与分红下看到的价格

因子从不直接接触 `adj_events`。它看到的是 `DataStore.get_panel(..., adj=...)` 返回的、已复权的
`(T, N)` 价格矩阵。计算逻辑在 [`adjust.py`](../../src/assay/data/store/adjust.py)，采用
**前复权（即「后复权到今天」）**：历史价格被重标到 **最近一日的基准** 上，因此最新日线永远等于
原始价格，只有过去被重标。

### 3.1 拆股与股本变更

一个 **前向比率** 为

```
r = split_to / split_from        # 1 拆 2 → r = 2 ；10 缩 1 → r = 0.1
```

的股本变更事件（拆股、缩股、并购换股），会把其除权日 **严格之前** 的每个价格除以 `r`，并把对应的
**成交量** 乘以 `r`。除权日当天的日线已反映拆股，故保持不变。多次拆股按乘法叠加。缩股（`r < 1`）
与并购换股用同样机制。

> 例：2024-06-10 执行 1 拆 2。2024-06-07 的收盘 `$400`，复权后变为 `$200`（÷2），与
> 2024-06-10 起的约 `$200` 日线连续对齐；2024-06-07 的成交量翻倍。因此跨拆股计算的收益是正确的。

### 3.2 现金分红（仅 `adj="total"`）

在 `total` 模式下，一笔除息日为 `e` 的现金分红 `D`，会把 `e` **严格之前** 的每个价格乘以

```
ratio = 1 − D / close_prev
```

其中 `close_prev` 是 `e` 前一交易日的 **原始** 收盘价。由于 `D` 与 `close_prev` 都在原始价格
空间，分红因子与拆股因子正确叠加（`price_factor = split_factor × dividend_factor`）。两条保护：

- **前收缺口** —— 若 `e` 前一交易日在 `_MAX_PRIOR_GAP_DAYS = 10` 个自然日之外（数据缺口，或在
  加载的前导区间之外），则跳过该分红而非错误缩放。
- **分红 ≥ 价格** —— 若 `D ≥ close_prev`（ratio ≤ 0），跳过该分红而非把历史翻负。

### 3.3 复权模式（`adj`）

| 模式 | 拆股 | 分红 | 何时用 |
|---|---|---|---|
| `none` | ✗ | ✗ | 需要真实成交价（如涨跌停逻辑） |
| `split`（默认） | ✓ | ✗ | 大多数 alpha 研究——跨拆股连续，无分红漂移 |
| `total`（别名 `forward`） | ✓ | ✓ | 全收益研究；分红再投资 |

供应商自带的 `historical_adjustment_factor` 会 **存但不用** 于计算——复权只由查询时点已知的事件
算出，因此不会有来自「悄悄编码了未来行为」的供应商因子的泄漏。

### 3.4 为因子选择 `adj`

- 动量 / 反转 / 多数价格形态因子 → **`split`**（默认）。你要跨拆股连续的序列且无分红台阶，但也
  不要拆股处的原始价格跳变。
- 与全收益基准比较，或显式建模分红再投资 → **`total`**。
- 执行约束 / 涨跌停逻辑（A 股）→ 对与原始 `up_limit` / `down_limit` 比较的价格用 **`none`**
  （见 §4）。

---

## 4. A 股特有事项

A 股数据（Tushare）比美股多几处细节：

- **送转** 被转换为拆股比率："10 转 15" → `1 + 15/10 = 2.5`，即当作 `r = 2.5` 的股本变更事件。
  现金分红进入 `adj_events.dividend_cash`（优先税后值）。
- **涨跌停** 存于 `trade_status`（`up_limit` / `down_limit` 及 `*_locked` 标志）。对执行约束用
  `adj="none"` 的价格——一只封涨停的股票无法按收盘买入。
- **成交量单位** —— Tushare 以 手（100 股）报量。导入时已归一化；ASSAY 的 `volume` 列以「股」为
  单位，与美股路径一致。
- **增量更新** 按 **交易日** 抓取（每次 API 调用返回全市场所有股票），并追加到逐股原始文件，因此
  一次日更是个位数次调用，而非每只股票一次。

---

## 5. 自己复现

```python
from assay.data.store import DataStore
store = DataStore(cfg)

raw   = store.get_panel(["close"], syms, s, e, as_of, adj="none")   # 成交价
split = store.get_panel(["close"], syms, s, e, as_of, adj="split")  # 拆股连续
total = store.get_panel(["close"], syms, s, e, as_of, adj="total")  # + 分红

# 三种模式下最新日线完全相同；只有历史被重标。
```

命令行交叉核对（在已知行为附近对比复权与原始）：

```bash
python -m assay.cli verify --start 2024-06-01 --end 2024-06-30 --adj split
```

另见：[数据流水线](data-pipeline.md)（如何加载 / 更新数据）·
[快速上手](getting-started.md) · [工程设计](../design/engineering.md)。
