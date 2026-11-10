# 面向 Judge 的研究审计

Audit 将一个研究 session 整理成紧凑、带证据引用的评审报告。它检查已保存的记录，不调用模型或执行后端，不修改候选决策，也不消耗实验预算。MCP、REST、Python、工作台和离线 manifest 使用同一套实现。

## 使用方式

在工作台选择 session，点击 **Audit session** 查看检查项、错误、待复核事项和候选证据。**Download audit JSON** 保留服务端原始响应及其哈希。连同 session manifest 一起下载，就能交给 judge 复核。

MCP 调用只读、幂等工具 `assay_research_audit`：

```json
{"session_id":"research_...","offset":0,"limit":20}
```

HTTP 接口返回相同的报告：

```bash
curl -H "Authorization: Bearer $ASSAY_MCP_API_KEY" \
  'https://assay.qfinverse.com/v1/research/sessions/research_.../audit?limit=20'
```

Python 使用已有工作区：

```python
report = service.research.audit_session(session_id)
print(report["record_integrity"], report["finding_counts"])
```

Judge 也可以离线检查导出的 manifest，无需服务端或行情：

```bash
python -m assay.audit --manifest session.json --output session.audit.json
# 安装更新后的包，也可以直接使用：
assay-audit --manifest session.json --output session.audit.json
```

CLI 在记录检查失败时返回 1，记录一致时返回 0（可以带复核警告），参数／输入错误返回 2；不会覆盖已有输出文件。离线审计不导入 manifest，也不创建工作区。

## 检查内容

| 检查项 | 证据 |
|---|---|
| Manifest 与规格 | 格式／版本／哈希、dataset hash、记录结构 |
| 实验与预算 | ID／request ID 唯一性、所有终态及运行中实验、已用／剩余预算 |
| 来源关系 | session／spec 关联、规范表达式、parent／replay 链路和时间顺序 |
| 生命周期 | 决策引用匹配的实验；接受候选必须有已完成实验 |
| 诊断 | 已记录的 lookahead、评分完整性、表达式策略 |
| IC 指标 | 有限数值、相关系数范围、日期对齐／评分范围、序列均值 |
| 组合回测 | NAV 算术、配置／spec 一致性、数值指标与成本假设 |
| 本地 Registry | 同一 SQLite 快照中的因子—实验关联及实验／决策身份列 |

在线审计的数据库连接启用 query-only。跨 session 的 replay 来源若在该快照中，会一起核对；离线 manifest 没有包含的来源则列为待复核。Registry 的 jobs、scores 发现视图和 usage 计费不在这项索引检查范围内。

失败、取消的实验保留在总数中；一次实验失败不等于 audit 失败。运行中实验会产生复核事项，审计不会取消它，也不会让它变成可导入的终态记录。

## 理解结果

| 字段 | 含义 |
|---|---|
| `record_integrity` | 已保存记录的一致性，`passed`／`failed` |
| `status` | `failed`／`needs_review`／`passed`；警告不会改变一致性判定 |
| `checks`／`finding_counts` | 整个 session 的检查结果，不随分页截断 |
| `findings` | 稳定代码、严重度、实验 ID 和 JSON Pointer 证据位置 |
| `summary` | 目标、数据规格、预算、全部状态计数、决策与候选指标 |
| `assessment` | 明确审计范围和仍需 judge 判断的内容 |
| `manifest_hash`／`audit_hash` | 选定的输入版本和报告内容摘要 |

数据未冻结、执行／成本存在限制时，会列出复核事项，不会直接判记录损坏。候选指标只用于描述，审计不内置“IC 必须为正”的门槛，也不替 judge 决定是否接受。紧凑摘要不返回 backend option 值，完整配置以原始 manifest 为准。

即使只返回一页，也会检查全部记录。`page.finding_total` 和 `page.candidate_total` 分别给出总数；`offset`／`limit` 同时对两类列表分页，`has_more`、`candidates_have_more` 指示各自是否还有下一页。详细序列通过现有 experiment／history 工具读取。

后续调用可把首次返回的 `manifest_hash` 传入 `expected_manifest_hash`；离线使用 `--expected-manifest-hash`。如果输入版本变化，报告给出 `EXPECTED_MANIFEST_MISMATCH`，避免 judge 混合多个版本的证据。

哈希能识别内容变化，不证明作者身份。实际重放、行情真实性／覆盖、样本外有效性和研究目标是否达成都需要相应证据；这些判断在 `assessment` 中单独标明。

相关指南：[Agent 研究](agent-research.md)、[MCP](mcp-server.md)、[部署](deployment.md)。
