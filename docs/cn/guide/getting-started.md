# 快速开始

Assay 是一个点对点（PIT）正确、面向智能体（agent-native）的因子回测引擎。本指南将带你
从一次干净的检出（checkout）走到你的第一个打分因子和一次组合回测。

## 1. 安装

采用 src 布局的包；从源码安装（尚无 PyPI 发布版本）：

```bash
git clone <repo> && cd Assay
pip install -e .            # core deps
# optional extras (all already vendored in this environment):
pip install -e ".[perf,api,mcp]"
```

如果你没有执行 `pip install -e .`，一切都可以通过 `PYTHONPATH=src` 运行。

## 2. 配置数据源与输出目录

配置基于环境变量，由 `AssayConfig.from_env()` 加载。项目级 `.env` 在导入时被读取（shell
环境变量始终优先）。该流水线读取一个**本地**的 MASSIVE 镜像 —— 无需凭证。参见
[.env.example](../../../.env.example)：

```bash
MASSIVE_DATA_DIR=/data/massive_data    # root of the local MASSIVE mirror (source)
ASSAY_DATA_DIR=data                    # parquet store root (point at any folder)
```

> 美股（MASSIVE —— NASDAQ-100 / S&P 500）和中国 A 股（Tushare —— 沪深300/500/1000）
> 均已接入。捆绑的美股数据为 **OHLCV + 成交笔数 —— 没有 `vwap`**。

## 3. 传输数据

将本地镜像转换为一个点对点的 NASDAQ-100 数据集（股票池 + 公司行为 + 价格），存放在
`ASSAY_DATA_DIR` 所指向的文件夹下。详见[数据流水线指南](data-pipeline.md)。

```bash
python -m assay.cli prepare-nasdaq100 --start 2025-01-01 --end 2026-06-09
python -m assay.cli status                      # what's ingested
```

## 4. 评估一个因子

**CLI** —— 先做一次快速的 AST + 面板检查，再生成一份完整的打分报告：

```bash
python -m assay.cli parse 'cs_rank(ts_corr(close, volume, 20))'      # no data needed
python -m assay.cli run   'cs_rank(ts_corr(close, volume, 20))' \
    --start 2025-01-02 --end 2026-06-09
```

**Python SDK** —— 参见 [SDK 指南](python-sdk.md)：

```python
import assay
report = assay.backtest(
    "cs_rank(ts_corr(close, volume, 20))",
    universe="NASDAQ100", period=("2025-01-02", "2026-06-09"),
)
print(report.rank_ic, report.rank_icir, report.decay_halflife_days, report.failure_mode)
```

一份 `FactorReport` 包含 `ic`/`rank_ic`/`icir`/`rank_icir`、`ic_by_horizon`、衰减、换手率、
冗余度、前视（look-ahead）检测、一段自然语言 `suggestion`，以及结构化的
`diagnostics`。`.to_dict()` 是 JSON 安全的，可供智能体消费。

## 5. 运行一次组合回测

```python
from assay.portfolio import PortfolioBacktestConfig
cfg = PortfolioBacktestConfig(universe="NASDAQ100",
                              period_start="2025-01-02", period_end="2026-06-09",
                              rebalance_type="monthly", weight_method="signal_prop")
pf = assay.backtest_portfolio("cs_rank(ts_returns(close, 20))", cfg)
print(pf.sharpe, pf.max_drawdown, pf.annual_turnover, pf.cost_drag)
```

参见[组合回测指南](portfolio-backtest.md)。

## 6. 启动各个界面

```bash
python -m assay.cli serve-api --port 8000     # REST API + WebUI at http://localhost:8000
python -m assay.cli serve-mcp                 # MCP server (stdio) for LLM agents
```

- **WebUI** —— 打开 `http://localhost:8000`（参见 [WebUI 指南](webui.md)）。
- **REST API** —— 交互式文档位于 `/docs`；参见 [REST 指南](rest-api.md)。
- **MCP** —— 11 个智能体工具；参见 [MCP 指南](mcp-server.md)。

## 接下来去哪

| 指南 | 涵盖内容 |
|---|---|
| [数据流水线](data-pipeline.md) | MASSIVE 加载器、`prepare-nasdaq100`、数据文件夹、注意事项 |
| [Python SDK](python-sdk.md) | `backtest`、`Session`、`batch_backtest`、`library`、`stream`、`backtest_portfolio` |
| [CLI](cli.md) | 每一个 `python -m assay.cli` 子命令 |
| [REST API](rest-api.md) | `/v1/*` 端点、SSE 流式、鉴权 |
| [MCP 服务器](mcp-server.md) | 智能体工具与客户端设置 |
| [WebUI](webui.md) | 零安装的 Web 应用 |
| [组合回测](portfolio-backtest.md) | 配置、运行、指标、A 股说明 |
| [性能](performance.md) | Alpha-101 缓存 vs 无缓存基准测试 |
