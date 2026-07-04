# 性能与缓存

因子求值的热路径以 WorldQuant **Alpha-101** 目录为基准做了测评，
对比**有缓存与无缓存**两种情形。基准脚本为
[`scripts/bench_alpha101.py`](../../../scripts/bench_alpha101.py)；一个守卫测试位于
[`tests/performance/test_performance.py`](../../../tests/performance/test_performance.py)。

## 缓存

| Cache | What it reuses | Where |
|---|---|---|
| **warm engine / session** | 已加载的价格面板 + 字段矩阵的透视 | 一个 `FactorEngine` 在多个因子间复用；`assay.Session`；`/v1/session/create` |
| **L2 disk cache** | 已计算的 `(T, N)` 因子结果矩阵，以 (expr, universe, period, adj, market) 为键 | `assay.cache.L2FactorCache`，位于 `<data_dir>/cache` 下 |

## 运行基准

```bash
# synthetic panel (offline, deterministic, runs all 101 alphas)
PYTHONPATH=src python scripts/bench_alpha101.py --synthetic --n 200 --t 504

# real ingested data (uses ASSAY_DATA_DIR; runs the alphas whose fields exist)
PYTHONPATH=src python scripts/bench_alpha101.py --real --start 2025-01-02 --end 2026-06-09

# the guard test (prints the report; -s to see it)
PYTHONPATH=src python -m pytest -m performance -s
```

每种情形都经过**正确性校验**：它必须产生完全相同的因子矩阵
（`max|Δ| = 0`）——缓存绝不能改变结果。

## 代表性数值

**合成面板，全部 101 个 alpha（300 个日期 × 120 个代码）：**

| regime | ms/factor | factors/s | speedup |
|---|---|---|---|
| no-cache (fresh engine/factor) | 91.2 | 11 | base |
| warm engine (panel/pivot cache) | 43.6 | 23 | **2.1×** |
| L2 warm (load from disk) | 0.13 | 7,725 | **704×** |

**真实数据——NASDAQ-100，2025-01-02…2026-06-09（359 × 101，52 个可运行的 alpha）：**

| regime | ms/factor | factors/s | speedup |
|---|---|---|---|
| no-cache (`from_store` per factor) | 168 | 6 | base |
| warm engine (session cache) | ~36–49 | 20–28 | **3.4–4.6×** |

两个层面的故事：复用一个热引擎会摊薄约 200 ms 的 parquet 面板加载
（engineering 文档 §8.2 的效应——在真实数据上最显著），而 **L2 结果缓存使
重新运行一个已求值的因子集快约 700×**（磁盘加载 vs 重新计算）。真实数据上
101 个 alpha 中只有 52 个可运行（没有 `vwap`/`market_cap`）；合成面板上全部 101 个都能运行。

## 说明

- `cvxpy`/`scikit-learn` 不是依赖项；优化路径使用 scipy + numpy。
- 性能测试只断言对计时稳健的事实（正确性 + 巨大的 L2 优势）；
  热引擎加速会被报告但不做断言（在极小的面板上它可忽略不计，因而计时会不稳定——
  它在真实数据上通过脚本清晰显现）。
- L2 缓存目前由基准和测试来演练；将其接入
  `AssayService.evaluate`（使真实回测自动获得结果缓存的收益）是一个
  自然的后续工作。

背景：[engineering.md](../design/engineering.md) §5（缓存系统）和 §8（性能）。
