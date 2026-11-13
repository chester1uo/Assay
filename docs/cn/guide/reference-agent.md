# 参考 Agent 与离线演示

`assay.agent` 是可替换的参考客户端：从真实 stdio 或鉴权 HTTP MCP 发现工具，由支持 Chat Completions
工具调用协议的模型决定调用顺序。服务端保存研究状态，客户端保存对话、模型回执与用量。

## 连接公开服务

分别配置模型的 `ASSAY_AGENT_API_KEY` 与 Assay 的 `ASSAY_MCP_API_KEY`。
`MODEL_BASE_URL` 和 `MODEL_NAME` 指定模型供应商：

```bash
python -m assay.agent \
  --base-url "$MODEL_BASE_URL" --model "$MODEL_NAME" \
  --task-file task.txt --output ./remote-agent-run \
  --mcp-url https://assay.qfinverse.com/mcp/ \
  --mcp-api-key-env ASSAY_MCP_API_KEY --mcp-timeout 300
```

`--mcp-url` 与 `--server-command` 二选一。明文 HTTP 只允许 loopback 测试。
服务端保存数据集、预算、实验与交接笔记；本地客户端保存模型对话和回执。
调用模型前可先运行只读的[部署验收脚本](deployment.md)。

## 离线运行

```bash
pip install '.[mcp]'
python -m assay.demo --output ./demo-workspace
```

输出目录必须尚不存在。命令生成合成 OHLCV，执行两个因子及一次诊断失败，重启恢复状态，
并生成 `manifest.json` 与可直接打开的 `report.html`。无需模型或行情供应商凭证；合成数据
用于演示软件流程，不支持投资收益结论。

## 接入真实模型

```bash
python -m assay.demo --prepare-only --output ./agent-workspace
export ASSAY_DATA_DIR="$PWD/agent-workspace"
# 通过自己的密钥管理方式设置 DEEPSEEK_API_KEY。
python -m assay.agent \
  --preset deepseek-flash \
  --task-file task.txt --output ./agent-run \
  --max-turns 8 --max-tool-calls 16 --max-tokens 4096 \
  --server-command python -m assay.mcp.server
```

`--preset deepseek-flash` 使用 DeepSeek 官方 API、`deepseek-flash` 模型和
`DEEPSEEK_API_KEY` 环境变量，默认关闭独立思考模式。也可以继续显式指定
`--base-url`、`--model`、`--api-key-env` 和 `--extra-body` 来连接其他供应商。

`task.txt` 可以要求 Agent 发现合成数据集、创建三次尝试预算的会话、检验反转假设、查看
失败原因并保存交接笔记。`--server-command` 放在最后。客户端和服务端使用的 Python 均需
安装 MCP extra。工具定义、研究文本与报告会发送给配置的模型提供商；密钥保留在环境变量
及请求头中。更换提供商时需核对其协议与实际模型身份。

输出包含 `tools.json`、`conversation.json` 和 `run.json`，后者记录实际响应、请求配置、
模型身份、工具结果、耗时与供应商报告的 token 用量。返回模型名须匹配请求名，或由
`--allow-returned-model` 显式列出允许的别名；别名匹配不代表模型等价。

HTTP 请求不自动重试、不跟随重定向。失败或截断的回执会保留；未知用量明确标记为不完整。
`model_finished` 仅表示模型已停止，是否完成任务必须核对持久实验记录。客户端暂不自动
恢复中断的对话；可以新建客户端运行，指定原研究会话进行交接。服务端仍独立执行预算约束。

默认把实验执行结果整理成紧凑反馈后发给模型，完整报告仍保存于回执和服务端。Agent 可
显式调用 `assay_experiment_get` 获取完整序列；会话历史默认通过紧凑研究摘要读取。
此转换不重新计算分数，也不把原始大面板的分数混入指定评分窗口。

参考客户端会统一处理 MCP 的结构化 JSON 和单个 JSON 文本块，混合内容保持原样。
运行记录分别保存原始传输内容和 `model_result`；MCP 工具失败会以明确的
`isError` 标志反馈给模型。报告压缩发生在参考客户端，服务端仍返回完整工具结果。
