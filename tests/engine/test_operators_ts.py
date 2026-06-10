"""Exhaustive time-series operator tests.

Each ``ts_*`` kernel is checked against an independent reference (hand-computed
small arrays or numpy on the explicit window slice — never the kernel's own
sliding-window code), plus NaN warm-up semantics, per-symbol independence, and
documented error cases. Run with::

    PYTHONPATH=src python -m pytest tests/engine/test_operators_ts.py -q
"""

from __future__ import annotations

import numpy as np
import pytest

from assay.engine import operators as op


def col(values) -> np.ndarray:
    """A single-symbol (T, 1) column."""
    return np.asarray(values, dtype=float).reshape(-1, 1)


def mat(*columns) -> np.ndarray:
    """A (T, N) matrix from per-symbol columns."""
    return np.column_stack([np.asarray(c, dtype=float) for c in columns])


# ---------------------------------------------------------------------------
# ts_delay / ts_delta / ts_returns / ts_log_returns
# ---------------------------------------------------------------------------


def test_ts_delay_shifts_down_and_warms_up():
    x = col([10, 20, 30, 40])
    out = op.ts_delay(x, 1)[:, 0]
    assert np.isnan(out[0])
    np.testing.assert_array_equal(out[1:], [10, 20, 30])


def test_ts_delay_multi_step_and_identity():
    x = col([1, 2, 3, 4, 5])
    np.testing.assert_array_equal(op.ts_delay(x, 2)[2:, 0], [1, 2, 3])
    np.testing.assert_array_equal(op.ts_delay(x, 0)[:, 0], [1, 2, 3, 4, 5])  # d=0 is identity


def test_ts_delay_negative_is_rejected():
    with pytest.raises(ValueError, match="look ahead"):
        op.ts_delay(col([1, 2, 3]), -1)


def test_ts_delta_is_x_minus_delay():
    x = col([1, 3, 6, 10])
    out = op.ts_delta(x, 1)[:, 0]
    assert np.isnan(out[0])
    np.testing.assert_array_equal(out[1:], [2, 3, 4])


def test_ts_returns_and_zero_denominator():
    x = col([100, 110, 121])
    np.testing.assert_allclose(op.ts_returns(x, 1)[1:, 0], [0.10, 0.10], rtol=1e-12)
    # a zero prior price yields NaN (not inf)
    z = col([0.0, 5.0])
    assert np.isnan(op.ts_returns(z, 1)[1, 0])


def test_ts_log_returns_and_nonpositive_ratio():
    x = col([100, 110])
    np.testing.assert_allclose(op.ts_log_returns(x, 1)[1, 0], np.log(1.1), rtol=1e-12)
    neg = col([-1.0, 5.0])  # ratio 5/-1 < 0 -> NaN
    assert np.isnan(op.ts_log_returns(neg, 1)[1, 0])


# ---------------------------------------------------------------------------
# ts_mean / ts_sum / ts_product
# ---------------------------------------------------------------------------


def test_ts_mean_sum_product_exact():
    x = col([1, 2, 3, 4, 5])
    m = op.ts_mean(x, 3)[:, 0]
    assert np.isnan(m[0]) and np.isnan(m[1])
    np.testing.assert_allclose(m[2:], [2, 3, 4])
    np.testing.assert_allclose(op.ts_sum(x, 3)[2:, 0], [6, 9, 12])
    np.testing.assert_allclose(op.ts_product(col([1, 2, 3, 4]), 2)[1:, 0], [2, 6, 12])


def test_ts_window_propagates_interior_nan():
    x = col([1, 2, np.nan, 4, 5])
    m = op.ts_mean(x, 2)[:, 0]
    # windows touching the NaN are NaN; the window [4,5] is clean
    assert np.isnan(m[2]) and np.isnan(m[3])
    np.testing.assert_allclose(m[4], 4.5)


# ---------------------------------------------------------------------------
# ts_std
# ---------------------------------------------------------------------------


