# 复用 AlphaBench V2 测试 Assay

本测试复用 `/home/xluo/workdir/alphabench-v2` 的组件，不启动或恢复原 benchmark campaign。
所有新脚本、研究数据库、计划、模型响应、MCP 调用记录和测试结果均保存在 Assay 下。
AlphaBench 源码通过只读 import 复用；运行时关闭 Python 字节码写入。

## 已接入的组件

| AlphaBench 组件 | 在 Assay 中的用途 |
|---|---|
| `DeepSeekChatClient` | Provider 调用与原始响应、usage、模型身份记录 |
| `MCTSCellSearch` | 原生固定预算 seed → 训练反馈 → refinement 循环 |
| `AssayClient` / `EvaluationSpec` | 调用带补丁的 `127.0.0.1:8002`，保留 PIT 请求配置 |
| `dsl_registry` / `lint_generation` | 复用 OHLCV 字段和算子契约及候选检查 |
| `privacy` / `build_prompt` | 不向模型暴露公司、股票代码、绝对日期或原始市场上下文 |
| `orient_factor` | 仅按训练期 RankIC 固定因子方向 |
| `evaluate_generation` | 独立预热行情、标签缓冲和严格留出期日期覆盖核对 |
| `atomic_json` / `payload_hash` | 原子保存证据和配置哈希 |

本轮没有接入全部 benchmark participant；AlphaAgent、RD-Agent、FactorMiner 的原生运行器
仍需要各自的依赖、协议和生命周期适配。不能由本测试推断它们已经兼容。

## 测试链路

单轮生成和原生 MCTS 都通过真实 stdio MCP 调用 Assay 的持久研究工具。第一次客户端保存
实验与笔记后，关闭 MCP 子进程，再启动新进程读取会话并重放相同请求 ID，核对状态和预算未变。
MCTS 自身仍使用原来的搜索机制；共享状态的存在不代表已验证记忆能提升研究效果。

训练窗口为 2024-01-02 至 2024-06-28，固定 2024-06-28 的 NASDAQ100 股票轴。
两种策略最终表达式和方向冻结后，才进行 2024-07-01 至 2024-07-31 的留出评估。
留出行情另带 180 个自然日预热，并读至 2024-08-02 以提供 forward-return 标签。
只统计 7 月的冻结交易日。新 Dataset 使用 `score_period`/`score_dates`，研究会话里的报告顶部
也是该区间的指标；原始读取面板另存为 `panel_report`。原始面板 `rank_ic` 包含预热，不能作为 OOS 分数。
`*-holdout-evaluation.json` 继续使用 AlphaBench 的严格评分逻辑，核对其 OOS 分数与工作区顶部指标一致。
Dataset 同时固定 DSL 字段、算子与窗口约束，所有策略的候选在执行前都经过工作区检查。

## 运行

安装 Assay 的 MCP/性能依赖，并让现有带补丁的 8002 服务可用。运行完整 live smoke：

```bash
PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 python scripts/smoke_alphabench_reuse.py \
  --output /home/xluo/workdir/Assay/artifacts/new-live-smoke \
  --accept-returned-model deepseek-flash
```

请求的模型名来自 AlphaBench registry。当前服务实测返回 `deepseek-flash`；本地显式接受该
返回标识，不修改 AlphaBench 注册表，也不据此证明两个模型名等价。每次最多三个模型请求，
无隐藏 transport retry；API 凭证只在进程环境中读取，不写入计划。
此命令会调用模型；输出目录必须不存在，避免覆盖已有尝试。

可用 `--replay-direct PATH` 显式复用完全一致的已记录单轮生成响应。
也可以一次回放全部三个响应，无需新模型调用：

```bash
PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 python scripts/smoke_alphabench_reuse.py \
  --output /home/xluo/workdir/Assay/artifacts/new-complete-replay \
  --accept-returned-model deepseek-flash \
  --replay-run /home/xluo/workdir/Assay/artifacts/alphabench-reuse-20261103-03
```

回放会核对源文件哈希、每一步提示词与 decoding；任何不匹配都报错，**不会转为 live 调用**。
它会重新经过真实 MCP、评估器、会话重启及留出评分，结果明确标记 `recorded_provider_replay`。
回放不是新的 LLM 实验，也不是 MCTS 内存搜索树中断恢复。

只修复留出评估时，不必再调用模型：

```bash
PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 python scripts/recover_alphabench_holdout.py \
  --source /home/xluo/workdir/Assay/artifacts/alphabench-reuse-20261103-03 \
  --output /home/xluo/workdir/Assay/artifacts/new-holdout-recovery
```

共享 Python 环境的 SQLite 动态库存在版本冲突。本轮验证通过命令环境临时设置
`LD_PRELOAD=/home/hluo/miniconda3/envs/llada/lib/libsqlite3.so.0`，没有修改共享环境。
使用正常可导入 `sqlite3` 的 Python 环境时不需要该设置。

## 证据与限制

`plan.json` 冻结配置和源文件哈希，`provider-calls/` 保存原始响应，`mcp-ledger-*.json` 保存
工具调用，`workspace/research/workspace.sqlite3` 保存研究状态。源码与 git 状态的前后核对结果
保存在 `alphabench-source-before.json` / `alphabench-source-after.json`。
原始失败目录保留，修正后的运行另建目录，不将失败记录改写成成功。

这是工程集成 smoke：只有一个历史美股窗口，两种策略预算不同，也没有真实用户评估。
其结果不能证明策略优劣、框架提升、记忆有效或因子可盈利；行情内容也尚未成为不可变快照。
