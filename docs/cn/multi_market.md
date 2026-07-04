# 多市场支持(中国 A 股)

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
无幸存者偏差)。香港指数序列会被下载,但受该 token 的
限制,HK 没有个股价格历史或成分 API — 见
[tushare.md](tushare.md)。
