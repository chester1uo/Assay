"""Multi-factor combination with train / validation / test evaluation (§6.3).

A factor *combination* blends several single-factor signals into one composite
alpha. This module is the pure-numpy engine that most quant factor systems sit on:
it standardises each factor cross-sectionally, **orients** it to be positively
predictive, **learns combination weights on a train window only**, optionally
**selects the scheme on a validation window**, and reports the composite's
predictive quality **out-of-sample on a test window** — the canonical train/val/
test protocol that keeps the reported test IC honest (no peeking).

Like every :mod:`assay.evaluator` module this is a *pure function* over aligned
``(T, N)`` float matrices (``T`` dates on axis 0, ``N`` symbols on axis 1): it
imports neither the engine nor the data layer. The caller materialises the factor
matrices (e.g. via :class:`assay.engine.FactorEngine`) and the forward-return
panels (:func:`assay.evaluator.forward_returns`) and hands them here already on a
shared grid. :meth:`AssayService.combine_factors` is the wiring that does that.

Pipeline (design-doc §6.3):

1. **Standardise** every factor per date — ``zscore`` (demean / std) or ``rank``
   (cross-sectional percentile mapped to ``[-1, 1]``) — so factors on different
   scales blend sanely. NaN-aware: a symbol missing on a date stays NaN.
2. **Orient** each factor by the *sign of its train IC* so every factor points the
   same (positive) way; the orientation is reported so the weights are readable.
3. **Fit weights on the train window** by one of:

   * ``equal``       — ``1/K`` each (after orientation: an equal blend of the
     predictive directions);
   * ``ic_weight``   — ∝ mean train IC magnitude;
   * ``icir_weight`` — ∝ train ICIR (mean IC / std IC) — the default, rewarding
     *stable* predictors over merely strong ones;
   * ``ols``         — pooled cross-sectional regression of forward returns on the
     oriented factors (the classic multi-factor "factor-return" weights);
   * ``ridge``       — L2-regularised regression (``ridge_lambda``), robust when
     factors are collinear;
   * ``max_icir``    — ``Σ⁻¹·IC̄`` (Grinold), the linear blend that maximises the
     combination's IC information ratio, with a ridge on ``Σ`` for stability.

4. **Select** (when ``method='auto'``): fit every candidate on train, score each on
   the **validation** window, keep the best by validation ICIR.
5. **Evaluate** the frozen composite on train / val / **test** with the existing
   IC / RankIC / ICIR kernels (:mod:`assay.evaluator.metrics`).

To stop a horizon-``h`` forward label from leaking across a split boundary,
:func:`make_splits` purges (embargoes) the last ``embargo`` dates of the train and
validation blocks — the "purged" split practitioners use for overlapping labels.
"""

from __future__ import annotations

import contextlib
import os
import warnings
from dataclasses import dataclass, field
from typing import Sequence

import numpy as np

from assay.evaluator.metrics import ic_series, ic_summary, rank_ic_series
from assay.evaluator.turnover import turnover as _turnover

# Bounded worker count for the tree / boosting models. The pooled cross-sectional
# design is small (≈ n_dates × n_symbols rows, a handful of features), so the
# embarrassingly-parallel libraries (RF / LightGBM / XGBoost) gain nothing from
# many threads and *lose* badly to OpenMP oversubscription on a high-core host —
# an unbounded ``n_jobs=-1`` can hang for minutes. Cap it small.
_N_JOBS = max(1, min(4, (os.cpu_count() or 2)))

__all__ = [
    "CombinationResult",
    "SplitMetrics",
    "standardize_xs",
    "make_splits",
    "combine_factors",
    "available_methods",
    "COMBINATION_METHODS",
    "ANALYTIC_METHODS",
    "MODEL_METHODS",
]

# Analytic / optimization weighting schemes — always available (numpy + scipy).
# These produce an explicit per-factor *weight* and the composite is the linear
# blend ``Σ wₖ·factorₖ`` (design-doc §6.3):
#   equal/ic_weight/icir_weight — heuristic IC weighting;
#   ols/ridge/nnls              — (constrained) cross-sectional regression;
#   max_icir                    — Grinold's Σ⁻¹·IC̄ optimal-ICIR blend.
ANALYTIC_METHODS = (
    "equal",
    "ic_weight",
    "icir_weight",
    "ols",
    "ridge",
    "nnls",
    "max_icir",
)