def test_ts_std_matches_numpy_ddof1():
    rng = np.random.default_rng(0)
    x = col(rng.normal(size=20))
    out = op.ts_std(x, 5)[:, 0]
    for t in range(4, 20):
        assert out[t] == pytest.approx(np.std(x[t - 4 : t + 1, 0], ddof=1))
    assert np.isnan(out[:4]).all()


def test_ts_std_requires_two():
    with pytest.raises(ValueError):
        op.ts_std(col([1, 2, 3]), 1)


# ---------------------------------------------------------------------------
# ts_min / ts_max / ts_argmax / ts_argmin
# ---------------------------------------------------------------------------


def test_ts_min_max_rolling():
    x = col([3, 1, 2, 5, 4])
    np.testing.assert_array_equal(op.ts_min(x, 3)[2:, 0], [1, 1, 2])
    np.testing.assert_array_equal(op.ts_max(x, 3)[2:, 0], [3, 5, 5])


def test_ts_argmax_argmin_days_ago():
    x = col([3, 1, 2, 5, 4])
    # window [3,1,2] -> max at oldest -> 2 days ago; [1,2,5] -> today -> 0; [2,5,4] -> 1 ago
    np.testing.assert_array_equal(op.ts_argmax(x, 3)[2:, 0], [2, 0, 1])
    # min: [3,1,2]->1 ago; [1,2,5]->2 ago; [2,5,4]->2 ago
    np.testing.assert_array_equal(op.ts_argmin(x, 3)[2:, 0], [1, 2, 2])


def test_ts_argmax_nan_window():
    x = col([1, np.nan, 3, 4])
    out = op.ts_argmax(x, 2)[:, 0]
    assert np.isnan(out[1]) and np.isnan(out[2])  # windows containing the NaN
    assert out[3] == 0  # window [3,4] -> max today


# ---------------------------------------------------------------------------
# ts_rank
# ---------------------------------------------------------------------------


def test_ts_rank_monotone_series():
    inc = col([1, 2, 3, 4, 5])
    np.testing.assert_allclose(op.ts_rank(inc, 3)[2:, 0], [1.0, 1.0, 1.0])  # always the max
    dec = col([5, 4, 3, 2, 1])
    np.testing.assert_allclose(op.ts_rank(dec, 3)[2:, 0], [0.0, 0.0, 0.0])  # always the min


def test_ts_rank_midpoint_and_bounds():
    # window [10, 30, 20] -> current 20 is the middle -> (2-1)/(3-1) = 0.5
    x = col([10, 30, 20])
    assert op.ts_rank(x, 3)[2, 0] == pytest.approx(0.5)
    out = op.ts_rank(col([1, 9, 2, 8, 3, 7]), 4)[3:, 0]
    assert ((out >= 0) & (out <= 1)).all()


def test_ts_rank_requires_two():
    with pytest.raises(ValueError):
        op.ts_rank(col([1, 2, 3]), 1)


# ---------------------------------------------------------------------------
# ts_decay_linear
# ---------------------------------------------------------------------------


def test_ts_decay_linear_weights_and_constant():
    # weights 1,2,3 over [1,2,3] -> (1*1+2*2+3*3)/6 = 14/6
    assert op.ts_decay_linear(col([1, 2, 3]), 3)[2, 0] == pytest.approx(14 / 6)
    # newest day gets the largest weight: a constant stays constant
    np.testing.assert_allclose(op.ts_decay_linear(col([7, 7, 7, 7]), 3)[2:, 0], 7.0)


# ---------------------------------------------------------------------------
# ts_ema / ts_dema
# ---------------------------------------------------------------------------


def _ema_ref(x, d):
    a = 2.0 / (d + 1.0)
    out = np.empty_like(x)
    out[0] = x[0]
    for t in range(1, len(x)):
        out[t] = a * x[t] + (1 - a) * out[t - 1]
    return out


def test_ts_ema_recurrence():
    x = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
    np.testing.assert_allclose(op.ts_ema(col(x), 3)[:, 0], _ema_ref(x, 3), rtol=1e-12)


