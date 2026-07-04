# Assay — 组合回测设计

**版本：** 0.1 · 草案 → **已实现**（市场无关核心）
**范围：** 因子驱动的组合模拟，完整支持 A 股约束
**依赖：** Assay FactorEngine、DataStore PIT 层、评估器

> **实现状态（2026-06）：✅ 已实现**，位于 [`src/assay/portfolio/`](../../../src/assay/portfolio/)
> （config、report、signal、rebalance、weights、constraints、costs、execution、accounting、
> metrics、backtester），并接入 `AssayService.backtest_portfolio`、SDK
> （`assay.backtest_portfolio`）、REST `POST /v1/portfolio/backtest` 和 CLI `assay portfolio`。
> 745+ 测试通过；一个真实的 NASDAQ-100 回测可端到端运行（见[使用指南](../guide/portfolio-backtest.md)）。
>
> **落地于现实。** 市场无关的核心目前运行在我们唯一拥有的数据上——
> **美股（NASDAQ-100，OHLCV）**。A 股的*价格机制*规则（T+1 结算、
> 涨跌停阻断、印花税/佣金/过户费成本模型）是**当其输入被提供时才激活**的配置。A 股的*数据依赖*
> 过滤器（ST 过滤、停牌处理、指数调整规避、北向资金、行业中性）作为可选输入被搭建好，当那些数据
> 不在存储中时会**优雅地空操作**。基准是一个**等权股票池代理**（Assay 没有指数价格序列）。
> 状态图例：✅ 已实现 · 🔶 仅在提供正确输入时激活 · 📋 需要我们缺失的数据。

---

## 1. 概述

组合回测回答一个 IC 分析无法回答的问题：*给定一个因子信号、真实的
交易约束和市场成本，可实现的净收益是多少？* 该模块消费一个因子**表达式**（重新求值为
`(T, N)` 矩阵），并在评估期内模拟建仓、再平衡、计费和盯市，产出一个
结构化的 `PortfolioReport`。

### 1.1 流水线

```
factor expression
    │  evaluate -> (T, N) factor matrix + close/open prices
    ▼
UniverseFilter        eligible = finite factor & finite price (& tradable mask)   ✅
    ▼
SignalProcessor       winsorize · neutralize · normalize (rank/zscore/raw)         ✅
    ▼
RebalanceScheduler    calendar · threshold · signal-autocorr                       ✅
    ▼
WeightConstructor     equal · signal_prop · quantile · mean-variance · risk-parity ✅
    ▼
ConstraintApplicator  position/count caps · turnover cap · gross/net · sector-neut ✅ / 🔶
    ▼
ExecutionSimulator    slippage · ADV cap · price limits · T+1 settlement           ✅ / 🔶
    ▼
PortfolioAccountant   weight drift · daily MTM NAV · costs · trade/position log    ✅
    ▼
PortfolioReport       Sharpe · drawdown · turnover · cost drag · benchmark · A-share ✅
```

优化再平衡器使用 **scipy**（`scipy.optimize.minimize`，SLSQP）和一个 numpy
Ledoit-Wolf 收缩——`cvxpy`/`scikit-learn` 被有意*排除*在依赖之外。

---

## 2. 配置

所有参数都位于一个 `PortfolioBacktestConfig`（72 个字段）上。`PortfolioBacktestConfig.preset('US'|'A'|'HK')`
应用市场表（§6）的成本/限价默认值。标记 🔶 的字段是 A 股价格机制
（在提供正确输入时激活）；📋 需要存储中缺失的 A 股数据。

**评估期** — `period_start`、`period_end`、`oos_split_date`、`warmup_days`

**股票池与市场** — `universe`（默认 `NASDAQ100`）、`market`（默认 `US`）、
`custom_symbols`、`as_of_date`、`include_delisted`

**再平衡** — `rebalance_type`（daily/weekly/monthly/quarterly/threshold/signal）、
`rebalance_day`、`threshold_rank_shift`、`threshold_weight_drift`、`signal_autocorr_floor`、
`min_rebalance_interval`、`execution_offset_days`（≥1；0 是前视偏差）

**权重构建** — `weight_method`（equal/signal_prop/mv/risk_parity/quintile/decile/bl）、
`long_short`、`gross_exposure`、`net_exposure`、`signal_transform`、`quintile_long_n`、
`quintile_short_n`、`mv_risk_aversion`、`cov_window`、`cov_method`、`bl_tau`

**约束** — `max_single_weight`、`min_single_weight`、`max_sector_weight` 📋、
`sector_neutral` 📋、`market_neutral`、`max_turnover_per_period`、`max_annual_turnover`、
`min_stock_count`、`max_stock_count`、`benchmark_tracking_err`、`capacity_adv_limit`

**A 股（§2.6）** — 价格机制 🔶：`t_plus_1`、`price_limit_pct`、`star_chinext_limit`、
`enforce_limit_price`、`stamp_duty_rate`、`commission_rate`、`commission_min`、
`transfer_fee_rate`；数据依赖 📋：`st_filter`、`new_listing_lockout_days`/`ipo_lockout_days`、
`suspend_handling`、`rebalance_around_index`、`inclusion_anticipation`、`northbound_flow_filter`、
`sz_sh_connect_only`

