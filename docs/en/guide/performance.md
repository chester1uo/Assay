# Performance & Caching

The default local-data research backend reuses market panels and factor values across
experiments. Each experiment still runs its diagnostics and portfolio evaluation and
writes its own persistent research record.

## Research execution reuse

| Layer | Reused work | Invalidation and limits |
|---|---|---|
| Panel and engine pool | Loaded prices, field matrices, membership, groups and forward returns | Store, universe, period, as-of date, adjustment and source-file revision; up to 4 idle retained contexts and an estimated 256 MiB retained-memory budget |
| Subexpression memo | Raw built-in operator results shared by factor expressions | Per engine, bounded to 32 MiB; operator replacement clears the memo |
| L2 factor values | Computed factor matrices across process restarts | Observed panel fingerprint, expression, operator signature and implementation digest; atomic disk publication |

The memory estimate includes panels, engine/session arrays, additional forward-return
horizons and the reserved memo capacity. Active leases can temporarily exceed the
retention budget; idle entries are evicted when a lease finishes. Calls sharing one
engine are serialized, while different retained panels can be evaluated concurrently.

Subexpressions are cached before output membership masking, so rolling operators retain
history from before a security joined the universe. Custom or replaced operators bypass
factor-value caching. Portfolio reports and research records are recomputed independently.
New request IDs consume the research budget even when the expression is cached; retrying
an existing request ID preserves the original attempt.

This path applies to the native `DataStore` through the research backend, including its
REST, MCP and SDK entry points. Custom stores and `QFVPanelStore` retain their existing
execution behavior. A direct call to `AssayService.evaluate` does not acquire this pool;
explicit sessions and the standalone L2 cache remain available separately.

### Source updates

Reuse supports read-only market data or the canonical ingester's atomic file replacement.
File revision checks cover prices, membership, adjustments, groups and trade status and
run again after loading or waiting for an engine. A change during these checks fails the
attempt so it can be retried with a fresh panel.

The revision token uses file metadata, not a content scan on every request. External
writers that modify files in place while preserving metadata must disable panel reuse:

```python
from assay.research import AssayBackend

service.research.backends["assay"] = AssayBackend(service, reuse_panels=False)
```

The pool does not create a transactional snapshot of a dataset being updated concurrently.
Use a stable published dataset revision for reproducible experiments.

### Inspect reuse

```python
backend = service.research.backends["assay"]
print(backend.execution_pool.stats())
# panel_hits, panel_builds, evictions, entries, retained_bytes,
# factor_computations, factor_disk_hits, subtree_hits
```

Hit and build counters span the pool lifetime. Factor counters describe currently
retained entries and reset when those entries are evicted.

## Measured workloads

Three repetitions on a local NASDAQ-100 2023 daily panel gave these median times.
The complete research task evaluates 24 fixed factors with diagnostics, weekly
cost-adjusted portfolio reports and SQLite writes; model generation is excluded.

| Complete Assay research task | Before reuse | Current | Speedup |
|---|---:|---:|---:|
| 24 new expressions | 9.451 s | 3.969 s | 2.38× |
| 24 repeated expressions, new attempts | 9.194 s | 3.715 s | 2.47× |

All 288 attempts completed. The 144 before/after pairs had identical IC, RankIC and NAV.
Each current run built one panel for 48 attempts and computed each of the 24 expressions once.

The separate factor-execution comparison uses 250 dates × 111 historical members,
24 formulas, one thread, prepared data and matched float32-representable inputs:

| Native factor-execution path | 24 new formulas | Same 24 formulas, cached |
|---|---:|---:|
| Assay factor engine | 0.0538 s | 0.0122 s |
| Qlib 0.9.7 `D.features` | 1.1887 s | 0.4424 s |
| RD-Agent `FactorFBWorkspace.execute` | 11.7185 s | 0.0688 s |

Qlib's native feature cache is expanded to 20,000 entries, sufficient for this workload;
an external Redis disk cache is not configured. RD-Agent's native signed result cache is
enabled and its repeated runs hit that cache. Preparation and process-wide imports are
recorded separately. Full-window and historical-membership masks are applied to every
implementation and included in timing. Values pass `rtol=3e-5, atol=3e-5`; raw masks are
also retained because native missing-value conventions differ. These are component
measurements, not complete Qlib or RD-Agent research-loop timings.

A separate fixed daily execution replay uses 249 dates, 90 fully observed securities,
whole-share target quantities and zero costs. Assay's accountant took 0.00914 s versus
0.72349 s for Qlib's native simulator. Both matched an independent cash/share NAV ledger
within `2.45e-15`. Qlib quote construction (0.29270 s) is excluded. The native paths produce
different report structures; this replay measures the shared fixed execution workload.

Reproduction commands and measurement boundaries:
[efficiency benchmarks](../../../experiments/efficiency/README.md).

## Alpha-101 cache microbenchmark

The standalone Alpha-101 benchmark remains available for synthetic and real panels:

```bash
PYTHONPATH=src python scripts/bench_alpha101.py --synthetic --n 200 --t 504
PYTHONPATH=src python scripts/bench_alpha101.py --real --start 2025-01-02 --end 2026-06-09
PYTHONPATH=src python -m pytest -m performance -s
```

It checks cache/no-cache matrix equality. Its disk-hit timing excludes the diagnostics,
portfolio construction and persistence included in a complete research attempt.

Background: [cache design](../design/engineering.md) and
[precomputation and CSE](precompute-cse.md).
