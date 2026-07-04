# Assay

[English](README.md) · **简体中文**

Assay 是一个高性能、点对点（point-in-time）正确的因子回测引擎，面向 LLM 智能体驱动的
Alpha 挖掘。在同一套引擎之上，它提供四种接入方式——Python SDK、REST API（支持 SSE 流式）、
面向智能体的 MCP 服务，以及零安装的 WebUI——外加完整的 **组合回测（portfolio backtest）**
模块和 **因子合成（factor combination）** 工作区。

它同时服务 **美股**（NASDAQ-100 / S&P 500，数据源 MASSIVE）与 **A 股**
（沪深 300 / 500 / 1000，数据源 Tushare）。原始（RAW）供应商数据既可以由内置的
**数据管理器（Data Manager，WebUI）** 下载并保持同步，也可以从本地镜像导入——详见
[数据流水线](docs/guide/data-pipeline.md) 指南。

## 📚 文档

完整的设计规范与使用指南在 **[docs/](docs/README.md)**：

- **从这里开始：** [快速上手](docs/guide/getting-started.md)
- **数据与正确性：** [数据与复权](docs/guide/data-and-adjustment.zh.md)
  （[English](docs/guide/data-and-adjustment.md)）—— 数据层、点对点读取，以及因子在
  **拆股与分红** 下看到的价格。
- **指南：** [数据流水线](docs/guide/data-pipeline.md) · [Python SDK](docs/guide/python-sdk.md) ·
  [CLI](docs/guide/cli.md) · [REST API](docs/guide/rest-api.md) · [MCP](docs/guide/mcp-server.md) ·
  [WebUI](docs/guide/webui.md) · [因子合成](docs/guide/factor-combination.md) ·
  [组合回测](docs/guide/portfolio-backtest.md) · [性能](docs/guide/performance.md)
- **设计：** [工程](docs/design/engineering.md) · [架构](docs/design/architecture.md) ·
  [算子对照表](docs/design/operator-compatibility.md) · [组合](docs/design/portfolio-backtest.md)

本 README 其余部分是对 **数据层** 与 **因子执行引擎**（基础）的聚焦介绍；引擎之上的一切都在
指南中覆盖。

---

## 流水线做了什么

```
本地 MASSIVE 镜像         ─┐
  day_aggs_v1/*.parquet    │   读取            归一化 + PIT              读取（PIT）
  corporate_actions/*.jsonl├─►  读取器  ──►   导入器  ──►  parquet  ──►  DataStore.get_panel
NASDAQ-100 历史 (YAML)    ─┘   （本地，无网络）           存储
```

在 `$ASSAY_DATA_DIR`（默认 `./data`）下产出三个 parquet 存储，schema 见工程文档 §3.2：

| 存储 | 路径 | 内容 |
|---|---|---|
| `price_raw` | `price_raw/market=US/year=Y/month=M/price_raw.parquet` | 未复权日线 OHLCV（含成交笔数） |
| `adj_events` | `adj_events/market=US/adj_events.parquet` | 拆股、缩股、并购、分红 |
| `universe_snapshots` | `universe_snapshots/market=US/universe_snapshots.parquet` | NASDAQ-100 成分历史 |

公司行为（拆股 / 并购 / 分红）在 **读取时** 由
[`DataStore.get_panel`](src/assay/data/store/datastore.py) 施加，因此存储的价格保持原始，
任意点对点切片都能被精确复现。

---

## 安装

```bash
pip install -r requirements.txt          # 或：pip install -e .
```

### 数据源

Assay 把 **RAW** 供应商镜像与已准备好的 **ASSAY** parquet 存储分离。填充 RAW 有两种方式：

1. **从数据管理器下载**（WebUI →「数据」标签，或 `/v1/admin/*` API）：配置 MASSIVE S3
   凭证（美股）和 / 或 Tushare token（A 股），点「测试连接」，然后运行 **初始化** /
   **更新** 任务。凭证以掩码形式存储在被 git 忽略的 `.assay.config.json` 中。详见
   [数据流水线](docs/guide/data-pipeline.md) 指南。
