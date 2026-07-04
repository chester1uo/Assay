# 组合回测

将因子信号转化为带有真实交易约束和成本的模拟组合，并得到一份
`PortfolioReport`（夏普、回撤、换手率、成本拖累、NAV 序列）。设计规范：
[portfolio-backtest.md](../design/portfolio-backtest.md)。

## 快速开始

```python
import assay
from assay.portfolio import PortfolioBacktestConfig

cfg = PortfolioBacktestConfig(
    universe="NASDAQ100", period_start="2025-01-02", period_end="2026-06-09",
    market="US",
    rebalance_type="monthly",        # daily|weekly|monthly|quarterly|threshold|signal
    weight_method="signal_prop",     # equal|signal_prop|quintile|decile|mv|bl|risk_parity
    long_short=False,
    max_single_weight=0.05,
    execution_price="next_open",     # next_open|next_close
)
pf = assay.backtest_portfolio("cs_rank(ts_returns(close, 20))", cfg)

print(pf.total_return, pf.annual_return, pf.sharpe, pf.max_drawdown,
      pf.annual_turnover, pf.cost_drag, pf.n_rebalances)
pf.to_dict()   # JSON-safe, agent-ready
```

CLI：

```bash
python -m assay.cli portfolio 'cs_rank(ts_returns(close, 20))' \
    --start 2025-01-02 --end 2026-06-09 --rebalance monthly --weight-method signal_prop
```

REST：`POST /v1/portfolio/backtest`（见 [REST 指南](rest-api.md)）。

## 多空、美元中性

```python
cfg = PortfolioBacktestConfig(
    universe="NASDAQ100", period_start="2025-01-02", period_end="2026-06-09",
    weight_method="quintile", long_short=True, net_exposure=0.0,   # dollar-neutral
    quintile_long_n=1, quintile_short_n=1,
)
pf = assay.backtest_portfolio("cs_rank(ts_corr(close, volume, 20))", cfg)
```

## 权重方法

| Method | Notes |
|---|---|
| `equal` | 在合格集合上等权 |
| `signal_prop` | 与（归一化后的）信号成比例 |
| `quintile` / `decile` | 顶部/底部分桶做多/做空 |
| `mv` | 均值-方差，经由 scipy SLSQP + numpy Ledoit-Wolf 收缩（不用 cvxpy/sklearn） |
| `risk_parity` | 逆波动率 / 等风险贡献（纯多头） |

## 关键配置分组

`rebalance_*`（调仓计划 + `min_rebalance_interval`、`execution_offset_days`）、权重
（`gross_exposure`、`net_exposure`、`signal_transform`）、约束（`max_single_weight`、
`max/min_stock_count`、`max_turnover_per_period`）、执行（`slippage_model`、`slippage_k`、
`max_adv_fraction`、`partial_fill_handling`）、基准（`benchmark`、`risk_free_rate`）、输出
（`save_trade_log`、`save_position_log`）。`PortfolioBacktestConfig.preset('US'|'A'|'HK')` 会应用
市场的成本/限制默认值。完整字段列表见[设计文档](../design/portfolio-backtest.md) §2。

## 报告中的指标

收益（total/annual/gross/excess）、风险调整（夏普、索提诺、卡玛、信息比率、
最大回撤 + 恢复、beta、CAPM alpha、跟踪误差）、换手率与成本（年化换手率、
成本拖累、平均持有天数），外加 `nav_series`/`nav_dates`、`monthly_returns`、`trade_log`、
`position_log` 以及 `a_share_metrics`（仅当 `market='A'` 时）。

## A 股

设置 `market="A"`（或 `PortfolioBacktestConfig.preset("A")`）以启用 T+1 结算、涨跌停
拦截以及印花税/佣金/过户费成本模型。当其输入被提供时，这些会在任何市场的数据上运行
（价格限制用 `prev_close`，ST/停牌用 `tradable_mask`，等等）。在美股数据上，
依赖数据的过滤器处于惰性状态（不会崩溃）。真正的指数基准需要 Assay 没有的指数价格序列——
基准使用等权股票池代理。

## 当前数据集的注意事项

- **未复权拆股。** 如果数据文件夹没有 `adj_events`，少数真实拆股会表现为
  约 −90% 的单日收益，从而虚增结果。回填公司行动
  （`assay.cli corp-actions`）以获得干净的数值——见[数据管理指南](data-pipeline.md)。
- **`rebalance_type='signal'`** 配合高自相关因子可能永远不会触发（符合规范），
  留下一条全现金的平坦 NAV——请选择日历调仓计划以获得非退化的运行。
