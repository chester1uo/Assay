# Assay 文档

Assay 是一个点对点(PIT)正确的、面向 agent 原生的因子回测引擎,用于 LLM 驱动的 alpha
挖掘。本目录包含**设计规范**与**使用指南**。

初次使用?请从 **[快速开始](guide/getting-started.md)** 开始。

## 使用指南 — `guide/`

| 指南 | 内容 |
|---|---|
| [快速开始](guide/getting-started.md) | 安装 → 配置 → 导入 → 第一个因子与组合回测 |
| [数据管线](guide/data-pipeline.md) | MASSIVE 加载器、`prepare-nasdaq100`、parquet 存储、数据目录与复权注意事项 |
| [Python SDK](guide/python-sdk.md) | `backtest`、`Session`、`batch_backtest`、`library`、`stream`、`backtest_portfolio` |
| [CLI](guide/cli.md) | 每一个 `python -m assay.cli` 子命令 |
| [REST API](guide/rest-api.md) | `/v1/*` 端点、SSE 流式、lint、鉴权 |
| [MCP 服务器](guide/mcp-server.md) | 11 个 agent 工具与客户端配置 |
| [WebUI](guide/webui.md) | 由 FastAPI 提供的零安装 web 应用 |
| [因子合成](guide/factor-combination.md) | 将多个因子混合为一个复合因子;训练/验证/测试;解析式、优化式与 ML 方法 |
| [组合回测](guide/portfolio-backtest.md) | 配置、运行、指标、A 股说明 |
| [性能](guide/performance.md) | Alpha-101 有缓存 vs 无缓存基准测试 |
| [预计算与 CSE](guide/precompute-cse.md) | 挖掘公共子表达式、为所有资产预计算、加速批量扫描 |

## 设计规范 — `design/`

| 规范 | 范围 |
|---|---|
| [工程](design/engineering.md) | 系统架构、数据层、引擎、缓存、评估、性能模型 |
| [全栈架构](design/architecture.md) | 建立在同一共享引擎之上的四个界面(SDK · REST · WebUI · MCP) |
| [WebUI 设计](design/webui.md) | (目标为 React 的)web 应用的屏幕级设计 |
| [组合回测](design/portfolio-backtest.md) | 带 A 股约束的因子驱动组合模拟 |
| [算子兼容性](design/operator-compatibility.md) | qlib ↔ Assay ↔ Alpha-101 算子映射 |

## 报告 — `reports/`

- [Alpha-101 测试报告](reports/alpha101.md) — 101 个公式化 Alpha 目录的保真度。

---

### 状态约定

设计文档是**面向未来、但与现实保持一致的规范**,并附有状态徽章:
✅ 已实现 · 🔶 已实现但简化 / 仅在提供正确输入时激活 · 📋 计划中。
当前已构建的部分:数据层、因子引擎、IC/衰减评估器、因子库、
`AssayService` + Python SDK、REST API(+ SSE)、MCP 服务器、零安装 WebUI,以及
组合回测。已接入的股票池:**NASDAQ-100 / S&P 500**(美股,OHLCV — 无 `vwap`)和
**CSI300 / CSI500 / CSI1000**(中国 A 股,经由 Tushare)。