# qlib-style learned models — the composite is the model's *prediction* of the
# forward return from the oriented factors (not a linear blend), so per-factor
# numbers reported are feature *importances*. Each entry maps the method name to
# ``(import_module, kind)``; a method whose library is not installed is reported
# unavailable by :func:`available_methods` and raises a clear error if requested.
MODEL_METHODS = {
    "linear":        ("sklearn", "linear"),     # OLS via sklearn (parity w/ 'ols')
    "lasso":         ("sklearn", "linear"),     # L1 sparse linear
    "elastic_net":   ("sklearn", "linear"),     # L1+L2 linear
    "random_forest": ("sklearn", "tree"),       # bagged trees
    "extra_trees":   ("sklearn", "tree"),       # extremely-randomised trees
    "gbrt":          ("sklearn", "boost"),      # sklearn gradient boosting
    "hist_gbrt":     ("sklearn", "boost"),      # sklearn histogram GBDT (fast)
    "mlp":           ("sklearn", "neural"),     # small MLP regressor
    "lightgbm":      ("lightgbm", "boost"),     # LightGBM GBDT
    "xgboost":       ("xgboost", "boost"),      # XGBoost GBDT
}

# Every method the module can name (analytic first, then learned models).
COMBINATION_METHODS = ANALYTIC_METHODS + tuple(MODEL_METHODS)

# Candidate set tried when ``method='auto'`` (validation-selected). Kept to the
# cheap analytic schemes by default; pass ``candidate_methods`` to include models.
_AUTO_CANDIDATES = ("equal", "icir_weight", "ic_weight", "ridge", "nnls", "max_icir")


def _have(module: str) -> bool:
    """True if ``module`` is importable (used to gate the optional model methods)."""
    import importlib.util

    return importlib.util.find_spec(module) is not None


def available_methods() -> list[dict]:
    """List every combination method with its kind and install availability.

    Returns ``[{name, kind, available}]`` — analytic schemes are always available;
    a learned model is ``available=False`` when its backing library (scikit-learn /
    lightgbm / xgboost) is not installed. Drives the WebUI method picker and the
    ``GET /v1/combination/methods`` endpoint.
    """
    out = [{"name": m, "kind": "analytic", "available": True} for m in ANALYTIC_METHODS]
    for name, (lib, kind) in MODEL_METHODS.items():
        out.append({"name": name, "kind": kind, "available": _have(lib)})
    return out


# ---------------------------------------------------------------------------
# result containers
# ---------------------------------------------------------------------------
@dataclass
class SplitMetrics:
    """Predictive quality of the composite on one split (NaN where undefined)."""

    n_dates: int
    ic: float
    icir: float
    rank_ic: float
    rank_icir: float
    ic_by_horizon: dict[int, float] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "n_dates": int(self.n_dates),
            "ic": _clean(self.ic),
            "icir": _clean(self.icir),
            "rank_ic": _clean(self.rank_ic),
            "rank_icir": _clean(self.rank_icir),
            "ic_by_horizon": {int(h): _clean(v) for h, v in self.ic_by_horizon.items()},
        }


@dataclass
class CombinationResult:
    """The fitted composite, its weights, and its train/val/test scorecard.

    ``combined`` is the ``(T, N)`` composite signal over the *full* panel (the same
    weights everywhere; only the *fit* used the train window). It is kept on the
    object for downstream use (e.g. a portfolio backtest of the composite) but is
    omitted from :meth:`to_dict` so the JSON payload stays small.
    """

    method: str
    standardize: str
    horizon: int
    factor_names: list[str]
    weights: dict[str, float]          # per-factor weight (analytic) or importance (model)
    weight_kind: str                   # 'weight' (linear blend) | 'importance' (learned model)
    orientation: dict[str, float]      # +1/-1 applied to point each factor at +IC
    per_factor_train_ic: dict[str, float]
    train: SplitMetrics
    val: SplitMetrics
    test: SplitMetrics
    combined: np.ndarray = field(repr=False, default=None)  # (T, N), not serialised
    selection: dict | None = None      # {candidate: val_icir} when method='auto'
    diagnostics: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        """JSON-safe summary (drops the ``(T, N)`` ``combined`` array)."""
        return {
            "method": self.method,
            "standardize": self.standardize,
            "horizon": int(self.horizon),
            "factor_names": list(self.factor_names),
            "weights": {k: _clean(v) for k, v in self.weights.items()},
            "weight_kind": self.weight_kind,
            "orientation": {k: _clean(v) for k, v in self.orientation.items()},
            "per_factor_train_ic": {k: _clean(v) for k, v in self.per_factor_train_ic.items()},
            "train": self.train.to_dict(),
            "val": self.val.to_dict(),
            "test": self.test.to_dict(),
            "selection": (
                {k: _clean(v) for k, v in self.selection.items()}
                if self.selection is not None
                else None
            ),
            "diagnostics": self.diagnostics,
        }


