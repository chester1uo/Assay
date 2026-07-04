# 预计算与公共子表达式消除（CSE）

当你在同一面板上求值**多个**因子——一次扫描、一次库重评分、一次合成搜索——时，
它们会共享子表达式。`Sub(high, low)`、
`Mean(volume, 20)`、`Ref(close, 1)` 在*成千上万*个原本各不相同的因子中反复出现。
将每个共享子树计算一次并复用其结果，是提升批量吞吐量的最大杠杆。

Assay 通过两种方式实现，两者都以 AST 稳定的
[`struct_hash`](../design/engineering.md) 为键：两个结构相同的子树——
无论由哪种表层语法产生——共享同一个哈希，只计算一次。

1. **进程内 CSE**——`FactorEngine.evaluate_many([...])` 在每个表达式之间贯穿
   一个结构哈希备忘录，因此批次中共享的子树恰好只求值一次。零设置，始终正确。
2. **持久化预计算**——`PrecomputeStore` 挖掘语料库中最热的子树，
   **为每个资产物化每一个子树**，并将 `(T, N)` 矩阵存到磁盘。后续批次
   加载这些子树而非重新计算——跨*运行*和跨*进程*。

两者都与逐因子的 `evaluate()` **逐比特一致**（已验证：`max|Δ| =
0`，NaN 模式相同）。

### 实测

来自扫描语料库的 1,200 个因子样本，在 400×150 面板上（结构共享比 2.3×）：

| Path | Throughput | Speedup |
|---|---|---|
| naive per-factor `evaluate()` | 5 factors/s | base |
| `evaluate_many()` (in-process CSE) | 6 factors/s | **1.20×** |
| `evaluate_many(precompute=…)` warm | 6 factors/s | 1.18× |

进程内 CSE 的加速**随语料库重叠程度而增长**——一个随机的
1,200 因子样本比完整的 15k 语料库共享得少，而在后者中最热的子树
反复出现数千次（`sub(high, low)` ×4,667）。持久化预计算在对稳定面板做**反复**
扫描时（构建一次约 1s；跨运行和跨进程复用）以及*昂贵*子树反复出现时才能回本；
对于共享子树都很廉价的单次扫描，它大致中性（磁盘加载 ≈ 重新计算）。
根据工作负载选择工具——见末尾的表格。

---

## 找出公共子表达式

```bash
# pure analysis — no data needed
python -m assay.cli precompute top --corpus _assay_sweep/factors_canonical_unique.txt --top-k 15
```

```
top 15 common sub-expressions in _assay_sweep/factors_canonical_unique.txt:
  x4667   nodes=3   score=9334     sub(high, low)
  x1872   nodes=3   score=3744     ts_delay(close, 1)
  x1563   nodes=3   score=3126     ts_mean(volume, 20)
  x1351   nodes=3   score=2702     ts_std(close, 20)
  ...
```

- **count**——在语料库中的总出现次数。
- **score**——`count × (n_nodes − 1)`，即将该子树计算一次并在各处复用所省下的
  算子求值次数（排序键）。

SDK：`service.common_subexpressions(corpus, top_k=100)` →
`[{expr, count, n_factors, n_nodes, score}]`。

---

## 构建预计算存储

```bash
python -m assay.cli precompute build \
  --corpus _assay_sweep/factors_canonical_unique.txt \
  --universe NASDAQ100 --start 2025-01-02 --end 2026-06-09 --top-k 512
python -m assay.cli precompute stats
```

这会挖掘出最热的前 `k` 个共享子树，并为**所有资产**各自计算，
将矩阵存储在 `<data_dir>/precompute/` 下。构建本身也经过 CSE 加速
（嵌套的共享子树只计算一次）。

SDK：

```python
import assay
svc = assay.init()

info = svc.build_precompute(
    "_assay_sweep/factors_canonical_unique.txt",
    universe="NASDAQ100", period=("2025-01-02", "2026-06-09"), top_k=512,
)
print(info["built"], "subexprs |", info["est_evals_saved"], "evals saved / pass")
```

### 按历史自动更新

磁盘上的键会折入 `FactorEngine.panel_fingerprint()`——面板的
**日期 × 代码 × 字段**的摘要。导入新历史后指纹会改变，因此
陈旧条目会直接不再匹配（一次未命中），存储便为新面板重建。
无需手动失效；条目在一个面板内是稳定且共享的。

