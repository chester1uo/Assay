# WebUI

一个**零安装、零构建**的 web 应用，由 FastAPI 后端直接提供服务——原生 JS +
手写 SVG 图表，无框架、无打包器、无 CDN。位于
[`src/assay/api/static/`](../../../src/assay/api/static/)。

> [WebUI 设计文档](../design/webui.md)中的生产级 React/Vite/Recharts/Monaco 技术栈
> 是文档记载的目标；这个已交付的 UI 是可运行的等价实现（构建环境中没有 npm）。

## 运行

```bash
python -m assay.cli serve-api --port 8000
# open http://localhost:8000
```

它会自动采用已导入的数据区间作为默认求值周期（来自
`/v1/system/status`），因此因子在确实有数据的日期上求值。升级后请强制刷新
以获取新资源。

## 界面

- **Dashboard**——数据状态栏、KPI 卡片、因子排行榜、数据日历热力图。
- **Factor Library**——可过滤列表、因子详情、相关性矩阵热力图 + 剪枝预览。
- **Single Factor Test**——一个轻量的表达式编辑器，带**实时 lint/AST**（通过
  `POST /v1/factor/lint`）、qlib↔Python 转换按钮、配置行，以及**渐进式
  SSE 流式图表**（IC 时间序列、衰减、分组收益、IC 热力图）外加一个带
  保存到库功能的摘要面板。

## 使用技巧

- 求值需要已导入的数据。对于短窗口因子（例如 `cs_rank(close)`）你会立即看到
  图表；20 日窗口因子需要 ≥ 20 天的历史，否则会全为 NaN（UI 会显示诊断信息
  而非静默失败）。
- 可选的 API key：存储在 `localStorage.assay_api_key` 中，并作为 `X-API-Key` 发送
  （仅当服务器设置了 `ASSAY_API_KEYS` 时才需要）。

## 未接入（无后端）

实时 agent 信息流、Alpha-Space UMAP 图、Lineage DAG 以及 IC 热力图库模式
都被标注为"暂不可用"而非伪造——它们需要尚不存在的后端端点 / 数据。