def _clean(x) -> float | None:
    """NaN/inf -> None (JSON-safe); finite floats pass through."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if np.isfinite(v) else None


# ---------------------------------------------------------------------------
# 1. cross-sectional standardisation
# ---------------------------------------------------------------------------
def standardize_xs(mat: np.ndarray, method: str = "zscore") -> np.ndarray:
    """Standardise each date's cross-section of a ``(T, N)`` factor (NaN-aware).

    ``zscore`` removes the per-date mean and divides by the per-date std (``ddof=0``;
    a constant row -> all-zero, contributing no signal that date). ``rank`` maps the
    per-date percentile rank into ``[-1, 1]`` (robust to outliers; the median name
    -> ~0). NaNs are preserved so a symbol missing on a date never poisons the row.
    """
    x = np.asarray(mat, dtype=np.float64)
    if x.ndim != 2:
        raise ValueError("standardize_xs expects a 2-D (T, N) matrix")
    if method == "zscore":
        with _quiet(), np.errstate(invalid="ignore"):
            mean = np.nanmean(x, axis=1, keepdims=True)
            std = np.nanstd(x, axis=1, ddof=0, keepdims=True)
        out = np.where(std > 0, (x - mean) / np.where(std > 0, std, 1.0), 0.0)
        return np.where(np.isfinite(x), out, np.nan)
    if method == "rank":
        return np.vstack([_rank_pm1_row(x[t]) for t in range(x.shape[0])])
    raise ValueError(f"standardize method {method!r} invalid; expected 'zscore' or 'rank'")


@contextlib.contextmanager
def _quiet():
    """Suppress numpy's expected all-NaN-slice / empty-mean RuntimeWarnings.

    An all-NaN cross-section (a date where no symbol has the factor) deliberately
    standardises to NaN here; numpy's warning for that case is noise."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        yield


def _rank_pm1_row(row: np.ndarray) -> np.ndarray:
    """Cross-sectional percentile rank of one row mapped to ``[-1, 1]`` (NaN-aware)."""
    out = np.full(row.shape, np.nan)
    mask = np.isfinite(row)
    vals = row[mask]
    n = vals.size
    if n == 0:
        return out
    if n == 1:
        out[mask] = 0.0
        return out
    order = np.argsort(vals, kind="mergesort")
    ranks = np.empty(n)
    i = 0
    while i < n:  # average-rank tie handling
        j = i + 1
        while j < n and vals[order[j]] == vals[order[i]]:
            j += 1
        ranks[order[i:j]] = (i + j - 1) / 2.0
        i = j
    out[mask] = (ranks / (n - 1)) * 2.0 - 1.0  # [0,1] -> [-1,1]
    return out


# ---------------------------------------------------------------------------
# 2. train / val / test split masks (with label embargo)
# ---------------------------------------------------------------------------
def _date_key(d) -> str:
    """Coerce one date-like value to a sortable ``YYYY-MM-DD`` string."""
    return str(d)[:10]