### 与每日数据更新耦合

热缓存被接入数据管理，使其**自动**与实时数据保持对齐：

- 在一次成功的**数据更新任务**（`POST /v1/admin/data/jobs`）之后，同一任务会
  为该市场的主股票池刷新预计算存储，覆盖**刚导入的数据区间**，
  以**因子库**作为语料库进行挖掘
  （`AssayService.refresh_precompute_for_market`）。一次 `init` 运行会刷新每个股票池。
- 每次构建都会写入一份**清单（manifest）**，记录其有效期、`as_of`、
  指纹、构建时间以及最新导入日期——因此新鲜度只是一次廉价的
  日期比较（无需加载面板）。
- **数据管理页面**（Admin → Data Manager）会显示一张 *Hot cache* 卡片：按
  股票池展示有效期、条目数、最热的子表达式，以及一个
  **fresh / stale** 徽章（stale = 缓存构建后数据已推进）。
  **Rebuild US / Rebuild CN** 按钮会排入对每个股票池的完整重建。

```
GET  /v1/admin/cache/status     -> {store:{entries,bytes,dir}, scopes:[{universe, period, as_of,
                                     fingerprint, built_at, data_latest, n_entries, fresh, top}]}
POST /v1/admin/cache/rebuild     {market}  -> queues a background rebuild job
```

`precompute_status()` 仅当某个范围记录的 `data_latest` 仍等于该市场当前最新
导入日期时，才将其标记为 `fresh`——即缓存已对齐到实时数据的有效期。

---

## 用它来加速扫描

```python
# raw factor VALUES for a big corpus, CSE + precompute accelerated
results = svc.evaluate_many(exprs, universe="NASDAQ100",
                            period=("2025-01-02", "2026-06-09"), use_precompute=True)
for r in results:          # r is a FactorResult: r.values is the (T, N) matrix
    ...
```

在引擎层面（无服务 / 数据层）：

```python
from assay.engine import FactorEngine, PrecomputeStore

eng = FactorEngine(panel)
store = PrecomputeStore("/path/to/precompute")
store.build(eng, corpus_exprs, top_k=512)          # once

bound = store.bind(eng.panel_fingerprint())
results = eng.evaluate_many(exprs, precompute=bound)  # warm: hot subtrees loaded, not recomputed
print(f"{bound.hit_rate:.0%} of subtree lookups served from precompute")
```

`evaluate_many` 为每个输入返回一个 `FactorResult`（仅有值——无 IC 指标）。
格式错误的表达式会抛出异常，与 `evaluate()` 完全一样；若需要软失败，
可用 `engine.diagnose()` 预先过滤。

---

## 何时用哪个

| Situation | Use |
|---|---|
| 单个因子 | `evaluate()` |
| 偶尔的批量 | `evaluate_many()`（进程内 CSE——免费） |
| 对稳定面板的反复扫描 | 先 `precompute build` 一次，再 `evaluate_many(..., use_precompute=True)` |
| 评分（IC / 衰减 / 换手率），而非原始值 | `batch()`（逐因子指标） |

---

## 工作原理（内部）

- `assay.engine.cse.common_subexpressions(exprs)`——解析语料库，遍历每一棵
  AST，按 `struct_hash` 聚合子树，按所省的重新计算量排序。
- `FactorEngine._eval_cse(node, ctx, memo, precompute)`——备忘化求值：每个
  `OpNode` 结果按其 `struct_hash` 缓存；已绑定的 `precompute` 会通过从磁盘加载
  其矩阵来短路一个子树。叶子（字段 / 字面量）不耗成本，永不缓存。
- `PrecomputeStore`——内容寻址的 `(hash, fingerprint) → (T, N).npy`，分片
  并原子写入（临时文件 + `os.replace`），尽力而为（损坏的文件
  读作一次未命中）。与 L2 因子缓存如出一辙。

正确性依赖于算子的**纯粹性**：内核返回新数组且从不改动
输入，因此在多个父节点间共享同一个数组对象是安全的——这与
引擎在单次求值内跨每个引用复用字段矩阵时所依赖的假设相同。
