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