2. **指向已有的本地镜像**，只跑 RAW→ASSAY 转换（无需凭证）—— 通过 CLI、Python API，或
   「仅导入 RAW→ASSAY」按钮。

| 变量 | 用途 |
|---|---|
| `MASSIVE_DATA_DIR` | 本地 MASSIVE（美股）镜像根目录（默认 `/data/massive_data`） |
| `TUSHARE_DATA_DIR` | 本地 Tushare（A 股）镜像根目录（默认 `/data/tushare_data`） |
| `TUSHARE_TOKEN` | Tushare API token（A 股下载） |
| `ASSAY_DATA_DIR` / `ASSAY_DATA_DIR_CN` | 已准备存储的输出根目录 |

把 `.env.example` 复制为 `.env` 可覆盖默认值——`assay.config` 会自动加载 `.env`（shell
环境始终优先）。`.env` 和 `data/` 都已被 git 忽略。

---

## 使用

### 命令行 CLI

```bash
# 准备完整的 NASDAQ-100 数据集（成分 + 公司行为 + 价格）
python -m assay.cli prepare-nasdaq100 --start 2023-01-01 --end 2024-12-31

# 单独的各个阶段
python -m assay.cli universe     --index NASDAQ100 --start 2023-01-01 --end 2024-12-31
python -m assay.cli corp-actions --start 2023-01-01 --end 2024-12-31
python -m assay.cli prices       --start 2023-01-01 --end 2024-12-31

# 检查 / 验证
python -m assay.cli status
python -m assay.cli verify  --start 2024-06-01 --end 2024-06-30 --adj split
python -m assay.cli discover           # 显示本地 MASSIVE 源目录结构
```

从源码运行用 `PYTHONPATH=src python -m assay.cli ...`；或在 `pip install -e .` 之后用
`assay-data` 入口。也有一个薄封装：`python scripts/prepare_nasdaq100.py 2023-01-01 2024-12-31`。

### Python API

```python
from assay.config import AssayConfig
from assay.data.pipeline import prepare_nasdaq100
from assay.data.store import DataStore

cfg = AssayConfig.from_env()
prepare_nasdaq100(cfg, date(2024, 1, 1), date(2024, 12, 31))

store = DataStore(cfg)
universe = store.get_universe("NASDAQ100", date="2024-06-28", as_of_date="2024-06-28")
panel = store.get_panel(
    fields=["close", "volume"],
    symbols=universe,
    start_date="2024-01-01", end_date="2024-06-28",
    as_of_date="2024-06-28",   # 必填——保证点对点正确性
    adj="split",               # none | split | total（别名：forward）
)
```

---

## 点对点（PIT）正确性

每次读取都要求一个 `as_of_date`。存储只使用在那一刻「可知」的数据行：

* **价格** —— `as_of_date` = 交易日（第 *d* 天的 EOD 日线在第 *d* 天收盘时可知）。
* **拆股** —— `as_of_date` = 执行日（execution date）。
* **分红** —— `as_of_date` = 公告日（declaration date），缺失时回退到除息日（ex-date）。
* **成分股** —— `as_of_date` = 生效日（effective date）。

### 复权（`adj`）

前复权（forward adjustment）把历史价格重标到 **最近一日的基准** 上（最新日线不变）。一个前向
比率为 `r = split_to/split_from` 的拆股，会把其除权日 **严格之前** 的每个价格除以 `r`
（除权日当天的日线已反映拆股）；成交量乘以 `r`。缩股（`r<1`）与并购换股比使用同样的比率机制。
在 `total` 模式下，每笔现金分红 `D` 会额外把其除息日之前的价格乘以 `1 − D/close_prev`
（原始的前一日收盘价）。模式：`none`、`split`（默认）、`total`。

