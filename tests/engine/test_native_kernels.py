"""The Rust kernels (``assay_kernels``) against the Python/numba references.

Skipped where the extension is not built. Ranks and bucket sums must match bit for
bit; correlations to 1e-12 (same summation order, different compiler)."""

from __future__ import annotations

import importlib

import numpy as np
import pytest

native = pytest.importorskip("assay_kernels")


def _panel(seed, t=80, n=60):
    rng = np.random.default_rng(seed)
    x = np.round(rng.normal(size=(t, n)), 1)           # many ties
    x[rng.random(x.shape) < 0.12] = np.nan
    x[3] = np.nan
    x[4, 1:] = np.nan
    x[5] = 1.5
    x[6, 0] = np.inf
    return x


@pytest.fixture
def python_side(monkeypatch):
    """The modules reloaded with the native kernels switched off."""
    import assay._native as nat

    # the package re-exports functions named like its modules, so import modules by path
    metrics, turnover, groups, cross_sectional = (importlib.import_module(f"assay.{m}") for m in (
        "evaluator.metrics", "evaluator.turnover", "evaluator.groups", "engine.operators.cross_sectional"))
    monkeypatch.setattr(nat, "kernels", None)
    mods = [importlib.reload(m) for m in (metrics, turnover, groups, cross_sectional)]
    yield dict(zip(("metrics", "turnover", "groups", "cs"), mods))
    monkeypatch.undo()
    for m in (metrics, turnover, groups, cross_sectional):
        importlib.reload(m)


def test_cs_rank(python_side):
    x = _panel(1)
    np.testing.assert_array_equal(native.cs_rank(x), python_side["cs"].cs_rank(x))


def test_rank_ic_ic_and_autocorr(python_side):
    x, y = _panel(2), _panel(3)
    m = python_side["metrics"]
    np.testing.assert_allclose(native.rank_ic_series(x, y), m.rank_ic_series(x, y), rtol=0, atol=1e-12)
    np.testing.assert_allclose(native.ic_series(x, y), m.ic_series(x, y), rtol=0, atol=1e-12)
    np.testing.assert_allclose(native.rank_ic_series(x, x, 1), python_side["turnover"].rank_autocorr(x, 1),
                               rtol=0, atol=1e-12)


def test_group_sums(python_side):
    x, y = _panel(4), _panel(5)
    ref_sums = python_side["groups"].group_returns(x, y)
    import assay._native as nat

    sums, counts = native.group_sums(x, y, 5)
    assert nat.kernels is None and counts.sum() > 0
    # same bucket means as the Python loop, bit for bit
    with np.errstate(invalid="ignore"):
        per = np.where(counts > 0, sums / counts, np.nan)
    q = [float(per[:, g][np.isfinite(per[:, g])].mean()) for g in range(5)]
    assert q == [ref_sums["quantile_returns"][f"Q{g + 1}"] for g in range(5)]


def test_rank_ic_from_orders(python_side):
    x, y = _panel(6), _panel(7)
    xo, yo = native.row_orders(x), native.row_orders(y)
    assert xo[1].tolist() == np.isfinite(x).sum(axis=1).tolist()
    # same ranks, same Pearson sums: bit for bit the sort-per-pair kernel
    np.testing.assert_array_equal(native.rank_ic_ordered(x, *xo, y, *yo), native.rank_ic_series(x, y))
    np.testing.assert_array_equal(native.rank_ic_ordered(x, *xo, x, *xo, 2), native.rank_ic_series(x, x, 2))
    m = python_side["metrics"]
    np.testing.assert_allclose(native.rank_ic_ordered(x, *xo, y, *yo), m.rank_ic_series(x, y), rtol=0, atol=1e-12)
    with pytest.raises(ValueError):
        native.rank_ic_ordered(x, *xo, y, xo[0][:5], yo[1])


def test_evaluate_ic_reuses_orders():
    from assay import _native, evaluator
    from assay.evaluator import metrics

    if _native.kernels is None:
        pytest.skip("native kernels switched off (ASSAY_KERNELS)")

    x, y = _panel(8), _panel(9)
    fwd = {1: y, 5: y[::-1].copy()}
    a = evaluator.evaluate_ic(x, fwd)
    b = evaluator.evaluate_ic(x, fwd, order=metrics.row_orders(x))
    assert a["ic_by_horizon"] == b["ic_by_horizon"] and id(y) in metrics._FWD_ORDERS
    assert evaluator.turnover(x) == evaluator.turnover(x, order=metrics.row_orders(x))
    key = id(fwd[5])
    del fwd, a, b, y
    assert key not in metrics._FWD_ORDERS              # dropped with its array
