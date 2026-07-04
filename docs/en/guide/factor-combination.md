# Factor Combination

Blend several single factors into one **composite alpha** and score it honestly
out-of-sample. The combination layer follows the standard quant-research protocol —
**fit on train, select on validation, report on test** — so the test IC you read is
not contaminated by the fitting.

It is exposed three ways over one shared engine:

- **WebUI** — the *Factor Combination* tab.
- **REST** — `POST /v1/combination` (and `GET /v1/combination/methods`).
- **SDK** — `AssayService.combine_factors(...)`.

---

## Pipeline

For each rebalance the constituents go through:

1. **Standardize** — every factor is z-scored (`zscore`) or ranked (`rank`)
   cross-sectionally per day, so factors on different scales blend sanely.
   NaN-aware: a symbol missing on a date stays out of that day's cross-section.
2. **Orient** — each factor is flipped to point at **positive train IC**; the
   orientation (`+1` / `-1`) is reported so the weights stay readable.
3. **Fit on TRAIN only** — combination weights (or a model) are learned on the
   train window.
4. **Select on VALIDATION** — with `method="auto"`, every candidate is fit on
   train and the one with the best **validation ICIR** is kept.
5. **Score on TEST** — the frozen composite's IC / RankIC / ICIR is reported on the
   untouched test window.
6. **Embargo** — the last `embargo` days of the train and validation blocks are
   purged (default = the largest forward horizon) so an overlapping horizon-`h`
   label can never leak across a split boundary (the "purged" split).

The composite is a `(T, N)` signal evaluated everywhere with the **same** weights;
only the *fit* used the train window.

---

## Methods

### Analytic / optimization (always available — numpy + scipy)

| Method | What it does |
|---|---|
| `equal` | equal blend of the oriented factors |
| `ic_weight` | weight ∝ mean train IC magnitude |
| `icir_weight` | weight ∝ train ICIR (mean IC / std IC) — **default**; rewards *stable* predictors |
| `ols` | pooled cross-sectional regression of forward returns on the factors |
| `ridge` | L2-regularized regression (`ridge_lambda`); robust to collinear factors |
| `nnls` | **non-negative least squares** — a constrained (long-only) optimization |
| `max_icir` | Grinold's `Σ⁻¹·IC̄` — the linear blend that **maximizes the combination's IC information ratio** |

These produce an explicit per-factor **weight**; the composite is the linear blend
`Σ wₖ·factorₖ`.

### Learned models (qlib-style — optional libraries)

The composite is the model's **prediction** of the forward return from the oriented
factors, so the per-factor numbers reported are **feature importances**, not linear
weights.

| Method | Library | Kind |
|---|---|---|
| `linear`, `lasso`, `elastic_net` | scikit-learn | linear |
| `random_forest`, `extra_trees` | scikit-learn | tree ensemble |
| `gbrt`, `hist_gbrt` | scikit-learn | gradient boosting |
| `mlp` | scikit-learn | neural net |
| `lightgbm` | lightgbm | gradient boosting |
| `xgboost` | xgboost | gradient boosting |

A model appears in `GET /v1/combination/methods` (and the WebUI dropdown) only when
its library is installed:

```bash
pip install scikit-learn lightgbm xgboost
```

Hyper-parameters can be overridden per call via `model_params` (e.g.
`{"n_estimators": 500, "max_depth": 6, "learning_rate": 0.03}`); sensible defaults
are used otherwise, and every model is fit with a fixed seed so a run is a pure
function of its inputs.

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

## Saving & reloading runs

Every run auto-saves as the rolling "last run"; save a named, reloadable record — with the
fitted model (weights/importances, orientation, selection scores, scorecard) — and list or
reload them later without recomputing.

```bash
curl -X POST   localhost:8000/v1/combination/saved -d '{"name":"my blend","result":<result-json>}'
curl           localhost:8000/v1/combination/saved            # list summaries
curl           localhost:8000/v1/combination/saved/<id>       # full record (the model)
curl -X DELETE localhost:8000/v1/combination/saved -d '{"ids":["<id>"]}'
```

SDK equivalents on `AssayService`: `save_combination(result, name)`, `list_combinations()`,
`get_combination(id)`, `delete_combinations(ids)` — backed by
[`CombinationStore`](../../../src/assay/library/combination_store.py) under `<data_dir>/combinations/`.

**Factor specs** accept: a bare expression (`"rank(close)"`), a library reference
(`"lib:<factor_id>"`), an Alpha catalog number (`"alpha101:<n>"` / `"alpha158:<n>"`),
or a dict `{"name": ..., "expr": ...}` / `{"name": ..., "id": ...}`.

### Response (shape)

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

Non-finite floats are `null` (JSON-safe). If no data / no factor evaluates, the
payload is `{"failure": "NO_DATA" | "NO_FACTORS", "detail": "..."}` (HTTP 200);
an invalid knob (bad `method` / `standardize` / out-of-range field) is HTTP 422.

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

The pure-numpy kernel (engine-free, reusable in isolation) is
`assay.evaluator.combine_factors(...)`, which returns a `CombinationResult` whose
`.combined` is the `(T, N)` composite signal.

---

## Notes & caveats

- **Data window.** The bundled US data covers a specific range — pass split windows
  inside it (the WebUI auto-fills them from the ingested range; CLI/SDK do not).
- **`auto` candidate set** defaults to the cheap analytic schemes. To let `auto`
  also try models, pass `candidate_methods=[...]` including model names.
- **Threading.** Tree/boosting models are fit with a bounded worker count — the
  pooled design is small, and unbounded `n_jobs=-1` oversubscribes OpenMP on
  many-core hosts.
- **Backtesting the composite.** The composite is a *learned signal*, not an
  expression, so it does not yet feed directly into the portfolio backtester (which
  takes an expression). The `(T, N)` composite is available on the kernel result for
  custom downstream use.
