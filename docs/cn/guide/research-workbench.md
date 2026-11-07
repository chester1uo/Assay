# 研究工作台

在 Assay Web API 服务中打开 `/#/research`。工作台与 MCP、SDK 共用持久研究工作区，
展示研究目标、实验活动、父子及重放关系、完整证据和交接笔记。

## 本地合成数据演示

```bash
pip install '.[api,mcp]'
python -m assay.demo --prepare-only --output ./workbench-data
export ASSAY_DATA_DIR="$PWD/workbench-data"
python -m uvicorn assay.api.app:app --host 127.0.0.1 --port 8014
```

打开 `http://127.0.0.1:8014/#/research`。输出目录必须是新目录，行情为合成数据。

1. 在 **Start a new session** 中选择数据集，填写目标和实验预算。
2. 在 **Evaluate a candidate** 中提交 `unknown_operator(close)`，观察真实失败记录。
3. 选择该失败实验作为 parent，提交 `-cs_rank(ts_returns(close,5))` 和修订假设。
4. 查看实际评分和来源关系，保存交接笔记，下载 manifest，并刷新网页验证持久化。

这些网页操作全部标记为 `human:webui`。它们是真实执行器上的人工演示，不是模型自动
研究证据；执行成功也不代表假设或收益已经成立。

## 查看 Agent 研究

让 MCP Agent 使用同一 `ASSAY_DATA_DIR`，按参考 Agent 指南启动。点击 **Refresh sessions**
发现新会话，选中后每三秒读取持久状态。网页不会自行调用 LLM，也不提供 Agent 的启动、
暂停或对话恢复。当前展示的是保存的实验活动，不是完整的模型推理或工具调用 trace。

点击实验可查看完整记录。研究数据集固定在 session 内，工作台会隐藏旧版因子页面的
universe/period 控件。来源视图中的外部 replay ID 只是引用。

研究 API 复用已有 API-key 鉴权。运行中的评估允许并发查询；在当前页面内，网络失败后
重试相同输入会复用 request ID，返回 running 时也会保留，直到刷新看到终态。
重新加载网页会丢弃客户端重试状态，重新提交前应先查看已有实验。这不等于自动 worker
恢复或持久后台任务调度。

浏览器验证脚本为 `scripts/check_research_workbench_browser.py`，输出保存到 Assay 的
artifacts 目录。真实合成行情流程与 mock 传输竞态测试分开记录。

导出直接保留服务端 JSON 文本，避免浏览器重新序列化改变数字表示而破坏哈希。浏览器回归还会验证下载清单的完整性，并导入一个新工作区逐值核对实验。
