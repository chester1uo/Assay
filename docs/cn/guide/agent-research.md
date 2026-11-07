# Agent-Native 因子研究

Assay 为外部 Agent 提供持久研究状态与因子执行工具。模型、提示词和探索策略由调用方选择，
研究状态不绑定特定 LLM 或多 Agent 流水线。

| 对象 | 内容与语义 |
|---|---|
| Dataset | 固定股票池、日频区间、知识截止日、收益期限、执行模型、复权与后端名称 |
| Research Session | 研究目标、发起 Agent、尝试次数预算、持久笔记和实验历史 |
| Experiment | 独立尝试 ID、请求 ID、假设、表达式、父实验、状态、完整评估报告或异常 |

工作区位于 `<data_dir>/research/workspace.sqlite3`。现有 `assay.Session` 是临时计算面板缓存，
研究会话独立于该缓存，重启服务后仍可读取。

## Python 示例

离线体验：`PYTHONPATH=src python scripts/demo_agent_research.py`。脚本生成合成行情，调用真实评估器，
保留失败实验、重建服务，再以第二个模拟 Agent 客户端接续研究。它验证生命周期，不代表已验证真实 LLM 兼容性。

安装依赖并按[快速上手](getting-started.md)准备行情后：

```python
import assay

assay.init()
ws = assay.research_workspace()
dataset = ws.register_dataset(
    "nasdaq-reversal-2024", "NASDAQ100", ["2024-01-01", "2024-12-31"],
    horizons=[1, 5, 10], execution="next_open", adj="split",
)
session = ws.create_session(dataset["id"], "研究短期反转", agent="agent-a", max_experiments=20)
attempt = ws.evaluate(
    session["id"], "-cs_rank(ts_returns(close,5))", "近期涨幅较大的股票可能短期回落",
    request_id="reversal-5d-01", agent="agent-a",
)
ws.add_note(session["id"], "下一轮比较更短的回看窗口", agent="agent-a")
print(attempt)
```

重启或切换 Agent 后，用 `ws.list_sessions()` 发现会话，使用 `ws.get_session(id)` 读取目标、
配置、笔记、成功和失败实验及剩余预算。新尝试可用 `parent_id` 指向同一会话中的旧实验。
`ws.compare_experiments([id1, id2])` 返回指标、失败信息和配置是否一致，不丢掉失败行。

## 读取区间、评分区间与表达式约束

`period` 是行情读取范围，包含预热和前瞻标签缓冲。可另设 `score_period` 与预先核验并冻结的
`score_dates`，使报告顶部 IC/RankIC 只统计指定交易日；目前显式评分区间支持单一 horizon。
日期缺失、乱序、序列不齐或评分为空会保留为 `INCOMPLETE_SCORE_COVERAGE`，不回退到面板总体指标。

实验的 `report` 是区间内指标，`panel_report` 单独保留后端原始证据。无法仅凭每日 IC 重算的
换手率、分组收益和衰减会置空，避免继续展示预热区间的汇总值。

Dataset 还可指定 `expression_policy`：`fields`、`operators`、`max_window` 和
`max_cumulative_lookback`。工作区在调用后端之前，检查字段、算子和嵌套窗口；拒绝的候选
保留为失败尝试并计入次数预算，但不会执行行情评估。评分范围和约束均参与配置哈希，SDK/MCP 使用同一契约。
窗口预算沿用 AlphaBench 稳定 OHLCV 语法的规则，不代表 EMA 等递归滤波器只有有限历史依赖。
读取范围仍需显式包含足够预热和标签缓冲，注册接口不会自动下载或推导额外行情。

## MCP 研究工具

运行 `assay serve-mcp`。15 个研究工具如下，与原有 11 个评估/因子库工具共存：

| 工具 | 用途 |
|---|---|
| `assay_backend_list` | 发现后端、能力及可选的可用性检查 |
| `assay_research_context` | 获取紧凑研究交接摘要 |
| `assay_research_import` | 将清单恢复到新工作区，保留原预算与实验身份 |
| `assay_research_export` | 导出带 SHA-256 完整性哈希的会话清单 |
| `assay_experiment_replay` | 按原表达式及配置显式重跑终态实验 |
| `assay_dataset_register` | 注册评估数据集配置 |
| `assay_dataset_list` | 发现已注册配置 |
| `assay_research_session_create` | 创建有目标和预算的研究会话 |
| `assay_research_session_list` | 发现持久会话 |
| `assay_research_session_get` | 恢复研究上下文和实验历史 |
| `assay_research_note` | 保存判断、失败方向和交接说明 |
| `assay_experiment_run` | 执行假设并保留结果 |
| `assay_experiment_get` | 检查报告、诊断与异常 |
| `assay_experiment_compare` | 比较指标并标记配置差异 |
| `assay_experiment_abandon` | 显式结束中断尝试 |

建议流程：发现可用股票池 → 注册数据集 → 创建或恢复会话 → 查看已有尝试 → lint → 执行实验 →
检查指标与诊断 → 保存判断 → 决定修订、调用已有组合回测工具或停止。下一轮决策由外部 Agent 驱动。

