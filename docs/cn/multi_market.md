# 多市场支持(中国 A 股 + 香港)

Assay 的数据层与组合回测器与市场无关:价格、
公司行为和指数成分存放在按 `market=...` 分区的标准存储中,
而执行模拟器已经建模了 A 股的
微观结构规则。本文档介绍如何端到端地在**中国
A 股**上运行本框架,使用 Tushare 镜像(见 [tushare.md](tushare.md))。

## 1. 导入:原始 Tushare → 标准 CN 存储

`assay.data.tushare.ingest.prepare_cn` 读取原始镜像
(`$TUSHARE_DATA_DIR`,默认 `/data/tushare_data`),并写入与
美股管线相同的标准存储,置于 `market=CN` 下:

| 存储 | 来源 | 说明 |
|-------|--------|-------|
| `price_raw` | `cn/daily` | 未复权 OHLCV(`volume` 单位为 手/lots) |
| `adj_events` | `cn/dividend` | 送转 → 前向 `split_ratio = 1 + stk_div`;税前现金(`cash_div_tax`)→ `dividend_cash`;仅*已实施*(实施)的分红 |
| `universe_snapshots` | `cn/index_weight` | CSI300/500/1000 点对点成分(成分变更快照) |
| `trade_status` | `cn/stk_limit` + `cn/daily` | 原始 涨停价/跌停价 区间 + 锁定标志(仅 A 股) |
| `security_groups` | `cn/meta/stock_basic` | 用于行业中性化的按标的行业标签(当前快照) |

```bash
# writes market=CN partitions under $ASSAY_DATA_DIR
python scripts/prepare_cn.py 2010-01-01 2026-06-27
```

复权在读取时从原始事件重新计算(与美股相同的前向
机制),因此 `adj="split"` 仅针对 送转 复权,而
`adj="total"` 额外加入现金分红。

## 2. 组合回测中的交易约束

执行层(`assay.portfolio.execution.ExecutionSimulator`)在
`PortfolioBacktestConfig.market == "A"` 且数据存在时强制执行
A 股规则。这三项现在都已从标准存储接入:

- **每日涨跌停(涨跌停)。** 买入正处于涨停的标的、以及卖出
  正处于跌停的标的都会**零成交**(`blocked_reason` 为 `limit_up` /
  `limit_down`)。回测器从 `trade_status` 自动加载**真实的按板块
  区间**(主板 10%、科创/创业板 20%、北交所 30%、ST 5%),而非
  统一百分比。涨跌停价是原始价而价格面板是复权后的,因此每个
  区间在比较前会通过 `adj_close / raw_close` 重新基准化到面板基准 —
  参见 `PortfolioBacktester._cn_limit_matrices`。
- **T+1 结算。** 在某次调仓中买入的股票不能在同一步卖出
  (`blocked_reason` 为 `t_plus_1`)。对 `market == "A"` 经由预设激活。
- **停牌(停牌)。** 停牌的标的根本没有 `price_raw` K 线,因此其价格
  为 NaN,执行器将其视为不可交易(`blocked_reason` 为 `suspended`)。
  无需额外数据。

`A` 预设还应用 A 股的**成本模型**:**0.05% 印花税**
(卖出;2023-08-28 从 0.1% 下调)、0.03% 佣金(最低 ¥5)、0.002% 过户
费、√-冲击 `k=0.20`。

其他市场正确性默认值(预设 `'A'`):

- **强制只做多。** `market=='A'` 且 `long_short=True` 会报错 — A 股
  短 卖(融券)受限/不可用,因此空头账本不可执行。
- **总回报基准。** 引擎的复权基准由 `market` 派生
  (A/HK → `total` = 分红再投资,是 alpha 盈亏的正确基准;美股 →
  `split`),因此它绝不会在评估缓存中悄然混淆两次运行。
- **行业中性化**使用真实行业标签:设置 `sector_neutral=True`,
  回测器就会从存储自动加载 `security_groups`(Tushare
  `industry`);信号中性化器与行业权重约束随即
  启用。不设置时,运行绝不会被悄然做行业平衡。

## 3. 运行一次 CN 回测

```python
import datetime as dt
from assay.config import AssayConfig
from assay.data.store.datastore import DataStore
from assay.portfolio import PortfolioBacktestConfig, PortfolioBacktester

# A CN DataStore reads market=CN partitions; the 'A' preset drives trading rules.
store = DataStore(AssayConfig(market="CN", data_dir=AssayConfig.from_env().data_dir))
bt = PortfolioBacktester(store=store)

cfg = PortfolioBacktestConfig.preset(
    "A", universe="CSI300",
    period_start="2022-01-01", period_end="2023-12-31",
    rebalance_type="monthly", weight_method="signal_prop",
)
report = bt.run("ts_delta(close, 20) / delay(close, 20)", cfg)   # a momentum factor
print(report.total_return, report.sharpe, report.a_share_metrics)
```