def test_ts_ema_carries_through_gap():
    x = col([10.0, np.nan, 12.0])
    out = op.ts_ema(x, 2)[:, 0]
    assert out[0] == 10.0
    assert out[1] == 10.0  # NaN day carries the previous level
    assert np.isfinite(out[2])


def test_ts_dema_identity():
    x = col([1.0, 2.0, 3.0, 4.0, 5.0])
    e = op.ts_ema(x, 3)
    np.testing.assert_allclose(op.ts_dema(x, 3), 2 * e - op.ts_ema(e, 3), rtol=1e-12)


# ---------------------------------------------------------------------------
# ts_skew / ts_kurt
# ---------------------------------------------------------------------------


def test_ts_skew_symmetric_is_zero():
    x = col([1, 2, 3, 2, 1, 2, 3, 2, 1]).astype(float)
    out = op.ts_skew(x, 3)[:, 0]
    # window [3,2,1] is symmetric about its mean -> skew 0
    assert out[4] == pytest.approx(0.0, abs=1e-12)


def test_ts_skew_matches_formula():
    rng = np.random.default_rng(1)
    x = col(rng.normal(size=12))
    out = op.ts_skew(x, 6)[:, 0]
    for t in range(5, 12):
        w = x[t - 5 : t + 1, 0]
        dev = w - w.mean()
        m2 = (dev**2).mean()
        m3 = (dev**3).mean()
        assert out[t] == pytest.approx(m3 / m2**1.5)


def test_ts_kurt_matches_formula():
    rng = np.random.default_rng(2)
    x = col(rng.normal(size=12))
    out = op.ts_kurt(x, 6)[:, 0]
    for t in range(5, 12):
        w = x[t - 5 : t + 1, 0]
        dev = w - w.mean()
        m2 = (dev**2).mean()
        m4 = (dev**4).mean()
        assert out[t] == pytest.approx(m4 / m2**2 - 3.0)


# ---------------------------------------------------------------------------
# ts_corr / ts_cov
# ---------------------------------------------------------------------------


def test_ts_corr_matches_numpy():
    rng = np.random.default_rng(3)
    x = col(rng.normal(size=25))
    y = col(rng.normal(size=25))
    out = op.ts_corr(x, y, 10)[:, 0]
    for t in range(9, 25):
        ref = np.corrcoef(x[t - 9 : t + 1, 0], y[t - 9 : t + 1, 0])[0, 1]
        assert out[t] == pytest.approx(ref, rel=1e-9)
    assert np.isnan(out[:9]).all()


def test_ts_corr_perfect_and_anti():
    x = col([1, 2, 3, 4, 5])
    assert op.ts_corr(x, 2 * x + 1, 5)[4, 0] == pytest.approx(1.0)
    assert op.ts_corr(x, -x, 5)[4, 0] == pytest.approx(-1.0)


def test_ts_corr_zero_variance_is_nan():
    x = col([5, 5, 5, 5])  # no variance
    y = col([1, 2, 3, 4])
    assert np.isnan(op.ts_corr(x, y, 3)[3, 0])


def test_ts_cov_matches_numpy_ddof1():
    rng = np.random.default_rng(4)
    x = col(rng.normal(size=15))
    y = col(rng.normal(size=15))
    out = op.ts_cov(x, y, 8)[:, 0]
    for t in range(7, 15):
        ref = np.cov(x[t - 7 : t + 1, 0], y[t - 7 : t + 1, 0], ddof=1)[0, 1]
        assert out[t] == pytest.approx(ref, rel=1e-9)


# ---------------------------------------------------------------------------
# per-symbol independence: time-series ops act column-wise
# ---------------------------------------------------------------------------


def test_ts_ops_are_per_symbol():
    x = mat([1, 2, 3, 4], [10, 20, 30, 40])
    out = op.ts_mean(x, 2)
    np.testing.assert_allclose(out[1:, 0], [1.5, 2.5, 3.5])
    np.testing.assert_allclose(out[1:, 1], [15, 25, 35])


