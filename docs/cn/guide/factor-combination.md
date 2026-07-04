# 因子合成

将多个单因子融合为一个**复合 alpha**，并在样本外诚实地为其打分。
合成层遵循标准的量化研究流程——
**在训练集上拟合、在验证集上筛选、在测试集上报告**——因此你读到的测试 IC
不会被拟合过程污染。

它通过一个共享引擎以三种方式暴露：

- **WebUI**——*Factor Combination* 标签页。
- **REST**——`POST /v1/combination`（以及 `GET /v1/combination/methods`）。
- **SDK**——`AssayService.combine_factors(...)`。

---

## 流程

每次调仓时，成分股都会经历：

1. **标准化**——每个因子按天做横截面 z-score（`zscore`）或排名（`rank`），
   使不同量纲的因子能合理融合。
   NaN 感知：某个标的在某日缺失时，会被排除在当日横截面之外。
2. **定向**——每个因子都被翻转以指向**正的训练集 IC**；定向结果
   （`+1` / `-1`）会被报告，以保持权重可读。
3. **仅在 TRAIN 上拟合**——合成权重（或模型）在训练窗口上学习得到。
4. **在 VALIDATION 上筛选**——当 `method="auto"` 时，每个候选都在训练集上拟合，
   保留**验证集 ICIR** 最优的那个。
5. **在 TEST 上打分**——冻结后的复合因子的 IC / RankIC / ICIR 在未触碰的测试窗口上报告。
6. **禁运（Embargo）**——训练块和验证块的最后 `embargo` 天会被清除
   （默认 = 最大的前向期限），使得跨越切分边界的、重叠的期限-`h`
   标签绝不可能泄露（即"purged"切分）。

复合因子是一个 `(T, N)` 信号，在所有位置用**相同**的权重求值；
只有*拟合*使用了训练窗口。

---

## 方法

### 解析 / 优化（始终可用——numpy + scipy）

| Method | What it does |
|---|---|
| `equal` | 定向后各因子等权融合 |
| `ic_weight` | 权重 ∝ 训练集 IC 的均值幅度 |
| `icir_weight` | 权重 ∝ 训练集 ICIR（IC 均值 / IC 标准差）——**默认**；奖励*稳定*的预测因子 |
| `ols` | 对各因子做前向收益的池化横截面回归 |
| `ridge` | L2 正则化回归（`ridge_lambda`）；对共线因子稳健 |
| `nnls` | **非负最小二乘**——带约束的（纯多头）优化 |
| `max_icir` | Grinold 的 `Σ⁻¹·IC̄`——**使合成信息比率最大化**的线性融合 |

这些方法产生显式的逐因子**权重**；复合因子即线性融合
`Σ wₖ·factorₖ`。

### 学习型模型（qlib 风格——可选库）

复合因子是模型对定向后各因子的前向收益的**预测**，因此报告的
逐因子数值是**特征重要性**，而非线性权重。

| Method | Library | Kind |
|---|---|---|
| `linear`, `lasso`, `elastic_net` | scikit-learn | linear |
| `random_forest`, `extra_trees` | scikit-learn | tree ensemble |
| `gbrt`, `hist_gbrt` | scikit-learn | gradient boosting |
| `mlp` | scikit-learn | neural net |
| `lightgbm` | lightgbm | gradient boosting |
| `xgboost` | xgboost | gradient boosting |

只有在其库已安装时，模型才会出现在 `GET /v1/combination/methods`
（以及 WebUI 下拉框）中：

```bash
pip install scikit-learn lightgbm xgboost
```

超参数可通过 `model_params` 按调用覆盖（例如
`{"n_estimators": 500, "max_depth": 6, "learning_rate": 0.03}`）；否则
使用合理的默认值，且每个模型都以固定种子拟合，因此一次运行是其输入的纯函数。

---

## REST

```bash
curl -X POST localhost:8000/v1/combination \
  -H 'content-type: application/json' \
  -d '{
    "factors": ["rank(close)", "alpha101:1", "lib:<factor_id>",
                {"name": "mom", "expr": "delta(close, 10)"}],
    "train": ["2025-01-02", "2025-10-31"],
    "val":   ["2025-11-01", "2026-01-31"],
    "test":  ["2026-02-01", "2026-06-09"],
    "universe": "NASDAQ100",
    "horizons": [1, 5, 10],
    "method": "auto"
  }'

# which methods are installed?
curl localhost:8000/v1/combination/methods
```

## 保存与重新加载运行结果

