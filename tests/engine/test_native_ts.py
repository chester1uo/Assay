"""The Rust rolling kernels (``ts_rolling`` / ``ts_rolling2``) against the numpy ones.

Skipped where the extension is not built. Order statistics (min/max/arg*/rank/count)
must match exactly; the running-sum family to ~1e-12 relative (shifted sums rebuilt
every few steps), including on price-level data where a naive Σx² − (Σx)²/n would
lose most digits."""

from __future__ import annotations

import numpy as np
import pytest

native = pytest.importorskip("assay_kernels")

from assay import _native  # noqa: E402
from assay.engine.operators import time_series as ts  # noqa: E402

pytestmark = pytest.mark.skipif(_native.kernels is None, reason="native kernels switched off (ASSAY_KERNELS)")

ONE = ["ts_mean", "ts_sum", "ts_std", "ts_var", "ts_decay_linear", "ts_slope", "ts_resi", "ts_rsquare",
       "ts_skew", "ts_kurt", "ts_mad"]
EXACT = ["ts_min", "ts_max", "ts_argmin", "ts_argmax", "ts_rank", "ts_count"]
TWO = ["ts_corr", "ts_cov"]


def _numpy(monkeypatch, name, *args):
    with monkeypatch.context() as m:
        m.setattr(_native, "kernels", None)
        return getattr(ts, name)(*args)


def _data(seed, t=300, n=40, level=0.0, ties=False):
    rng = np.random.default_rng(seed)
    x = level + np.cumsum(rng.normal(size=(t, n)), axis=0) * (0.01 * level or 1.0)
    if ties:
        x = np.round(rng.normal(size=(t, n)), 1)
        x[rng.random(x.shape) < 0.2] = 0.0
    x[rng.random(x.shape) < 0.03] = np.nan
    x[:20, 3] = np.nan                                  # late listing
    x[100:110, 5] = np.nan                              # gap
    x[50, 7] = np.inf
    x[60, 8] = -np.inf
    return x


def _close(a, b, rtol):
    np.testing.assert_array_equal(np.isnan(a), np.isnan(b))
    ok = np.isfinite(b)
    np.testing.assert_array_equal(a[~ok & ~np.isnan(b)], b[~ok & ~np.isnan(b)])   # same ±inf
    scale = np.maximum(np.abs(b[ok]), 1.0)
    assert np.max(np.abs(a[ok] - b[ok]) / scale, initial=0.0) < rtol


@pytest.mark.parametrize("name", ONE)
@pytest.mark.parametrize("d", [2, 5, 20, 63])
def test_sum_family(monkeypatch, name, d):
    if d == 2 and name in ("ts_skew", "ts_kurt"):
        pytest.skip("two-point skew/kurt are rounding noise in numpy")
    x = _data(d)
    _close(getattr(ts, name)(x, d), _numpy(monkeypatch, name, x, d), 1e-10)


@pytest.mark.parametrize("name", ["ts_mean", "ts_std", "ts_var", "ts_slope", "ts_rsquare", "ts_resi"])
def test_price_level_precision(monkeypatch, name):
    x = _data(7, t=1500, level=2500.0)                  # ~1e-2 relative moves on a 2500 level
    _close(getattr(ts, name)(x, 40), _numpy(monkeypatch, name, x, 40), 1e-9)


@pytest.mark.parametrize("name", EXACT)
@pytest.mark.parametrize("d", [2, 7, 30])
def test_order_statistics_exact(monkeypatch, name, d):
    x = _data(d + 1, ties=True)
    np.testing.assert_array_equal(getattr(ts, name)(x, d), _numpy(monkeypatch, name, x, d))


@pytest.mark.parametrize("name", TWO)
@pytest.mark.parametrize("d", [3, 10, 60])
def test_pair(monkeypatch, name, d):
    x, y = _data(d), _data(d + 100)
    _close(getattr(ts, name)(x, y, d), _numpy(monkeypatch, name, x, y, d), 1e-10)


def test_constant_windows():
    x = np.ones((40, 3)) * 0.1
    x[:, 1] = np.arange(40.0)
    y = np.sin(np.arange(40.0))[:, None] * np.ones((1, 3))
    assert np.all(ts.ts_std(x, 10)[9:, 0] == 0.0)
    assert np.all(np.isnan(ts.ts_corr(x, y, 10)[:, 0]))
    assert np.all(np.isnan(ts.ts_rsquare(x, 10)[:, 0]))
    np.testing.assert_allclose(ts.ts_rsquare(x, 10)[9:, 1], 1.0)
    np.testing.assert_allclose(ts.ts_slope(x, 10)[9:, 1], 1.0)
    assert np.all(np.abs(ts.ts_corr(x, y, 10)[9:, 1]) <= 1.0)


def test_edges():
    x = _data(3, t=120)
    assert np.isnan(ts.ts_mean(x, 121)).all()            # window longer than the history
    np.testing.assert_array_equal(ts.ts_sum(x, 1), x)
    assert ts.ts_mean(np.empty((0, 4)), 5).shape == (0, 4)
    with pytest.raises(ValueError):
        native.ts_rolling("nope", x, 3)
    with pytest.raises(ValueError):
        native.ts_rolling2("corr", x, x[:5], 3)
    np.testing.assert_array_equal(ts.ts_mean(np.asfortranarray(x), 3), ts.ts_mean(x, 3))
