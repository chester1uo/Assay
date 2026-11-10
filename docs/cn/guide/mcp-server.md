# MCP Server

服务现有 **39 个工具**，含 8 个 FactorDB 工具及 20 个后端发现、持久数据集、研究会话与实验工具。
使用流程与恢复语义见 [Agent 研究指南](agent-research.md)。

MCP server 将 Assay 的 evaluate→library 循环暴露为 Model Context Protocol 工具，供 LLM
agent 调用以驱动自主的 alpha 挖掘。基于高层的 **FastMCP** API 构建
（[`src/assay/mcp/server.py`](../../../src/assay/mcp/server.py)）。

## 公开入口

远程 Agent 使用 **`https://assay.qfinverse.com/mcp/`**，传输为 Streamable HTTP。
通过 `Authorization: Bearer …` 或 `X-API-Key` 发送个人／服务 key。模型供应商的 key
与 Assay 访问 key 是两份不同的凭证。当前未实现 OAuth，客户端需要支持这两种请求头。

[`/.well-known/assay.json`](https://assay.qfinverse.com/.well-known/assay.json)
提供部署后的实际工具数与 schema 摘要。研究 session 持久化在服务端数据库中，MCP 传输本身无状态。

```python
import asyncio
import os
from assay.mcp.client import connect_mcp

async def main():
    async with connect_mcp(url="https://assay.qfinverse.com/mcp/",
                           api_key=os.environ["ASSAY_MCP_API_KEY"]) as session:
        tools = await session.list_tools()
        print([tool.name for tool in tools.tools])
        print(await session.call_tool("assay_backend_list", {"probe": True}))

asyncio.run(main())
```

研究流程使用 dataset → research session → experiment → 检查／决策。
`assay_experiment_run` 默认复用行情与因子计算，每次尝试仍独立记录。日常检查优先使用紧凑 context
和实验／历史分页接口；为保持兼容，run／replay 的即时返回仍包含完整实验。已认证研究员目前共享工作区。

部署和上线后验收见[部署指南](deployment.md)。

## 本地运行

```bash
python -m assay.mcp.server                          # stdio (Claude Desktop / local agents)
python -m assay.cli serve-mcp --transport http --port 8001 # 共用鉴权的 HTTP 应用
```

HTTP 模式需安装 `.[api,mcp]`，stdio 只需 MCP extra。使用已准备的本地行情不需要供应商凭证。
旧的 `--transport sse` 仅供 loopback 开发；公开部署使用共用的 HTTP 应用。

## 评估与因子库工具（11 个）

| Tool | Purpose |
|---|---|
| `assay_evaluate` | 求值单个因子 → `FactorReport`（qlib 或 Python 语法） |
| `assay_batch` | 并行求值多个因子，按 `rank_icir` 排序（优先于循环使用） |
| `assay_lint` | 无需数据的表达式语法检查（方言、字段、算子、诊断） |
| `assay_universes` | 列出可用股票池及其代码数量与默认值 |
| `assay_portfolio_backtest` | 对因子做完整组合回测 → 精简的 `PortfolioReport` |
| `assay_library_list` | 列出库中因子并支持过滤（生成前检查冗余） |
| `assay_library_get` | 获取单个因子的完整 `FactorReport` |
| `assay_library_save` | 将报告保存到库 |
| `assay_library_correlation` | 因子 ID 之间的两两相关性 |
| `assay_library_prune` | 识别 / 移除冗余因子 |
| `assay_system_status` | 数据新鲜度 + 缓存统计 |

`assay_evaluate` 的描述在运行时会被实时的算子模式
（`assay.engine.operator_schema()`）丰富，因此 agent 能内联看到可用的算子。

## Claude Desktop 配置

`~/.config/claude/claude_desktop_config.json`：

```json
{
  "mcpServers": {
    "assay": {
      "command": "python",
      "args": ["-m", "assay.mcp.server"],
      "env": { "ASSAY_DATA_DIR": "data_2025_2026h1" }
    }
  }
}
```

## 典型的 agent 循环

![agent 循环](../../assets/figures/cn/agent-loop.svg)


设计契约：[architecture.md](../design/architecture.md) §6。