@pytest.mark.parametrize(
    "fn",
    [op.ts_mean, op.ts_sum, op.ts_std, op.ts_min, op.ts_max, op.ts_rank, op.ts_decay_linear],
)
def test_ts_window_warmup_is_nan(fn):
    x = col(np.arange(1, 11))
    out = fn(x, 4)[:, 0]
    assert np.isnan(out[:3]).all()      # first d-1 rows are warm-up
    assert np.isfinite(out[3:]).all()   # everything after is defined


# ---------------------------------------------------------------------------
# review-driven: cov guard, EMA warm-up exception, tie conventions, NaN propagation
# ---------------------------------------------------------------------------


def test_ts_cov_requires_two():
    # consistent with ts_std/ts_rank: a 1-day window cannot define (co)variance
    with pytest.raises(ValueError):
        op.ts_cov(col([1, 2]), col([3, 4]), 1)


def test_ts_ema_is_finite_from_first_row():
    # EMA/DEMA intentionally seed at the first observation (no warm-up NaN),
    # unlike the windowed ts_* operators — pin this documented exception.
    assert np.isfinite(op.ts_ema(col([1, 2, 3]), 3)[0, 0])
    assert np.isfinite(op.ts_dema(col([1, 2, 3, 4]), 3)[0, 0])


def test_ts_ema_leading_nan_seeds_at_first_real_value():
    out = op.ts_ema(col([np.nan, 2.0, 3.0]), 2)[:, 0]
    assert np.isnan(out[0])
    assert out[1] == pytest.approx(2.0)  # seeded by the first finite observation
    assert np.isfinite(out[2])


def test_ts_argmax_argmin_ties_report_oldest():
    # filled.argmax/argmin pick the first (oldest) index among equal extrema
    assert op.ts_argmax(col([3, 5, 5]), 3)[2, 0] == 1.0  # older of the two 5s
    assert op.ts_argmax(col([5, 5, 5]), 3)[2, 0] == 2.0  # all equal -> oldest
    assert op.ts_argmin(col([3, 1, 1]), 3)[2, 0] == 1.0


def test_ts_rank_ties_count_as_leq_current():
    assert op.ts_rank(col([10, 10, 10]), 3)[2, 0] == 1.0   # all == current
    assert op.ts_rank(col([20, 10, 10]), 3)[2, 0] == 0.5   # current ties one, below one


def test_ts_skew_kurt_zero_variance_is_nan():
    assert np.isnan(op.ts_skew(col([5, 5, 5]), 3)[2, 0])
    assert np.isnan(op.ts_kurt(col([5, 5, 5]), 3)[2, 0])


def test_ts_corr_cov_interior_nan_propagates():
    x = col([1, 2, np.nan, 4, 5, 6, 7])
    y = col([1, 2, 3, 4, 5, 6, 7])
    c = op.ts_corr(x, y, 3)[:, 0]
    assert np.isnan(c[2]) and np.isnan(c[3]) and np.isnan(c[4])  # windows touching the NaN
    assert np.isfinite(c[5]) and np.isfinite(c[6])               # clean later windows
    assert np.isnan(op.ts_cov(x, y, 3)[2, 0])


@pytest.mark.parametrize("fn", [op.ts_delta, op.ts_returns, op.ts_min, op.ts_max, op.ts_corr, op.ts_std])
def test_ts_ops_independent_across_symbols(fn):
    x = mat([1, 2, 3, 4, 5], [50, 40, 30, 20, 10])
    args = (x, x, 3) if fn is op.ts_corr else (x, 3) if fn in (op.ts_min, op.ts_max, op.ts_std) else (x, 2)
    out = fn(*args)
    # recompute column 1 alone -> must match the multi-symbol result's column 1
    x1 = col([50, 40, 30, 20, 10])
    args1 = (x1, x1, 3) if fn is op.ts_corr else (x1, 3) if fn in (op.ts_min, op.ts_max, op.ts_std) else (x1, 2)
    np.testing.assert_allclose(out[:, 1], fn(*args1)[:, 0], equal_nan=True)
