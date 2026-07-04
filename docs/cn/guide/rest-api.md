# REST API

一个 FastAPI 应用，通过 HTTP 暴露 `AssayService`，并为评估提供 SSE 流式。运行它：

```bash
python -m assay.cli serve-api --port 8000      # or: uvicorn assay.api.app:app --port 8000
```

- 基址：`http://localhost:8000` · 所有数据路由位于 `/v1` 之下
- 交互式文档（Swagger）：`http://localhost:8000/docs`
- WebUI 服务于 `/`（参见 [WebUI 指南](webui.md)）。

## 鉴权

可选的 API-key 鉴权。设置 `ASSAY_API_KEYS`（逗号分隔）即要求提供 `X-API-Key` 请求头；
**未设置 = 开放**（开箱即用）。同源的 WebUI 无需密钥。

## 端点

| 方法 | 路径 | 用途 |
|---|---|---|
| `GET`  | `/health` | 存活检查 |
| `POST` | `/v1/factor/evaluate` | 评估一个因子 —— 返回 JSON 报告，或在 `stream:true` 时返回 SSE 流 |
| `POST` | `/v1/factor/batch` | 批量评估 → `{total, elapsed_ms, reports}` |
| `POST` | `/v1/factor/lint` | 仅解析：`{dialect, canonical, fields, operators, ast, diagnostics}`（无需数据） |
| `GET`  | `/v1/library/factors` | 带过滤条件的列表（`min_rank_icir`、`source`、`sort_by`、`limit`、...） |
| `GET`  | `/v1/library/factors/{id}` | 完整的 `FactorReport` |
| `POST` | `/v1/library/factors` | 保存一份报告 |
| `DELETE` | `/v1/library/factors` | 按 `{factor_ids: [...]}` 删除 |
| `GET`  | `/v1/library/correlation-matrix` | 对 `factor_ids` 的两两相关性 |
| `GET`  | `/v1/library/ic-heatmap` · `/embedding` · `/lineage` | RankIC 随时间、二维相似度图、派生 DAG |
| `POST` | `/v1/library/factors/bulk` | 批量评估 + 保存表达式 |
| `POST` | `/v1/library/prune` | 识别/移除冗余因子 |
| `GET`  | `/v1/combination/methods` | 列出合成方案（可用的已学习模型会被标记） |
| `POST` | `/v1/combination` | 合成因子，对训练/验证/测试打分 → 复合因子 + 模型 |
| `POST/GET/DELETE` | `/v1/combination/saved` | 保存 / 列出 / 删除已保存的合成运行 |
| `GET`  | `/v1/combination/saved/{id}` | 重新加载一个已保存的运行（拟合好的模型） |
| `GET`  | `/v1/market/bars` | 单个标的的 OHLCV K 线（`freq`、`adj`） |
| `POST` | `/v1/market/factor-series` | 单个标的上某一因子的取值随时间序列 |
| `POST` | `/v1/session/create` · `DELETE /v1/session/{id}` | 创建 / 释放一个面板缓存会话 |
| `POST` | `/v1/portfolio/backtest` | 运行一次组合回测 → `PortfolioReport` |
| `GET`  | `/v1/system/status` · `/universes` · `/data-calendar` | 引擎/数据/缓存状态、股票池、覆盖范围 |
| — | **数据管理器**（`/v1/admin/*`，运维） | 见下文 |

### 数据管理器（管理员）端点

位于 WebUI *Data* 标签页背后的运维界面（从 OpenAPI schema 中隐藏，但实际可用）：

| 方法 | 路径 | 用途 |
|---|---|---|
| `GET/PUT` | `/v1/admin/config` | 读取（脱敏）/ 更新目录、凭证、系统设置 |
| `GET`  | `/v1/admin/data/status` · `/usage` | RAW↔ASSAY 同步快照 · 各市场磁盘占用（RAW + ASSAY） |
| `POST` | `/v1/admin/data/test` | 测试一个数据提供方连接（`{provider: massive\|tushare}`） |
| `POST` | `/v1/admin/data/jobs` | 入队一个任务（`mode: init\|update\|ingest`）；`GET` 列表；`GET /{id}` 单个 |
| `GET/PUT` | `/v1/admin/schedule` | 自动更新计划（按市场：启用 + 每日时间） |
| `GET`  | `/v1/admin/cache/status` · `/entries` · `POST /rebuild` | 热缓存状态 / 内容 / 重建 |

## 评估 —— 阻塞式

```bash
curl -s -X POST http://localhost:8000/v1/factor/evaluate \
  -H 'Content-Type: application/json' \
  -d '{"expr":"ts_corr(close, volume, 20)","universe":"NASDAQ100",
       "period":["2025-01-02","2026-06-09"],"horizons":[1,5,10,20]}'
# -> { ...FactorReport }
```

## 评估 —— SSE 流

设置 `"stream": true`；响应为 `text/event-stream`，包含以下帧：

```
event: eval.started   data: {"factor_id": "...", "expr": "..."}
event: eval.ic_series data: {"ic": [...], "rank_ic": [...], "dates": [...]}
event: eval.decay     data: {"ic_by_horizon": {...}, "halflife": 12}
event: eval.groups    data: {"quintile_returns": [...]}
event: eval.complete  data: { ...full FactorReport }
```

所有数据都是 NaN 安全的 JSON（NaN/Inf → null），因此 `JSON.parse` 永不失败。`EventSource`
无法 POST —— 请使用 `fetch` + 一个 `ReadableStream` reader 来消费该流（WebUI 就是这么做的）。

## 组合回测

```bash
curl -s -X POST http://localhost:8000/v1/portfolio/backtest \
  -H 'Content-Type: application/json' \
  -d '{"expr":"cs_rank(ts_returns(close, 20))",
       "config":{"universe":"NASDAQ100","period_start":"2025-01-02","period_end":"2026-06-09",
                 "rebalance_type":"monthly","weight_method":"signal_prop"}}'
```

## 错误 schema

错误映射自引擎诊断（`assay.engine.diagnostics`）：稳定的 `code`
（`ASSAY-P###/E###/O###`）、`failure_mode`（`SYNTAX_ERROR · LOOKAHEAD · CONSTANT · ALL_NAN ·
RUNTIME_ERROR`）、`severity`、`stage`、`message`、`location`、`suggestion`。当数据存储为空时，
一个需要已导入数据的请求会返回 **503**。

完整契约：[architecture.md](../design/architecture.md) §4。