每次运行都会自动保存为滚动的"最近运行"；可保存一条具名、可重新加载的记录——
连同拟合好的模型（权重/重要性、定向、筛选分数、评分卡）——之后可列出或
重新加载而无需重新计算。

```bash
curl -X POST   localhost:8000/v1/combination/saved -d '{"name":"my blend","result":<result-json>}'
curl           localhost:8000/v1/combination/saved            # list summaries
curl           localhost:8000/v1/combination/saved/<id>       # full record (the model)
curl -X DELETE localhost:8000/v1/combination/saved -d '{"ids":["<id>"]}'
```

`AssayService` 上的等价 SDK：`save_combination(result, name)`、`list_combinations()`、
`get_combination(id)`、`delete_combinations(ids)`——由
[`CombinationStore`](../../../src/assay/library/combination_store.py)（位于 `<data_dir>/combinations/` 下）支撑。

**因子规格**接受：裸表达式（`"rank(close)"`）、库引用
（`"lib:<factor_id>"`）、Alpha 目录编号（`"alpha101:<n>"` / `"alpha158:<n>"`），
或字典 `{"name": ..., "expr": ...}` / `{"name": ..., "id": ...}`。

### 响应（结构）

```jsonc
{
  "method": "ridge",            // the chosen scheme (auto resolves it)
  "weight_kind": "weight",      // "weight" (linear) | "importance" (learned model)
  "standardize": "zscore",
  "horizon": 1,
  "factor_names": ["rank(close)", "alpha101_1", "mom"],
  "weights":      { "rank(close)": 0.41, "alpha101_1": 0.33, "mom": 0.26 },
  "orientation":  { "rank(close)": 1.0,  "alpha101_1": -1.0, "mom": 1.0 },
  "per_factor_train_ic": { "...": 0.012 },
  "train": { "n_dates": 170, "ic": 0.018, "icir": 0.21, "rank_ic": 0.020, "rank_icir": 0.24 },
  "val":   { "...": "..." },
  "test":  { "...": "..." },     // the headline, out-of-sample numbers
  "selection": { "equal": 0.18, "ridge": 0.21, "...": "..." },  // present when method="auto"
  "splits": { "train": ["..."], "val": ["..."], "test": ["..."], "embargo": 10 },
  "resolved_factors": [{ "name": "...", "expr": "..." }],
  "dropped": [{ "name": "alpha101_25", "failure_mode": "..." }],  // didn't evaluate
  "diagnostics": { "composite_turnover_1d": 0.08, "...": "..." }
}
```

非有限浮点数为 `null`（JSON 安全）。若无数据 / 没有因子求值成功，
载荷为 `{"failure": "NO_DATA" | "NO_FACTORS", "detail": "..."}`（HTTP 200）；
无效的旋钮（错误的 `method` / `standardize` / 超范围字段）为 HTTP 422。

---

## SDK

```python
from assay.service import AssayService
from assay.config import AssayConfig

svc = AssayService(AssayConfig())
out = svc.combine_factors(
    ["rank(close)", "alpha101:1", "lib:<id>"],
    train=("2025-01-02", "2025-10-31"),
    val=("2025-11-01", "2026-01-31"),
    test=("2026-02-01", "2026-06-09"),
    universe="NASDAQ100", horizons=[1, 5, 10],
    method="auto",                       # or any name from combination_methods()
    # method="lightgbm", model_params={"n_estimators": 500},
)
print(out["method"], out["test"]["ic"], out["test"]["icir"])

print(svc.combination_methods())          # [{name, kind, available}, ...]
```

纯 numpy 内核（无需引擎，可独立复用）是
`assay.evaluator.combine_factors(...)`，它返回一个 `CombinationResult`，其
`.combined` 就是 `(T, N)` 复合信号。

---

## 说明与注意事项

- **数据窗口。** 内置的美股数据覆盖特定区间——传入的切分窗口须落在其中
  （WebUI 会依据已导入的区间自动填充；CLI/SDK 不会）。
- **`auto` 候选集**默认为廉价的解析方法。若要让 `auto` 也尝试模型，
  可传入包含模型名称的 `candidate_methods=[...]`。
- **线程。** 树/提升模型以受限的 worker 数拟合——池化设计规模较小，
  而无限制的 `n_jobs=-1` 会在多核主机上过度占用 OpenMP。
- **回测复合因子。** 复合因子是*学习得到的信号*，而非表达式，因此它
  尚不能直接馈入组合回测器（后者接受表达式）。`(T, N)` 复合信号可在
  内核结果上获取，供自定义的下游用途。
