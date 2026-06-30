"""Tests for the multi-factor combination layer (design-doc §6.3).

Offline only — pure-numpy synthetic factors with hand-controlled IC, plus one
service-level integration test using the monkeypatched ``FactorEngine.from_store``
(the same offline pattern the portfolio tests use). Run with::

    PYTHONPATH=src python -m pytest tests/evaluator/test_combination.py -q

Covers: cross-sectional standardisation, purged train/val/test splits, every
weighting scheme, factor orientation (auto sign-flip), validation-driven method
selection, NaN-awareness, and the ``AssayService.combine_factors`` wiring.
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import polars as pl
import pytest

from assay.evaluator import combination as cmb
from assay.evaluator import forward_returns


# ---------------------------------------------------------------------------
# synthetic panel: a forward-return matrix + factors with controlled IC
# ---------------------------------------------------------------------------
def _make_world(T=240, N=30, seed=0):
    """Forward returns plus three factors: strong, weak, and sign-flipped."""
    rng = np.random.default_rng(seed)
    fwd = rng.normal(0.0, 0.02, (T, N))
    noise = lambda s: rng.normal(0.0, 1.0, (T, N))  # noqa: E731
    strong = fwd + 0.5 * noise(1)        # high IC with fwd
    weak = fwd + 4.0 * noise(2)          # low IC
    flipped = -fwd + 1.0 * noise(3)      # negatively predictive (orientation must flip it)
    fwd_by_h = {1: fwd}
    return fwd_by_h, {"strong": strong, "weak": weak, "flipped": flipped}, T


def _splits(T):
    # index-based windows mapped onto integer "dates" 0..T-1 (lexical strings)
    dates = [f"2021-{1 + i // 21:02d}-{1 + i % 21:02d}" for i in range(T)]
    return dates


# ---------------------------------------------------------------------------
# standardisation
# ---------------------------------------------------------------------------
def test_standardize_zscore_is_unit_per_row_and_nan_aware():
    x = np.array([[1.0, 2.0, 3.0, np.nan], [10.0, 10.0, 10.0, 10.0]])
    z = cmb.standardize_xs(x, "zscore")
    # row 0: finite entries demeaned/std (ddof=0), NaN preserved
    fin = z[0][np.isfinite(z[0])]
    assert np.isclose(fin.mean(), 0.0, atol=1e-9)
    assert np.isnan(z[0, 3])
    # row 1: constant -> all zeros (no signal), not NaN/inf
    assert np.allclose(z[1], 0.0)


def test_standardize_rank_maps_to_pm1():
    x = np.array([[10.0, 20.0, 30.0, 40.0, 50.0]])
    r = cmb.standardize_xs(x, "rank")
    assert np.isclose(r.min(), -1.0) and np.isclose(r.max(), 1.0)
    assert np.isclose(r[0, 2], 0.0)  # median -> 0


# ---------------------------------------------------------------------------
# splits + embargo
# ---------------------------------------------------------------------------
def test_make_splits_partition_and_embargo():
    dates = [f"2021-{m:02d}-01" for m in range(1, 13)]  # 12 monthly dates
    tr, va, te = cmb.make_splits(
        dates, ("2021-01-01", "2021-06-01"), ("2021-07-01", "2021-09-01"),
        ("2021-10-01", "2021-12-01"), embargo=0,
    )
    assert tr.sum() == 6 and va.sum() == 3 and te.sum() == 3
    assert not (tr & va).any() and not (va & te).any()  # disjoint
    # embargo drops the last train/val dates (label-leakage purge)
    tr2, va2, te2 = cmb.make_splits(
        dates, ("2021-01-01", "2021-06-01"), ("2021-07-01", "2021-09-01"),
        ("2021-10-01", "2021-12-01"), embargo=1,
    )
    assert tr2.sum() == 5 and va2.sum() == 2 and te2.sum() == 3  # test untouched


# ---------------------------------------------------------------------------
# combination — schemes, orientation, selection
# ---------------------------------------------------------------------------
def test_combine_orientation_flips_negative_factor():
    fwd_by_h, factors, T = _make_world()
    tr = np.zeros(T, bool); tr[: T // 2] = True
    va = np.zeros(T, bool); va[T // 2 : 3 * T // 4] = True
    te = np.zeros(T, bool); te[3 * T // 4 :] = True
    res = cmb.combine_factors(factors, fwd_by_h, tr, va, te, method="equal")
    # the negatively-predictive factor is oriented -1; the others +1
    assert res.orientation["flipped"] == -1.0
    assert res.orientation["strong"] == 1.0
    # equal weights, L1-normalised
    assert pytest.approx(sum(abs(w) for w in res.weights.values()), abs=1e-9) == 1.0


def test_combine_test_ic_positive_and_weights_normalised():
    fwd_by_h, factors, T = _make_world()
    tr = np.zeros(T, bool); tr[: T // 2] = True
    va = np.zeros(T, bool); va[T // 2 : 3 * T // 4] = True
    te = np.zeros(T, bool); te[3 * T // 4 :] = True
    for method in ("equal", "ic_weight", "icir_weight", "ols", "ridge", "max_icir"):
        res = cmb.combine_factors(factors, fwd_by_h, tr, va, te, method=method)
        assert res.method == method
        assert pytest.approx(sum(abs(w) for w in res.weights.values()), abs=1e-9) == 1.0
        # composite is predictive out-of-sample (signal dominates noise here)
        assert res.test.ic > 0.0, method
        # icir_weight should lean on the strong factor more than the weak one
    res = cmb.combine_factors(factors, fwd_by_h, tr, va, te, method="icir_weight")
    assert abs(res.weights["strong"]) > abs(res.weights["weak"])


def test_combine_auto_selects_by_validation_and_records_scores():
    fwd_by_h, factors, T = _make_world()
    tr = np.zeros(T, bool); tr[: T // 2] = True
    va = np.zeros(T, bool); va[T // 2 : 3 * T // 4] = True
    te = np.zeros(T, bool); te[3 * T // 4 :] = True
    res = cmb.combine_factors(factors, fwd_by_h, tr, va, te, method="auto")
    assert res.selection is not None
    assert set(res.selection).issubset(set(cmb.COMBINATION_METHODS))
    # the chosen method has the best (finite) validation ICIR among candidates
    finite = {m: v for m, v in res.selection.items() if v is not None}
    assert res.method == max(finite, key=finite.get)


def test_available_methods_lists_analytic_and_models():
    av = cmb.available_methods()
    names = {m["name"]: m for m in av}
    # analytic schemes always available, including the optimization-based ones
    for m in ("equal", "ridge", "nnls", "max_icir"):
        assert names[m]["available"] is True and names[m]["kind"] == "analytic"
    # learned models present with an availability flag (sklearn is installed here)
    assert names["random_forest"]["kind"] == "tree"
    assert names["lightgbm"]["kind"] == "boost"
    assert isinstance(names["xgboost"]["available"], bool)


def test_nnls_weights_are_nonnegative():
    fwd_by_h, factors, T = _make_world()
    tr = np.zeros(T, bool); tr[: T // 2] = True
    va = np.zeros(T, bool); va[T // 2 : 3 * T // 4] = True
    te = np.zeros(T, bool); te[3 * T // 4 :] = True
    res = cmb.combine_factors(factors, fwd_by_h, tr, va, te, method="nnls")
    assert res.weight_kind == "weight"
    assert all(v >= -1e-12 for v in res.weights.values())  # non-negative (long-only)


def test_model_method_predicts_with_importances():
    pytest.importorskip("sklearn")
    fwd_by_h, factors, T = _make_world()
    tr = np.zeros(T, bool); tr[: T // 2] = True
    va = np.zeros(T, bool); va[T // 2 : 3 * T // 4] = True
    te = np.zeros(T, bool); te[3 * T // 4 :] = True
    res = cmb.combine_factors(factors, fwd_by_h, tr, va, te, method="random_forest")
    assert res.method == "random_forest" and res.weight_kind == "importance"
    # importances are non-negative and L1-normalised
    assert pytest.approx(sum(abs(v) for v in res.weights.values()), abs=1e-9) == 1.0
    assert np.isfinite(res.test.ic)
    import json
    json.dumps(res.to_dict())


def test_combine_unknown_method_raises():
    fwd_by_h, factors, T = _make_world()
    tr = np.ones(T, bool); va = np.zeros(T, bool); te = np.zeros(T, bool)
    with pytest.raises(ValueError):
        cmb.combine_factors(factors, fwd_by_h, tr, va, te, method="not_a_method")


def test_combine_single_factor_and_all_nan_constituent():
    fwd_by_h, factors, T = _make_world()
    tr = np.zeros(T, bool); tr[: T // 2] = True
    va = np.zeros(T, bool); va[T // 2 : 3 * T // 4] = True
    te = np.zeros(T, bool); te[3 * T // 4 :] = True
    # single factor -> weight 1.0
    one = cmb.combine_factors({"a": factors["strong"]}, fwd_by_h, tr, va, te, method="ridge")
    assert pytest.approx(one.weights["a"], abs=1e-9) == 1.0
    # an all-NaN factor contributes nothing but does not crash
    factors["dead"] = np.full_like(factors["strong"], np.nan)
    res = cmb.combine_factors(factors, fwd_by_h, tr, va, te, method="icir_weight")
    assert np.isfinite(res.test.ic)
    d = res.to_dict()
    import json
    json.dumps(d)  # JSON-safe (NaN -> None)


# ---------------------------------------------------------------------------
# service integration (offline monkeypatched engine)
# ---------------------------------------------------------------------------
def _ohlcv_panel(t=160, n=20, seed=7):
    rng = np.random.default_rng(seed)
    close = 100.0 + rng.normal(0, 1, (t, n)).cumsum(axis=0)
    open_ = close + rng.normal(0, 0.2, (t, n))
    dates = [dt.date(2021, 1, 1) + dt.timedelta(days=i) for i in range(t)]
    syms = [f"S{j}" for j in range(n)]
    return pl.DataFrame({
        "date": np.repeat(np.array(dates), n), "symbol": syms * t,
        "open": open_.reshape(-1), "high": (np.maximum(open_, close) + 1).reshape(-1),
        "low": (np.minimum(open_, close) - 1).reshape(-1), "close": close.reshape(-1),
        "volume": np.full((t, n), 1e6).reshape(-1),
    })


def test_service_combine_factors_end_to_end(monkeypatch):
    from assay.engine import FactorEngine
    from assay.service import AssayService
    from assay.config import AssayConfig

    panel = _ohlcv_panel()
    monkeypatch.setattr(
        FactorEngine, "from_store",
        staticmethod(lambda *a, **k: FactorEngine(panel)),
    )
    svc = AssayService(AssayConfig())

    out = svc.combine_factors(
        ["ts_mean(close, 5)", "rank(close)", {"name": "mom", "expr": "delta(close, 10)"}],
        train=("2021-01-01", "2021-03-01"),
        val=("2021-03-02", "2021-04-15"),
        test=("2021-04-16", "2021-12-31"),
        universe="NASDAQ100", horizons=[1, 5], method="auto",
    )
    assert "failure" not in out, out
    assert out["method"] in cmb.COMBINATION_METHODS
    assert len(out["factor_names"]) == 3
    assert set(out["weights"]) == set(out["factor_names"])
    # train/val/test scorecards present and the period spans the full envelope
    for split in ("train", "val", "test"):
        assert "ic" in out[split] and out[split]["n_dates"] > 0
    assert out["period"] == ["2021-01-01", "2021-12-31"]
    assert out["splits"]["embargo"] == 5  # default = max horizon
    import json
    json.dumps(out)


def test_service_combine_factors_resolves_pool_and_reports_dropped(monkeypatch):
    from assay.engine import FactorEngine
    from assay.service import AssayService
    from assay.config import AssayConfig

    panel = _ohlcv_panel()
    monkeypatch.setattr(
        FactorEngine, "from_store",
        staticmethod(lambda *a, **k: FactorEngine(panel)),
    )
    svc = AssayService(AssayConfig())
    # one valid expr + one Alpha101 needing vwap/cap (drops) + a bogus lib id (skipped)
    out = svc.combine_factors(
        ["rank(close)", "alpha101:25", "lib:does_not_exist"],
        train=("2021-01-01", "2021-03-01"),
        val=("2021-03-02", "2021-04-15"),
        test=("2021-04-16", "2021-06-30"),
        universe="NASDAQ100", horizons=[1], method="equal",
    )
    # bogus lib id never resolves; alpha101:25 resolves but fails to evaluate (vwap)
    names = [f["name"] for f in out["resolved_factors"]]
    assert "rank(close)" in names and "alpha101_25" in names
    assert all("does_not_exist" not in n for n in names)
    assert any(d["name"] == "alpha101_25" for d in out["dropped"])
    assert out["factor_names"] == ["rank(close)"]  # only the clean one survives
