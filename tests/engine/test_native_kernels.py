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
    x[7, ::3] = -0.0                                    # -0.0 ties with 0.0
    x[8, :2] = -np.inf
    return x


# 600 columns sort by radix, 60 by comparison
WIDE = pytest.mark.parametrize("n", [60, 600])


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


@WIDE
def test_cs_rank(python_side, n):
    x = _panel(1, n=n)
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


@WIDE
def test_row_orders_stable(n):
    x = _panel(10, n=n)
    order, count = native.row_orders(x)
    for r, row in enumerate(x):
        ok = np.flatnonzero(np.isfinite(row))
        want = ok[np.argsort(row[ok], kind="stable")]
        assert count[r] == ok.size and order[r, :ok.size].tolist() == want.tolist()


@WIDE
def test_group_sums_from_orders(n):
    x, y = _panel(11, n=n), _panel(12, n=n)
    a = native.group_sums(x, y, 5)
    b = native.group_sums_ordered(x, *native.row_orders(x), y, 5)
    np.testing.assert_array_equal(a[0], b[0])
    np.testing.assert_array_equal(a[1], b[1])


@WIDE
def test_rank_ic_from_orders(python_side, n):
    x, y = _panel(6, n=n), _panel(7, n=n)
    xo, yo = native.row_orders(x), native.row_orders(y)
    assert xo[1].tolist() == np.isfinite(x).sum(axis=1).tolist()
    # exact rank sums: bit for bit the sort-per-pair kernel
    np.testing.assert_array_equal(native.rank_ic_ordered(x, *xo, y, *yo), native.rank_ic_series(x, y))
    np.testing.assert_array_equal(native.rank_ic_ordered(x, *xo, x, *xo, 2), native.rank_ic_series(x, x, 2))
    m = python_side["metrics"]
    np.testing.assert_allclose(native.rank_ic_ordered(x, *xo, y, *yo), m.rank_ic_series(x, y), rtol=0, atol=1e-12)
    with pytest.raises(ValueError):
        native.rank_ic_ordered(x, *xo, y, xo[0][:5], yo[1])


@WIDE
def test_rank_cache(n):
    x, y, z = _panel(13, n=n), _panel(14, n=n), _panel(15, n=n)
    z[:, :5] = np.nan                                   # other names on every date
    xo, yo, zo = native.row_orders(x), native.row_orders(y), native.row_orders(z)
    yc, xc = native.RankCache(*y.shape), native.RankCache(*x.shape)
    want = native.rank_ic_series(x, y)
    first = native.rank_ic_ordered(x, *xo, y, *yo, xcache=xc, ycache=yc)
    again = native.rank_ic_ordered(x, *xo, y, *yo, xcache=xc, ycache=yc)
    np.testing.assert_array_equal(first, want)
    np.testing.assert_array_equal(again, want)
    hits, misses = yc.stats()
    assert hits > 0 and hits + misses == 2 * len(x)
    # the same y ranked against other names misses and still matches
    np.testing.assert_array_equal(native.rank_ic_ordered(z, *zo, y, *yo, ycache=yc), native.rank_ic_series(z, y))
    np.testing.assert_array_equal(native.rank_ic_ordered(x, *xo, y, *yo, ycache=yc), want)
    with pytest.raises(ValueError):
        native.rank_ic_ordered(x, *xo, y, *yo, ycache=native.RankCache(3, n))
    # one cache on both sides: the second borrow is skipped, not an error
    np.testing.assert_array_equal(native.rank_ic_ordered(y, *yo, y, *yo, xcache=yc, ycache=yc),
                                  native.rank_ic_series(y, y))


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


@WIDE
def test_cs_rank_comes_with_its_orders(n):
    x = _panel(16, n=n)
    ranks, order, count = native.cs_rank_ordered(x)
    np.testing.assert_array_equal(ranks, native.cs_rank(x))
    want = native.row_orders(ranks)
    np.testing.assert_array_equal(count, want[1])
    for r in range(len(x)):
        assert order[r, :count[r]].tolist() == want[0][r, :want[1][r]].tolist()
    keep = np.random.default_rng(1).random(x.shape) < 0.7
    got, want = native.orders_where(order, count, keep), native.row_orders(np.where(keep, ranks, np.nan))
    np.testing.assert_array_equal(got[1], want[1])
    for r in range(len(x)):
        assert got[0][r, :got[1][r]].tolist() == want[0][r, :want[1][r]].tolist()


@WIDE
def test_lag_reads_the_factor_cache(n):
    rng = np.random.default_rng(17)
    x, y = np.round(rng.normal(size=(80, n)), 1), _panel(18, n=n)
    x[:, n // 2:] = np.nan                              # names y has and x lacks
    x[40:, 3] = np.nan                                  # one name leaves: that date misses
    y[:, : n // 2] = np.nan_to_num(y[:, : n // 2])      # y has every name x has
    xo, yo = native.row_orders(x), native.row_orders(y)
    c = native.RankCache(*x.shape)
    native.rank_ic_ordered(x, *xo, y, *yo, xcache=c)    # scoring fills the factor's ranks
    before = c.stats()
    got = native.rank_ic_ordered(x, *xo, x, *xo, 1, xcache=c, ycache=c)
    np.testing.assert_array_equal(got, native.rank_ic_series(x, x, 1))
    assert c.stats()[0] > before[0]                    # rows reused, none written


def test_factor_ending_in_cs_rank_is_not_sorted_again():
    from assay import _native
    from assay.engine.engine import FactorEngine
    from assay.engine import parse
    from assay.evaluator import metrics

    if _native.kernels is None:
        pytest.skip("native kernels switched off (ASSAY_KERNELS)")
    x = _panel(19, n=300)
    eng = FactorEngine.from_matrices(np.arange(len(x)).astype("datetime64[D]"), np.array([f"S{i:03d}" for i in range(300)]),
                                     {"close": np.nan_to_num(x, nan=1.0), "volume": x})
    eng.membership = np.random.default_rng(2).random(x.shape) < 0.8
    v = eng.evaluate(parse("cs_rank(ts_delta(volume, 2))")).values
    known = _native.known_orders(v)
    assert known is not None and metrics.row_orders(v) is known
    fresh = native.row_orders(np.ascontiguousarray(v))
    np.testing.assert_array_equal(known[1], fresh[1])
    for r in range(len(v)):
        assert known[0][r, :known[1][r]].tolist() == fresh[0][r, :fresh[1][r]].tolist()
