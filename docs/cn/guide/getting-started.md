# 快速开始

Assay 在时点正确的数据上评估因子表达式：IC、RankIC、衰减、换手、冗余度，以及带成本的组合回测。有两种用法：直接用线上站点，或者自己部署。

## 使用线上站点

[assay.qfinverse.com](https://assay.qfinverse.com) 跑在 QFinVerse 的美股日频数据上（2003 年起，股票池 TOP500 / TOP1000 / TOP3000、SP500、NASDAQ100）。数据包含什么见[美股数据（QFinVerse）](qfv-data.md)。

1. **拿到邀请。** 只能凭邀请注册。向管理员要一个链接，打开后设置用户名和密码（至少 10 位）。
2. **登录。** 在 `/login` 登录，登录状态保持 7 天。
3. **评估因子。** 打开「单因子测试」，在顶部横栏选好股票池和区间，输入表达式，例如 `-ts_delta(close, 5) / ts_delay(close, 5)`，点「评估」。好的因子可以存进因子库。详见 [WebUI 指南](webui.md)。
4. **创建 API 密钥**，给脚本和 agent 用：「账户」→「API 密钥」→「创建密钥」。密钥（`asy_...`）只显示一次，代表你本人，不用时撤销。
5. **调用 REST API**，把密钥放在 `X-API-Key` 请求头里：

```bash
export ASSAY_KEY=asy_...
curl -s -X POST https://assay.qfinverse.com/v1/factor/evaluate \
  -H "X-API-Key: $ASSAY_KEY" -H 'Content-Type: application/json' \
  -d '{"expr": "-ts_delta(close, 5) / ts_delay(close, 5)", "universe": "SP500",
       "period": ["2019-01-01", "2026-10-30"], "horizons": [1, 5, 10, 20]}'
```

全部接口见 [REST API 指南](rest-api.md)；站点的 `/docs` 是可交互的接口文档。

## 自己部署

### 1. 安装

从源码安装（没有 PyPI 版本）：

```bash
git clone https://github.com/QFinVerse/Assay.git && cd Assay
pip install -e ".[perf,api,mcp]"      # perf = numba + scipy，评估快很多
```

不安装的话，加 `PYTHONPATH=src` 也能直接运行。

### 2. 配置数据

配置都是环境变量，由 `AssayConfig.from_env()` 读取。导入时会加载项目里的 `.env`，shell 里的变量优先。参见 [.env.example](../../../.env.example)。

- **美股，读 QFinVerse 日频面板**（推荐）：把 `ASSAY_QFV_PANEL_DIR` 设为面板根目录，不需要再导入数据。见[美股数据（QFinVerse）](qfv-data.md)。
- **美股，读本地 MASSIVE 镜像**：设置 `MASSIVE_DATA_DIR`，然后生成 Parquet 存储：

```bash
export MASSIVE_DATA_DIR=/data/massive_data   # 本地镜像，不需要凭证
export ASSAY_DATA_DIR=data                   # 存储写到这里
python -m assay.cli prepare-nasdaq100 --start 2025-01-01 --end 2026-06-09
python -m assay.cli status
```

- **A 股和港股**来自 Tushare，见[多市场](../multi_market.md)和 [Tushare](../tushare.md)。

MASSIVE 日线只有 OHLCV 和成交笔数，没有 `vwap` 字段。

### 3. 评估一个因子

命令行：

```bash
python -m assay.cli parse 'cs_rank(ts_corr(close, volume, 20))'      # 只检查语法，不需要数据
python -m assay.cli run   'cs_rank(ts_corr(close, volume, 20))' \
    --start 2025-01-02 --end 2026-06-09
```

Python（见 [SDK 指南](python-sdk.md)）：

```python
import assay
report = assay.backtest(
    "cs_rank(ts_corr(close, volume, 20))",
    universe="NASDAQ100", period=("2025-01-02", "2026-06-09"),
)
print(report.rank_ic, report.rank_icir, report.decay_halflife_days, report.failure_mode)
```

`FactorReport` 包含 `ic` / `rank_ic` / `icir` / `rank_icir`、`ic_by_horizon`、衰减、换手、冗余度、前视检查、改进建议 `suggestion` 和结构化的 `diagnostics`。`.to_dict()` 可以直接转 JSON。

### 4. 组合回测

```python
from assay.portfolio import PortfolioBacktestConfig
cfg = PortfolioBacktestConfig(universe="NASDAQ100",
                              period_start="2025-01-02", period_end="2026-06-09",
                              rebalance_type="monthly", weight_method="signal_prop")
pf = assay.backtest_portfolio("cs_rank(ts_returns(close, 20))", cfg)
print(pf.sharpe, pf.max_drawdown, pf.annual_turnover, pf.cost_drag)
```

见[组合回测指南](portfolio-backtest.md)。

### 5. 启动服务

```bash
python -m assay.cli serve-api --port 8000     # REST API、WebUI 和 Doc Hub，端口 8000
python -m assay.cli serve-mcp                 # MCP 服务（stdio），给 agent 用
```

服务默认不需要登录。要求登录时设置 `ASSAY_ACCOUNTS=1`，再用状态目录里的 setup token 创建第一个管理员，见 [REST API → 鉴权](rest-api.md)。

## 接下来

| 指南 | 内容 |
|---|---|
| [WebUI](webui.md) | 控制台：页面、操作、账户 |
| [美股数据（QFinVerse）](qfv-data.md) | 股票池、证券 ID、拆股与分红 |
| [数据与复权](data-and-adjustment.md) | 价格如何复权、如何按时点读取 |
| [数据流水线](data-pipeline.md) | MASSIVE 加载器、`prepare-nasdaq100`、数据目录 |
| [Python SDK](python-sdk.md) | `backtest`、`Session`、`batch_backtest`、`library`、`backtest_portfolio` |
| [CLI](cli.md) | 全部 `python -m assay.cli` 子命令 |
| [REST API](rest-api.md) | `/v1/*` 和 `/auth/*` 接口、SSE 流式、鉴权模式 |
| [MCP 服务](mcp-server.md) | agent 工具和客户端配置 |
| [组合回测](portfolio-backtest.md) | 配置、运行、指标、A 股说明 |
| [性能](performance.md) | Alpha-101 缓存基准 |