供应商给出的 `historical_adjustment_factor` 会被存下来用于交叉核对，但 **不** 参与计算，
因此复权只由「查询时点已知」的事件算出（不会有来自未来行为的泄漏）。

---

## NASDAQ-100 历史

点对点成分建模参考
[jmccarrell/n100tickers](https://github.com/jmccarrell/n100tickers)：按年份的 YAML 文件
（内置于 [src/assay/data/universe/data/](src/assay/data/universe/data/)），保存
`tickers_on_Jan_1` 加上带日期的 `union` / `difference` 变更。

```python
from assay.data.universe import nasdaq100
nasdaq100.tickers_as_of(2020, 9, 1)          # 某日成分的 frozenset
nasdaq100.union_over_range(start, end)        # 区间内曾经的所有成分（无幸存者偏差）
nasdaq100.membership_snapshots(start, end)    # 供成分存储用的 PIT 快照
```

回测的下载股票池是区间内所有成分的 **并集**，因此退市 / 剔除的名字也会被抓取。

---

## 因子引擎

引擎（[src/assay/engine/](src/assay/engine/)）把因子表达式解析为统一 AST，并在 PIT 面板上
求值。两种前端语法降解到 **同一** AST 与算子后端——完整算子表见
[docs/design/operator-compatibility.md](docs/design/operator-compatibility.md)：

* **qlib** —— `$` 字段、驼峰算子：`Corr($close, $volume, 20)`
* **函数调用** —— Assay 原生的 `ts_*`/`cs_*`，以及 Alpha-101 / WorldQuant 写法
  （`delay`、`correlation`、`rank`、`decay_linear`、`SignedPower`、`adv{d}` 宏、
  `? :` 三元、`^` 幂、`||` 逻辑或）：`ts_corr(close, volume, 20)`

```python
from assay.engine import FactorEngine, parse

# 两种方言下等价的表达式产生相同的树：
assert parse("Corr($close, $volume, 20)").struct_hash() \
    == parse("ts_corr(close, volume, 20)").struct_hash()

eng = FactorEngine(panel)        # panel：长表 (date, symbol, *fields)
res = eng.evaluate("cs_rank(ts_corr(close, volume, 20))")
res.values                       # 对齐 date/symbol 轴的 (T, N) numpy 矩阵
res.to_frame()                   # 长表 (date, symbol, factor) DataFrame
```

用 `FactorEngine.from_store(store, universe, period, as_of)` 可直接从存储构建面板。分组算子
（`cs_neutralize`、`cs_group_rank`、`cs_group_mean`）通过 `group_data=` 传入逐股标签
（行业数据不属于 Phase-1 存储，缺失时会抛出清晰的错误）。

```bash
# 仅解析——显示方言、结构哈希、字段、算子（无需数据）
python -m assay.cli parse 'cs_rank(ts_corr($close, $volume, 20)) - 0.5'

# 在已准备的 PIT 面板上求值并汇总因子
python -m assay.cli eval 'ts_returns(close, 20)' --start 2024-01-01 --end 2024-06-14
```

### 自定义算子

算子内核在 [src/assay/engine/operators/](src/assay/engine/operators/) 包中，每类一个模块
（`time_series`、`cross_sectional`、`math_ops`、`arithmetic`），共享一个
[注册表](src/assay/engine/operators/registry.py)。用 `@op` 装饰器（或 `register(...)`）
注册你自己的算子——解析器会解析任何已注册的名字，因此立即可用于表达式：

```python
import numpy as np
from assay.engine import operators as ops, FactorEngine

@ops.op("ts_zscore", 2, 2, category="custom", output_range="(-inf, inf)")
def ts_zscore(x, d):                      # x 是 (T, N) 矩阵；d 是字面量参数
    return (x - ops.ts_mean(x, d)) / ops.ts_std(x, d)

FactorEngine(panel).evaluate("cs_rank(ts_zscore(close, 20))")   # 直接可用
```

内核对数组操作数收到 `(T, N)` 矩阵，对字面量参数收到 python 标量。传入 `needs_ctx=True`
可同时接收求值上下文 `ctx`（分组算子通过 `ctx.require_groups(name)` 解析标签）。
`unregister(name)` 移除某个算子；`operator_schema()` 返回包含自定义算子的实时 schema。

### 诊断（面向智能体挖掘循环）

`FactorEngine.diagnose(expr)` 从不抛异常——它跑完整条流水线，返回带稳定错误码、表达式内位置、
以及每个问题的可执行建议的结构化 [`FactorDiagnostics`](src/assay/engine/diagnostics.py)，
覆盖三个阶段：

* **parse**（`ASSAY-P###`）—— 语法、未知算子 / 变量、错误元数、非法窗口 / 参数
* **execute**（`ASSAY-E###`）—— 未知字段、缺少分组数据、内核抛错、错误参数
* **output**（`ASSAY-O###`）—— 序列可疑：全 NaN、高 NaN、无横截面方差、无穷、极端量级、
  过长预热、低覆盖

```python
fd = FactorEngine(panel).diagnose("ts_corr(close, volume)")
fd.ok            # False
fd.failure_mode  # "SYNTAX_ERROR"  （对应 FactorReport §7.2）
print(fd)        # 人类可读，缺陷 token 下带脱字符
fd.to_dict()     # 供 LLM 智能体的 JSON——见下
```

成功时 `fd.result` 持有 `FactorResult`，`fd.stats` 报告覆盖 / NaN 比例 / 预热 / 离散度。
面板无关的语法检查用 `lint(expr)`。完整错误码目录是 `assay.engine.diagnostics.CATALOG`。

### 101 个公式化 Alpha

来自 [Kakushadze (2016), arXiv:1601.00991](https://arxiv.org/abs/1601.00991) 的全部 101 个
alpha，都逐字编录为 Assay 表达式，见
[src/assay/factors/alpha101.py](src/assay/factors/alpha101.py)：

```python
from assay.factors.alpha101 import ALPHA_101, INDNEUTRALIZE_ALPHAS
from assay.engine import FactorEngine

eng = FactorEngine(panel, group_data={"sector": ..., "industry": ..., "subindustry": ...})
report = eng.evaluate(ALPHA_101[58])   # indneutralize 类 alpha 需要 group_data
```

保真说明：非整数天数向下取整（`floor(d)`，遵循论文）；`adv{d}` 映射到
`ts_mean(volume, d)`（成交股数——论文本意是成交额）；`signedpower` 保号，而 `^` 是普通
`pow`。18 个 `indneutralize` alpha（`INDNEUTRALIZE_ALPHAS`）需要 行业 / 子行业
`group_data`。见 [tests/factors/test_alpha101.py](tests/factors/test_alpha101.py)。

> **范围。** 这是冷启动的单因子路径：parse → evaluate。IC / RankIC / 衰减评估、
> `FactorReport`、因子库、`AssayService` + SDK、REST/MCP/WebUI 接口，以及组合回测，都建立
> 在这些内核之上——见 [文档指南](docs/README.md)。

---

## 测试

测试按模块分组于 `tests/`（与源码包镜像）：

```
tests/data/      数据层——读取器、公司行为复权、成分、DataStore PIT
tests/engine/    因子引擎——解析器、算子、方言、计算、自定义算子、诊断
tests/factors/   因子目录——101 个公式化 Alpha
```

单一入口可跑整个套件或任意模块 / 分组（会自动设置 `PYTHONPATH=src` 并透传额外 pytest 参数）：

```bash
python scripts/run_tests.py            # 整个套件
python scripts/run_tests.py engine     # 一个分组
python scripts/run_tests.py diagnostics # 一个模块
python scripts/run_tests.py engine -k corr -x   # 额外参数透传
```