两个市场旋钮,有意分开:

- `AssayConfig.market="CN"` 选择存储读取哪个**数据分区**。
- `PortfolioBacktestConfig.market="A"` 选择**交易规则体制**
  (涨跌停 / T+1 / 成本模型)。`report.a_share_metrics` 报告 `n_limit_hits`、
  `n_forced_holds`(T+1)、`n_blocked_suspended` 及对应比率。

CN 可用股票池:`CSI300`、`CSI500`、`CSI1000`(点对点、
无幸存者偏差)。

## 4. 香港

HK 沿用同一套标准存储,置于 `market=HK` 下,由
`assay.data.tushare.ingest.prepare_hk`(CLI:`scripts/prepare_hk.py`)写入。
它需要有相应权限的 Tushare 接口 — 见 [tushare.md](tushare.md)。

| 存储 | 来源 | 说明 |
|-------|--------|-------|
| `price_raw` | `hk/daily` | 未复权 OHLCV(`volume` 单位为**股**,而非手) |
| `adj_events` | `hk/adj_factor` | 由 `cum_adjfactor` 的跳变重建,并与 `pre_close` 相互印证 |
| `universe_snapshots` | `hk/daily` 成交额 + `hk_basic` ISIN | **HK100** 与 **HSHARE100** 点对点成分 |
| `trade_status` | — | 不写入:港交所没有每日涨跌停区间 |
| `security_groups` | — | 不写入:Tushare 未提供 HK 行业分类 |

```bash
# writes market=HK partitions under $ASSAY_DATA_DIR_HK (default ./data_hk)
python scripts/prepare_hk.py 2015-01-01 2026-07-27
```

**HK100** 是覆盖整个港股大盘的股票池。它在每个月末按过去三个月的成交额,
对所有真正有成交的标的排序,取前 100 名,并在*下*一个月的第一个交易日生效 —
因此一份快照永远看不到用来给它打分的收益。

**HSHARE100** 把完全相同的排名规则只施加于 **H 股** — 即在港交所上市、
注册地却在中国内地的发行人,区别于构成大盘其余部分的红筹股
(注册于开曼/百慕大的控股公司)与本地香港发行人。成分由 ISIN 的国家前缀
(`CNE`)判定,那是发行人注册地的属性,因此永远不会改变;由于上市列表在
下载时就已包含退市标的,这个池子不存在幸存者偏差。两个股票池大约有一半
重叠 — 即那些同时也位列大盘成交最活跃标的之中的 H 股。

当命题关乎内地企业(A/H 两地上市、内地政策敞口、港股通资金流)时,
请用 `HSHARE100`;当命题关乎整个香港市场时,请用 `HK100`。`HSI` 与
`HSTECH` 也已注册,但它们是供应商提供的**当前**快照,因而存在幸存者偏差。

`HK` 预设在每一个要紧之处都与 `A` 不同:

- **没有涨跌停,没有 T+1。** 港交所两者皆无,因此 `price_limit_pct=None`
  且 `t_plus_1=False`;涨跌停与结算这两道闸门根本不会启用。
- **允许做空。** 与 `A` 不同,`long_short=True` 是被接受的。
- **印花税双边收取。** 港交所对买入与卖出同样按 0.10% 征收,因此
  `TransactionCostModel` 在 `market == "HK"` 时对称收取 — A 股的 印花税 仍
  只在卖出侧收取。每一边预设合计 0.1484%:经纪佣金 0.04% + 印花税 0.10% +
  港交所交易费 0.00565% + 证监会征费 0.0027% + 财汇局征费 0.00015%。
- **总回报基准**,与 `A` 相同,并且现在确实由已导入的 `adj_events` 支撑。

```python
store = DataStore(AssayConfig(market="HK", data_dir=Path(os.environ["ASSAY_DATA_DIR_HK"])))
cfg = PortfolioBacktestConfig.preset(
    "HK", universe="HK100",       # or "HSHARE100" for H-shares only
    period_start="2022-01-01", period_end="2023-12-31",
)
```

HK 特有的两点注意:**无法做行业中性化**(没有分组标签),以及 NAV 以
**HKD** 计价且没有汇率账本,因此一次 HK 运行无法与美股或 CN 的运行直接
比较。
