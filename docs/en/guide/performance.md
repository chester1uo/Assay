# Performance & Caching

The factor-evaluation hot path is benchmarked over the WorldQuant **Alpha-101** catalog,
comparing **cache vs no-cache** regimes. The benchmark is
[`scripts/bench_alpha101.py`](../../../scripts/bench_alpha101.py); a guard test lives at
[`tests/performance/test_performance.py`](../../../tests/performance/test_performance.py).

## Caches

| Cache | What it reuses | Where |
|---|---|---|
| **warm engine / session** | the loaded price panel + field-matrix pivots | one `FactorEngine` reused across factors; `assay.Session`; `/v1/session/create` |
| **L2 disk cache** | computed `(T, N)` factor-result matrices, keyed by (expr, universe, period, adj, market) | `assay.cache.L2FactorCache` under `<data_dir>/cache` |

## Run the benchmark

```bash
# synthetic panel (offline, deterministic, runs all 101 alphas)
PYTHONPATH=src python scripts/bench_alpha101.py --synthetic --n 200 --t 504

# real ingested data (uses ASSAY_DATA_DIR; runs the alphas whose fields exist)
PYTHONPATH=src python scripts/bench_alpha101.py --real --start 2025-01-02 --end 2026-06-09

# the guard test (prints the report; -s to see it)
PYTHONPATH=src python -m pytest -m performance -s
```

Every regime is **correctness-checked**: it must produce identical factor matrices
(`max|Δ| = 0`) — caching may never change a result.

## Representative numbers

**Synthetic, all 101 alphas (300 dates × 120 symbols):**

| regime | ms/factor | factors/s | speedup |
|---|---|---|---|
| no-cache (fresh engine/factor) | 91.2 | 11 | base |
| warm engine (panel/pivot cache) | 43.6 | 23 | **2.1×** |
| L2 warm (load from disk) | 0.13 | 7,725 | **704×** |

**Real data — NASDAQ-100, 2025-01-02…2026-06-09 (359 × 101, 52 runnable alphas):**

| regime | ms/factor | factors/s | speedup |
|---|---|---|---|
| no-cache (`from_store` per factor) | 168 | 6 | base |
| warm engine (session cache) | ~36–49 | 20–28 | **3.4–4.6×** |

Two stories: reusing a warm engine amortizes the ~200 ms parquet panel load (the
engineering-doc §8.2 effect — strongest on real data), and the **L2 result cache makes
re-running an evaluated factor set ~700× faster** (disk load vs recompute). Only 52 of 101
alphas run on real data (no `vwap`/`market_cap`); all 101 run on the synthetic panel.

## Notes

- `cvxpy`/`scikit-learn` are not dependencies; the optimization paths use scipy + numpy.
- The performance test asserts only the timing-robust facts (correctness + the huge L2 margin);
  the warm-engine speedup is reported but not asserted (it's negligible on tiny panels and so
  would be timing-flaky — it shows clearly on real data via the script).
- The L2 cache is currently exercised by the benchmark and tests; wiring it into
  `AssayService.evaluate` (so real backtests get the result-cache win automatically) is a
  natural follow-up.

Background: [engineering.md](../design/engineering.md) §5 (cache system) and §8 (performance).