def make_splits(
    dates: Sequence,
    train: tuple[str, str],
    val: tuple[str, str],
    test: tuple[str, str],
    *,
    embargo: int = 0,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Boolean ``(T,)`` masks for the train / val / test date windows.

    Each window is an inclusive ``(start, end)`` pair of ``YYYY-MM-DD`` strings;
    a date belongs to a window when ``start <= date <= end``. Windows should be
    chronological and non-overlapping (the function does not enforce ordering, but
    overlap would double-count dates). ``embargo`` purges the last ``embargo`` dates
    of the **train** and **validation** blocks so a horizon-``h`` forward label
    computed on the tail of one split cannot leak into the next (set it to the max
    holding horizon for overlapping labels).
    """
    keys = [_date_key(d) for d in dates]
    arr = np.array(keys)

    def _mask(window: tuple[str, str]) -> np.ndarray:
        lo, hi = window
        return (arr >= lo) & (arr <= hi)

    tr, va, te = _mask(train), _mask(val), _mask(test)
    if embargo > 0:
        tr = _embargo_tail(tr, embargo)
        va = _embargo_tail(va, embargo)
    return tr, va, te


def _embargo_tail(mask: np.ndarray, k: int) -> np.ndarray:
    """Drop the last ``k`` True positions of ``mask`` (label-leakage purge)."""
    out = mask.copy()
    idx = np.flatnonzero(out)
    if idx.size and k > 0:
        out[idx[-int(k):]] = False
    return out


# ---------------------------------------------------------------------------
# 3. weight fitting (train window only)
# ---------------------------------------------------------------------------
def _factor_ic_train(
    factor: np.ndarray, fwd: np.ndarray, train_mask: np.ndarray
) -> tuple[float, float, np.ndarray]:
    """Per-factor train ``(mean IC, ICIR, per-date IC series over train)``."""
    series = ic_series(factor, fwd)            # (T,) Pearson IC per date
    tr = series[train_mask]
    mean, icir = ic_summary(tr)
    return mean, icir, tr


def _orient(stack: np.ndarray, ics: np.ndarray) -> np.ndarray:
    """Per-factor orientation signs (+1 / -1) so each points at positive train IC."""
    signs = np.where(ics >= 0, 1.0, -1.0)
    signs[~np.isfinite(ics)] = 1.0  # an undefined IC keeps the factor as-is
    return signs


def _fit_weights(
    method: str,
    aligned: np.ndarray,            # (K, T, N) oriented, standardised factors
    fwd: np.ndarray,                # (T, N) headline-horizon forward returns
    train_mask: np.ndarray,         # (T,)
    abs_ic: np.ndarray,             # (K,) |mean train IC| per oriented factor
    icir: np.ndarray,               # (K,) train ICIR per oriented factor (>=0 after orient)
    ic_train_series: np.ndarray,    # (K, n_train) per-date train IC per oriented factor
    ridge_lambda: float,
) -> np.ndarray:
    """Combination weights ``(K,)`` for ``method``, fit on the train window only.

    All weight vectors are L1-normalised (``sum|w| == 1``) so schemes are comparable
    and the composite scale is irrelevant to the (scale-invariant) IC metrics.
    """
    k = aligned.shape[0]
    if k == 1:
        return np.array([1.0])

    if method == "equal":
        w = np.ones(k)
    elif method == "ic_weight":
        w = np.where(np.isfinite(abs_ic), abs_ic, 0.0)
    elif method == "icir_weight":
        w = np.where(np.isfinite(icir), np.maximum(icir, 0.0), 0.0)
    elif method == "max_icir":
        w = _max_icir_weights(ic_train_series, ridge_lambda)
    elif method in ("ols", "ridge"):
        w = _regression_weights(
            aligned, fwd, train_mask, ridge_lambda if method == "ridge" else 0.0
        )
    elif method == "nnls":
        w = _nnls_weights(aligned, fwd, train_mask)
    else:
        raise ValueError(
            f"analytic method {method!r} invalid; expected one of {ANALYTIC_METHODS}"
        )

    w = np.asarray(w, dtype=np.float64)
    if not np.all(np.isfinite(w)) or np.all(w == 0.0):
        w = np.ones(k)  # degenerate fit -> equal weight (never returns all-zero)
    s = np.abs(w).sum()
    return w / s if s > 0 else np.ones(k) / k


def _max_icir_weights(ic_train_series: np.ndarray, ridge_lambda: float) -> np.ndarray:
    """Grinold's ``Σ⁻¹·IC̄`` max-ICIR blend from the per-date train IC series ``(K, n)``.

    ``IC̄`` is the per-factor mean IC; ``Σ`` is the covariance of the factors' IC
    series (a small ridge keeps it invertible when factors' ICs are collinear).
    """
    s = np.asarray(ic_train_series, dtype=np.float64)
    # Use dates where every factor has a finite IC (joint-finite columns).
    ok = np.isfinite(s).all(axis=0)
    s = s[:, ok]
    k = s.shape[0]
    if s.shape[1] < 2:
        return np.ones(k)
    ic_bar = s.mean(axis=1)
    cov = np.cov(s, ddof=1)
    cov = np.atleast_2d(cov)
    scale = float(np.trace(cov) / k) if k else 1.0
    cov = cov + np.eye(k) * (ridge_lambda * 1e-4 * max(scale, 1e-12) + 1e-12)
    try:
        return np.linalg.solve(cov, ic_bar)
    except np.linalg.LinAlgError:
        return ic_bar


def _regression_weights(
    aligned: np.ndarray, fwd: np.ndarray, train_mask: np.ndarray, ridge_lambda: float
) -> np.ndarray:
    """Pooled cross-sectional (ridge) regression of forward returns on the factors.

    Stacks every train ``(date, symbol)`` complete case — finite forward return and
    finite across **all** oriented factors — into ``X (m, K)`` / ``y (m,)`` and
    solves ``(XᵀX + λI)⁻¹ Xᵀy`` (``λ = 0`` is OLS). No intercept: the standardised
    factors are already per-date demeaned, and the cross-sectional return level is
    not predictable from them.
    """
    k, _, n = aligned.shape
    # (m_rows, K) design over train rows that are finite in y and all factors.
    x_rows = aligned[:, train_mask, :].reshape(k, -1).T  # (n_train*N, K)
    y_rows = fwd[train_mask, :].reshape(-1)              # (n_train*N,)
    good = np.isfinite(y_rows) & np.isfinite(x_rows).all(axis=1)
    x = x_rows[good]
    y = y_rows[good]
    if x.shape[0] <= k:  # under-determined -> let the caller fall back to equal
        return np.zeros(k)
    xtx = x.T @ x
    xty = x.T @ y
    if ridge_lambda > 0.0:
        scale = float(np.trace(xtx) / k) if k else 1.0
        xtx = xtx + np.eye(k) * ridge_lambda * 1e-2 * max(scale, 1e-12)
    try:
        return np.linalg.solve(xtx, xty)
    except np.linalg.LinAlgError:
        w, *_ = np.linalg.lstsq(x, y, rcond=None)
        return w


def _train_design(aligned: np.ndarray, fwd: np.ndarray, train_mask: np.ndarray):
    """Pooled train ``(X (m, K), y (m,))`` complete-case design + a row-finite mask.

    Returns ``(x, y)`` over train ``(date, symbol)`` cells finite in the label and
    in **every** oriented factor — the design every regression / learned model
    consumes.
    """
    k = aligned.shape[0]
    x_rows = aligned[:, train_mask, :].reshape(k, -1).T
    y_rows = fwd[train_mask, :].reshape(-1)
    good = np.isfinite(y_rows) & np.isfinite(x_rows).all(axis=1)
    return x_rows[good], y_rows[good]


def _nnls_weights(aligned: np.ndarray, fwd: np.ndarray, train_mask: np.ndarray) -> np.ndarray:
    """Non-negative least squares of forward returns on the oriented factors (§6.3).

    A constrained (``wₖ ≥ 0``) optimisation — long-only factor weights — solved by
    :func:`scipy.optimize.nnls`. Because every factor is oriented to positive train
    IC, a non-negative blend is the natural long-only optimum.
    """
    from scipy.optimize import nnls

    k = aligned.shape[0]
    x, y = _train_design(aligned, fwd, train_mask)
    if x.shape[0] <= k:
        return np.zeros(k)
    try:
        w, _ = nnls(x, y)
        return w
    except Exception:  # noqa: BLE001 — degenerate design -> caller falls back to equal
        return np.zeros(k)


# ---------------------------------------------------------------------------
# 3b. learned models (qlib-style) — composite is the model's prediction
# ---------------------------------------------------------------------------
def _make_estimator(method: str, params: dict | None):
    """Build the scikit-learn / LightGBM / XGBoost regressor for ``method``.

    Deterministic (fixed ``random_state``) so a backtest stays a pure function of
    its inputs. ``params`` overrides the per-model defaults (e.g. ``n_estimators``,
    ``max_depth``, ``learning_rate``, ``alpha``). Raises a clear ``ValueError`` when
    the backing library is not installed.
    """
    p = dict(params or {})
    lib = MODEL_METHODS.get(method, (None, None))[0]
    if lib and not _have(lib):
        raise ValueError(
            f"combination method {method!r} needs the {lib!r} package "
            f"(pip install {lib}) — it is not installed"
        )

    def _g(key, default):
        return p.get(key, default)

    if method in ("linear", "lasso", "elastic_net"):
        from sklearn.linear_model import ElasticNet, Lasso, LinearRegression

        if method == "linear":
            return LinearRegression()
        if method == "lasso":
            return Lasso(alpha=_g("alpha", 1e-4), max_iter=int(_g("max_iter", 5000)))
        return ElasticNet(
            alpha=_g("alpha", 1e-4), l1_ratio=_g("l1_ratio", 0.5),
            max_iter=int(_g("max_iter", 5000)),
        )
    if method in ("random_forest", "extra_trees"):
        from sklearn.ensemble import ExtraTreesRegressor, RandomForestRegressor

        cls = RandomForestRegressor if method == "random_forest" else ExtraTreesRegressor
        return cls(
            n_estimators=int(_g("n_estimators", 200)),
            max_depth=_g("max_depth", None),
            min_samples_leaf=int(_g("min_samples_leaf", 50)),
            n_jobs=_N_JOBS, random_state=0,
        )
    if method == "gbrt":
        from sklearn.ensemble import GradientBoostingRegressor

        return GradientBoostingRegressor(
            n_estimators=int(_g("n_estimators", 200)), max_depth=int(_g("max_depth", 3)),
            learning_rate=_g("learning_rate", 0.05), subsample=_g("subsample", 0.8),
            random_state=0,
        )
    if method == "hist_gbrt":
        from sklearn.ensemble import HistGradientBoostingRegressor

        return HistGradientBoostingRegressor(
            max_iter=int(_g("n_estimators", 300)), max_depth=_g("max_depth", None),
            learning_rate=_g("learning_rate", 0.05), random_state=0,
        )
    if method == "mlp":
        from sklearn.neural_network import MLPRegressor

        return MLPRegressor(
            hidden_layer_sizes=tuple(_g("hidden_layer_sizes", (64, 32))),
            alpha=_g("alpha", 1e-3), max_iter=int(_g("max_iter", 300)), random_state=0,
        )
    if method == "lightgbm":
        import lightgbm as lgb

        return lgb.LGBMRegressor(
            n_estimators=int(_g("n_estimators", 300)), max_depth=int(_g("max_depth", -1)),
            num_leaves=int(_g("num_leaves", 31)), learning_rate=_g("learning_rate", 0.05),
            subsample=_g("subsample", 0.8), n_jobs=_N_JOBS, random_state=0, verbosity=-1,
        )
    if method == "xgboost":
        import xgboost as xgb

        return xgb.XGBRegressor(
            n_estimators=int(_g("n_estimators", 300)), max_depth=int(_g("max_depth", 4)),
            learning_rate=_g("learning_rate", 0.05), subsample=_g("subsample", 0.8),
            n_jobs=_N_JOBS, random_state=0, verbosity=0,
        )
    raise ValueError(f"learned model {method!r} unknown; expected one of {tuple(MODEL_METHODS)}")


def _importance(est, k: int) -> np.ndarray:
    """Per-factor importance ``(K,)`` from a fitted estimator (coef_ or tree gains).

    Linear models report ``|coef|``; tree / boosting models report
    ``feature_importances_``; anything opaque (e.g. the MLP) falls back to uniform.
    L1-normalised for display, mirroring the analytic ``weights``.
    """
    imp = getattr(est, "feature_importances_", None)
    if imp is None:
        coef = getattr(est, "coef_", None)
        imp = np.abs(np.asarray(coef, dtype=np.float64).reshape(-1)) if coef is not None else None
    if imp is None or np.asarray(imp).shape[0] != k:
        imp = np.ones(k)
    imp = np.asarray(imp, dtype=np.float64)
    s = np.abs(imp).sum()
    return imp / s if s > 0 else np.ones(k) / k


def _fit_predict_model(
    method: str, aligned: np.ndarray, fwd: np.ndarray, train_mask: np.ndarray,
    params: dict | None,
) -> tuple[np.ndarray | None, np.ndarray | None]:
    """Fit a learned model on the train cells and predict the composite everywhere.

    The composite is ``model.predict(oriented factors)`` at every ``(date, symbol)``
    cell finite across all factors (NaN elsewhere). Returns ``(combined (T, N),
    importance (K,))``, or ``(None, None)`` when the train design is too small to fit
    (the caller then falls back to an equal-weight blend).
    """
    k, t, n = aligned.shape
    x_tr, y_tr = _train_design(aligned, fwd, train_mask)
    if x_tr.shape[0] <= max(k + 1, 10):
        return None, None
    est = _make_estimator(method, params)
    x_all = aligned.reshape(k, -1).T                    # (T*N, K)
    finite = np.isfinite(x_all).all(axis=1)
    pred = np.full(x_all.shape[0], np.nan)
    with warnings.catch_warnings():  # quiet benign sklearn/lightgbm fit-time warnings
        warnings.simplefilter("ignore")
        est.fit(x_tr, y_tr)
        if finite.any():
            pred[finite] = est.predict(x_all[finite])
    return pred.reshape(t, n), _importance(est, k)


# ---------------------------------------------------------------------------
# 4. combine + score
# ---------------------------------------------------------------------------
def _combine_stack(aligned: np.ndarray, weights: np.ndarray) -> np.ndarray:
    """Weighted blend of oriented factors ``(K, T, N) -> (T, N)``, NaN-aware.

    Each cell sums ``w_k * factor_k`` over the factors finite there (NaN treated as
    absent); a cell where **every** factor is NaN stays NaN (no signal). This keeps
    the composite defined wherever at least one constituent has an opinion.
    """
    w = np.asarray(weights, dtype=np.float64).reshape(-1, 1, 1)
    finite = np.isfinite(aligned)
    contrib = np.where(finite, aligned, 0.0) * w
    combined = contrib.sum(axis=0)
    any_finite = finite.any(axis=0)
    return np.where(any_finite, combined, np.nan)


def _split_metrics(
    combined: np.ndarray,
    fwd_by_h: dict[int, np.ndarray],
    mask: np.ndarray,
    horizon: int,
) -> SplitMetrics:
    """IC / RankIC / ICIR of the composite restricted to ``mask`` dates."""
    n_dates = int(mask.sum())
    if n_dates == 0:
        return SplitMetrics(0, np.nan, np.nan, np.nan, np.nan, {})
    sub = combined[mask]
    by_h: dict[int, float] = {}
    for h, fwd in fwd_by_h.items():
        by_h[h] = ic_summary(rank_ic_series(sub, fwd[mask]))[0]
    head = fwd_by_h[horizon][mask]
    ic_mean, icir = ic_summary(ic_series(sub, head))
    rank_mean, rank_icir = ic_summary(rank_ic_series(sub, head))
    return SplitMetrics(n_dates, ic_mean, icir, rank_mean, rank_icir, by_h)


def _build_composite(
    method: str, aligned: np.ndarray, head_fwd: np.ndarray, train_mask: np.ndarray,
    abs_ic: np.ndarray, icir: np.ndarray, ic_rows: np.ndarray, ridge_lambda: float,
    model_params: dict | None,
) -> tuple[np.ndarray, np.ndarray, str]:
    """Build the ``(T, N)`` composite for ``method`` -> ``(combined, per_factor, kind)``.

    Analytic schemes return a linear ``weight`` blend; learned models return the
    model's ``importance``-weighted *prediction*. A learned model that cannot fit
    (train design too small) degrades to an equal-weight blend.
    """
    k = aligned.shape[0]
    if method in ANALYTIC_METHODS:
        w = _fit_weights(method, aligned, head_fwd, train_mask, abs_ic, icir, ic_rows, ridge_lambda)
        return _combine_stack(aligned, w), w, "weight"
    if method in MODEL_METHODS:
        combined, imp = _fit_predict_model(method, aligned, head_fwd, train_mask, model_params)
        if combined is None:
            w = np.ones(k) / k
            return _combine_stack(aligned, w), w, "weight"
        return combined, imp, "importance"
    raise ValueError(
        f"combination method {method!r} invalid; expected one of {COMBINATION_METHODS}"
    )


# ---------------------------------------------------------------------------
# 5. orchestrator
# ---------------------------------------------------------------------------
def combine_factors(
    factors: dict[str, np.ndarray],
    fwd_by_h: dict[int, np.ndarray],
    train_mask: np.ndarray,
    val_mask: np.ndarray,
    test_mask: np.ndarray,
    *,
    method: str = "icir_weight",
    standardize: str = "zscore",
    horizon: int | None = None,
    ridge_lambda: float = 10.0,
    candidate_methods: Sequence[str] | None = None,
    model_params: dict | None = None,
) -> CombinationResult:
    """Fit a factor combination on train, (optionally) select on val, score on test.

    Parameters
    ----------
    factors:
        ``{name: (T, N) factor matrix}`` — at least one. All must share the panel's
        ``(T, N)`` shape (the caller evaluates them on one engine).
    fwd_by_h:
        ``{horizon: (T, N) forward returns}`` (e.g. from
        :func:`assay.evaluator.forward_returns`). Must contain ``horizon``.
    train_mask, val_mask, test_mask:
        Boolean ``(T,)`` date masks (see :func:`make_splits`). The weights are fit
        on ``train`` only; ``val`` selects the scheme when ``method='auto'``;
        ``test`` is reported untouched by fitting.
    method:
        A name in :data:`COMBINATION_METHODS`, or ``'auto'`` to fit every
        ``candidate_methods`` on train and keep the best **validation** ICIR.
    standardize:
        ``'zscore'`` | ``'rank'`` cross-sectional standardisation before blending.
    horizon:
        Headline forward-return horizon for IC and the regression label. Defaults to
        the smallest key of ``fwd_by_h``.
    ridge_lambda:
        L2 strength for ``ridge`` / the ``max_icir`` covariance ridge.
    candidate_methods:
        Override the ``'auto'`` candidate set (defaults to a robust subset).

    Returns
    -------
    CombinationResult
        Fitted weights + orientation, per-factor train IC, the ``(T, N)`` composite,
        and train/val/test scorecards. Never raises on a degenerate factor: an
        all-NaN constituent contributes nothing and a degenerate fit falls back to
        equal weight.
    """
    if not factors:
        raise ValueError("combine_factors needs at least one factor")
    if not fwd_by_h:
        raise ValueError("combine_factors needs at least one forward-return horizon")
    names = list(factors.keys())
    horizon = int(min(fwd_by_h)) if horizon is None else int(horizon)
    if horizon not in fwd_by_h:
        raise ValueError(f"horizon {horizon} not in fwd_by_h horizons {sorted(fwd_by_h)}")
    head_fwd = fwd_by_h[horizon]
    shape = head_fwd.shape

    # Standardise + orient each factor by the sign of its train IC.
    std_list, abs_ic, icir, ic_series_rows, per_factor_ic, orient = [], [], [], [], {}, {}
    for name in names:
        mat = np.asarray(factors[name], dtype=np.float64)
        if mat.shape != shape:
            raise ValueError(
                f"factor {name!r} shape {mat.shape} != forward-return shape {shape}"
            )
        std = standardize_xs(mat, standardize)
        mean_ic, icir_k, tr_series = _factor_ic_train(std, head_fwd, train_mask)
        sign = 1.0 if (not np.isfinite(mean_ic) or mean_ic >= 0) else -1.0
        std_list.append(std * sign)
        # Metrics on the *oriented* factor (so weights see non-negative IC/ICIR).
        abs_ic.append(abs(mean_ic) if np.isfinite(mean_ic) else 0.0)
        icir.append(abs(icir_k) if np.isfinite(icir_k) else 0.0)
        ic_series_rows.append(tr_series * sign)
        per_factor_ic[name] = mean_ic
        orient[name] = sign

    aligned = np.stack(std_list, axis=0)             # (K, T, N)
    abs_ic = np.asarray(abs_ic)
    icir = np.asarray(icir)
    # Pad ragged train-IC series to a common width for the max_icir covariance.
    width = max((s.size for s in ic_series_rows), default=0)
    ic_rows = np.full((len(names), width), np.nan)
    for r, s in enumerate(ic_series_rows):
        ic_rows[r, : s.size] = s

    def _build(m: str):
        return _build_composite(
            m, aligned, head_fwd, train_mask, abs_ic, icir, ic_rows, ridge_lambda, model_params
        )

    # Method selection (validation-driven) or a single fixed scheme.
    selection: dict | None = None
    if method == "auto":
        cands = list(candidate_methods or _AUTO_CANDIDATES)
        selection = {}
        best, best_m, best_score = None, None, -np.inf
        for m in cands:
            try:
                comb, w, kind = _build(m)
            except Exception:  # noqa: BLE001 — a missing model lib drops that candidate
                selection[m] = None
                continue
            val_icir = _split_metrics(comb, fwd_by_h, val_mask, horizon).icir
            selection[m] = val_icir
            score = val_icir if np.isfinite(val_icir) else -np.inf
            if score > best_score:
                best, best_m, best_score = (comb, w, kind), m, score
        if best is None:  # every candidate failed -> equal-weight fallback
            best, best_m = _build("equal"), "equal"
        (combined, weights, weight_kind), chosen = best, best_m
    else:
        chosen = method
        combined, weights, weight_kind = _build(method)

    diagnostics = {
        "n_factors": len(names),
        "n_dates_total": int(shape[0]),
        "horizons": sorted(int(h) for h in fwd_by_h),
        "composite_turnover_1d": _clean(_turnover(combined)),
        "embargoed": int((~train_mask).sum() + (~val_mask).sum()),  # advisory
    }
    return CombinationResult(
        method=chosen,
        standardize=standardize,
        horizon=horizon,
        factor_names=names,
        weights={n: float(w) for n, w in zip(names, weights)},
        weight_kind=weight_kind,
        orientation={n: float(orient[n]) for n in names},
        per_factor_train_ic={n: float(per_factor_ic[n]) for n in names},
        train=_split_metrics(combined, fwd_by_h, train_mask, horizon),
        val=_split_metrics(combined, fwd_by_h, val_mask, horizon),
        test=_split_metrics(combined, fwd_by_h, test_mask, horizon),
        combined=combined,
        selection=selection,
        diagnostics=diagnostics,
    )
