# Precompute & Common Sub-Expression Elimination (CSE)

When you evaluate **many** factors over the same panel — a sweep, a library
re-score, a combination search — they share sub-expressions. `Sub(high, low)`,
`Mean(volume, 20)`, `Ref(close, 1)` recur in *thousands* of otherwise-distinct
factors. Computing each shared subtree once and reusing the result is the single
biggest lever on batch throughput.

Assay does this two ways, both keyed off the AST's stable
[`struct_hash`](../design/engineering.md): two structurally identical subtrees —
regardless of which surface syntax produced them — share a hash and are computed
once.

1. **In-process CSE** — `FactorEngine.evaluate_many([...])` threads one
   structural-hash memo across every expression, so a subtree shared across the
   batch is evaluated exactly once. Zero setup, always correct.
2. **Persistent precompute** — `PrecomputeStore` mines the corpus's hottest
   subtrees, **materialises each one for every asset**, and stores the `(T, N)`
   matrix on disk. Subsequent batches load those subtrees instead of recomputing
   them — across *runs* and *processes*.

Both are **bit-for-bit identical** to per-factor `evaluate()` (verified: `max|Δ| =
0`, identical NaN pattern).

### Measured

A 1,200-factor sample from the sweep corpus over a 400×150 panel (structural
sharing ratio 2.3×):

| Path | Throughput | Speedup |
|---|---|---|
| naive per-factor `evaluate()` | 5 factors/s | base |
| `evaluate_many()` (in-process CSE) | 6 factors/s | **1.20×** |
| `evaluate_many(precompute=…)` warm | 6 factors/s | 1.18× |

The in-process CSE speedup **scales with how much the corpus overlaps** — a random
1,200-factor sample shares less than the full 15k corpus, where the hottest subtrees
recur thousands of times (`sub(high, low)` ×4,667). The persistent precompute pays
off across **repeated** sweeps over a stable panel (build once at ~1s; reuse across
runs and processes) and when *expensive* subtrees recur; for a single sweep whose
shared subtrees are cheap it is roughly neutral (disk load ≈ recompute). Pick the
tool to the workload — see the table at the end.

---

## Find the common sub-expressions

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

- **count** — total occurrences across the corpus.
- **score** — `count × (n_nodes − 1)`, the operator-evaluations saved by computing
  the subtree once and reusing it everywhere (the ranking key).

SDK: `service.common_subexpressions(corpus, top_k=100)` →
`[{expr, count, n_factors, n_nodes, score}]`.

---

## Build the precompute store

```bash
python -m assay.cli precompute build \
  --corpus _assay_sweep/factors_canonical_unique.txt \
  --universe NASDAQ100 --start 2025-01-02 --end 2026-06-09 --top-k 512
python -m assay.cli precompute stats
```

This mines the top-`k` shared subtrees and computes each one for **all assets**,
storing the matrices under `<data_dir>/precompute/`. Building is itself
CSE-accelerated (nested shared subtrees compute once).

SDK:

```python
import assay
svc = assay.init()

info = svc.build_precompute(
    "_assay_sweep/factors_canonical_unique.txt",
    universe="NASDAQ100", period=("2025-01-02", "2026-06-09"), top_k=512,
)
print(info["built"], "subexprs |", info["est_evals_saved"], "evals saved / pass")
```

### Auto-update by history

The on-disk key folds in `FactorEngine.panel_fingerprint()` — a digest of the
panel's **dates × symbols × fields**. Ingest new history and the fingerprint
changes, so stale entries simply stop matching (a miss) and the store rebuilds for
the new panel. No manual invalidation; entries are stable and shared within a panel.

### Coupled to the daily data update

The hot cache is wired into the data pipeline so it stays aligned with the live
data **automatically**:

- After a successful **data-update job** (`POST /v1/admin/data/jobs`), the same job
  refreshes the precompute store for that market's primary universe, over the
  **freshly-ingested data range**, mining the **factor library** as the corpus
  (`AssayService.refresh_precompute_for_market`). An `init` run refreshes every
  universe.
- Each build writes a **manifest** recording its validity period, `as_of`,
  fingerprint, build time and the latest ingested date — so freshness is a cheap
  date comparison (no panel load).
- The **data manager page** (Admin → Data Manager) shows a *Hot cache* card: per
  universe, the validity period, entry count, top sub-expressions, and a
  **fresh / stale** badge (stale = the data advanced since the cache was built).
  A **Rebuild US / Rebuild CN** button queues a full rebuild of every universe.

```
GET  /v1/admin/cache/status     -> {store:{entries,bytes,dir}, scopes:[{universe, period, as_of,
                                     fingerprint, built_at, data_latest, n_entries, fresh, top}]}
POST /v1/admin/cache/rebuild     {market}  -> queues a background rebuild job
```

`precompute_status()` marks a scope `fresh` only when its recorded `data_latest`
still equals the market's current latest ingested date — i.e. the cache is aligned
to the live data validity period.

---

## Use it to accelerate a sweep

```python
# raw factor VALUES for a big corpus, CSE + precompute accelerated
results = svc.evaluate_many(exprs, universe="NASDAQ100",
                            period=("2025-01-02", "2026-06-09"), use_precompute=True)
for r in results:          # r is a FactorResult: r.values is the (T, N) matrix
    ...
```

At the engine level (no service / data layer):

```python
from assay.engine import FactorEngine, PrecomputeStore

eng = FactorEngine(panel)
store = PrecomputeStore("/path/to/precompute")
store.build(eng, corpus_exprs, top_k=512)          # once

bound = store.bind(eng.panel_fingerprint())
results = eng.evaluate_many(exprs, precompute=bound)  # warm: hot subtrees loaded, not recomputed
print(f"{bound.hit_rate:.0%} of subtree lookups served from precompute")
```

`evaluate_many` returns one `FactorResult` per input (values only — no IC metrics).
A malformed expression raises, exactly like `evaluate()`; pre-filter with
`engine.diagnose()` if you need soft failures.

---

## When to use which

| Situation | Use |
|---|---|
| One factor | `evaluate()` |
| A batch, occasional | `evaluate_many()` (in-process CSE — free) |
| Repeated sweeps over a stable panel | `precompute build` once, then `evaluate_many(..., use_precompute=True)` |
| Scoring (IC / decay / turnover), not raw values | `batch()` (per-factor metrics) |

---

## How it works (internals)

- `assay.engine.cse.common_subexpressions(exprs)` — parse the corpus, walk every
  AST, aggregate subtrees by `struct_hash`, rank by recompute saved.
- `FactorEngine._eval_cse(node, ctx, memo, precompute)` — memoised evaluation: each
  `OpNode` result is cached by its `struct_hash`; a bound `precompute` short-circuits
  a subtree by loading its matrix from disk. Leaves (fields / literals) cost nothing
  and are never cached.
- `PrecomputeStore` — content-addressed `(hash, fingerprint) → (T, N).npy`, sharded
  and written atomically (temp file + `os.replace`), best-effort (a corrupt file
  reads as a miss). Mirrors the L2 factor cache.

Correctness rests on operator **purity**: kernels return new arrays and never mutate
inputs, so sharing one array object across many parents is safe — the same
assumption the engine already makes when it reuses a field matrix across every
reference within a single evaluation.