## 重试、预算与恢复

每次尝试指定稳定的 `request_id`。重复提交只返回旧状态，不重复执行；相同请求 ID 搭配不同
表达式、假设或父实验会报错。新请求 ID 代表新尝试，相同因子的不同实验不互相覆盖。
SQLite 事务在执行前预留预算，并发进程也不能超出次数上限；失败和放弃的实验均计入预算。

状态为 `running → completed/failed`；显式恢复可将 `running` 改为 `abandoned`。
`completed` 仅代表评估器未返回失败模式，不代表因子有效或盈利。
进程退出会留下 `running` 记录。确认旧 worker 停止后，用 abandon 工具记录原因，
再用新请求 ID 重试。修改记录不负责终止正在运行的后端。

## 后端与复现边界

用 `ws.register_backend(name, backend)` 注册实现 `evaluate(expr, dataset) -> dict` 的评估器，
返回 JSON 安全的因子报告。状态保存后端名称，后端对象与凭证不落盘，重启后需重新注册。
该接口可用于后续连接 QFV；这次没有实现 QFV 完整执行适配器。

自定义数据集可指定参与配置哈希的 `backend_options`。内置后端拒绝未支持的设置。
可选的 `AlphaBenchBackend` 复用 AlphaBench V2 的 REST 客户端，连接带补丁的 `localhost:8002`，
核对股票池冻结日期、股票轴哈希/数量与数据截止日，再向 Agent 提供分数。
跨仓库复用及真实 MCP 测试说明见 [AlphaBench 复用测试](alphabench-reuse.md)。

Dataset 固定的是配置，不下载数据、不冻结行情。`spec_hash` 是配置的 SHA-256，不包含行情
内容、后端代码和模型版本。配置一致不等于不可变快照复现。内置 Assay 后端不支持解析快照 ID，
会拒绝指定 `snapshot_id`；自定义后端需自行解析和验证快照。
现有结束日股票池选择逻辑保持原样，样本外隔离和选择偏差仍需单独控制。

预算只统计评估尝试次数。集成测试脚本会记录真实 provider usage，但通用 Token 计费、自动 Agent runner
和独立研究 WebUI/REST 仍是后续能力。完整说明见[英文指南](../../en/guide/agent-research.md)。

## 发现、交接与显式重跑

先调用 `assay_backend_list(probe=True)`。返回值区分后端声明的能力与实际检查的可用性；
重启后尚未注册、但被历史数据集引用的后端也会列出。本地检查准备好的文件，远程检查
服务清单，均不保证任意指定期间的数据覆盖。适配器可选实现 `describe() -> dict` 和
`probe() -> dict`，原来只实现 `evaluate()` 的适配器仍可使用。

`assay_research_context(session_id, limit=10)` 提供剩余预算、失败统计、规范化表达式重复、
候选摘要与交接笔记，不返回指标时间序列。每个列表限制为 1–50 行，并标明截断情况；
单条文本没有 token 长度上限。候选按 RankIC 绝对值排序，仅便于查看，不代表统计验证；
表达式重复也不等于因子相关性。

```python
backends = ws.list_backends(probe=True)
context = ws.research_context(session_id, limit=10)
manifest = ws.export_session(session_id)
assert ws.verify_manifest(manifest)
rerun = ws.replay_experiment(experiment_id, session_id,
                            request_id="explicit-rerun-1", agent="next-agent")
```

导出读取一致的 SQLite 快照，状态不变时哈希不变。清单含完整研究记录，不包含行情文件或
后端代码；哈希验证内容完整性，不验证来源身份。可用 `assay_research_import` 将清单恢复到另一工作区。重跑保留原
表达式、假设、配置哈希与 `replay_of` 来源关系，消耗新预算；也允许目标为配置完全一致的
另一会话。重复请求 ID 返回旧尝试。可变行情仍可能使结果变化，且不会恢复 Agent 的搜索树。

## 会话迁移

SDK 使用 `target.import_session(manifest)`，MCP 使用 `assay_research_import(manifest)`。
导入前验证版本、完整性、结构、数据集配置哈希、实验身份、来源关系和预算，再在单个事务中
写入。原 ID、请求 ID、笔记、时间戳与终态尝试均保留。相同清单重复导入具有幂等性；若目标
记录有冲突，包括目标会话已继续实验，则拒绝覆盖或合并。读取历史不需要后端已注册，继续
执行前需要注册对应后端并提供行情。

含 running 尝试的清单不能导入：先确认旧 worker 停止，显式 abandon，再重新导出。跨会话
`replay_of` 作为来源引用保留，不自动导入来源会话；来源可解析时会检查状态、表达式、假设和配置是否一致，检查支持两种导入顺序；如需查看缺失的来源，需单独导入其会话。
哈希不验证历史报告的真实性，记录迁移也不转移正在运行的 Agent、搜索树或行情。

导入不会自动停用源工作区。预算约束按工作区生效，迁移后应选择在哪个工作区继续。
这是研究快照复制，不提供分布式 worker 协调或跨工作区统一预算。