**执行** — `execution_price`（next_open/next_close/vwap/arrival）、`slippage_model`
（sqrt/linear/zero/almgren_chriss）、`slippage_k`、`adv_window`、`max_adv_fraction`、
`partial_fill_handling`（defer/cancel/force）、`include_bid_ask`

**基准与归因** — `benchmark`（index→等权代理/cash/custom/none）、
`benchmark_symbol`、`risk_free_rate`、`attribution_model`、`attribution_factors`

**输出** — `output_frequency`、`save_trade_log`、`save_position_log`、`compute_attribution`、
`bootstrap_sharpe`、`n_bootstrap`

---

## 3. 再平衡算法

| 类型 | 何时使用 | 典型年换手率 |
|---|---|---|
| `daily` | 快速衰减因子（半衰期 < 5d） | 400–1200% |
| `weekly` | 半衰期 5–20d（动量 / 反转） | 100–400% |
| `monthly` | 半衰期 20–60d（多数基本面/质量） | 50–150% |
| `quarterly` | 半衰期 > 60d（价值、资产负债表） | 20–60% |
| `threshold` | 稳定因子；按排名漂移或权重漂移再平衡 | 视情况 |
| `signal` | 当因子排名自相关跌破下限时再平衡 | 视情况 |

`min_rebalance_interval` 对每种类型都强制执行。*把再平衡频率匹配到
因子的半衰期，而非技术上可能的最大频率。*

---

## 4. 绩效指标

**收益** — 总收益、年化（×252/T）、超额（对基准）、毛（成本前）、净。
**风险调整** — Sharpe、Sortino、Calmar、信息比率、最大回撤（+起点/终点/恢复）、
beta、CAPM alpha、跟踪误差。
**换手与成本** — 单边 / 年换手率、成本拖累（毛 − 净）、隐含平均持仓天数。
**A 股（`market='A'`）** — 涨跌停命中率、强制持有比率（T+1）、停牌影响、
ST 敞口天数、指数调整 alpha、北向资金相关性。*（仅在
底层输入存在时填充；在美股数据上只有涨跌停命中/强制持有有意义，且处于惰性状态。）*

所有年化均使用 252 个交易日。

---

## 5. PortfolioReport schema

镜像 `FactorReport`：供 agent 消费的机读 JSON。恒等标识（`run_id`
= factor_id + config 哈希的 sha256[:12]、`factor_id`、`config`）；区间（`period_start/end`、
`n_trading_days`、`n_rebalances`）；收益（`total/annual/gross/excess_return`）；风险
（`sharpe`、`sortino`、`calmar`、`information_ratio`、`max_drawdown` + `_start`/`_end`/
`drawdown_recovery_days`、`beta`、`alpha_capm`、`tracking_error`）；成本（`annual_turnover`、
`cost_drag`、`avg_holding_days`）；明细（`nav_series`、`nav_dates`、`benchmark_series`、
`monthly_returns`、`trade_log`、`position_log`、`attribution`、`a_share_metrics`）；溯源
（`lineage.{data_snapshot, eval_timestamp, adj_version}`）。`to_dict()`/`to_json()` 是 JSON 安全的
（NaN→None）；`from_dict()` 可往返。

---

## 6. 市场对比

| 参数 | A 股（SSE/SZSE） | 美股（NYSE/NASDAQ） | 港股（HKEX） |
|---|---|---|---|
| 结算 | T+1 | T+2 | T+2 |
| 涨跌停 | ±10%（主板）、±20%（科创） | 无 | 无 |
| 印花税 | 卖出 0.10% | 0% | 双边 0.13% |
| 佣金 | 双边 0.03%，最低 ¥5 | 0.01–0.05% | 0.03–0.05% |
| 过户费 | 0.002%（仅 SSE） | 0% | 0.003% |
| 指数再平衡 | 半年一次（6 月/12 月） | 季度 | 季度 |
| 行业分类 | CSRC | GICS | GICS / 恒生 |
| 市场冲击 `k` | 0.20 | 0.10 | 0.15 |

当前数据只演练了**美股**列。A/港股预设已完整规定，
并在对应市场数据被摄取时激活。

---

## 7. 正确性说明

- **生存者偏差** — 下载股票池是区间上的 PIT 并集
  （包含退市/剔除的名称）；绝不要用今天的成分做历史回测。
- **公司行为对齐** — `adj_factor` 是版本化的，并按回测日期 as-of 查询。
  （当前数据集没有 `adj_events`，因此少数真实拆股未经复权而残留——见
  [使用指南](../guide/portfolio-backtest.md) 的注意事项；回填公司行为可修复。）
- **成本 = 费率 × 名义** — 成本模型返回一个*费率*；执行器乘以
  交易权重比例。（此确切缺陷的一个回归测试已被捕获并修复。）

---

*原始 Word 源文件与本文件并存，名为 `portfolio-backtest.docx`。*
*— Assay · Portfolio Backtest Design · AlphaBench Project —*
