"""Element-wise math, comparison and logical operator tests.

Covers the paper's math operators and Assay's safety variants, including NaN
propagation rules, the ``^`` power vs ``signedpower`` distinction, matrix
exponents, ``safe_div``, ``fillna`` methods, and the ``||`` logical-or.
Run with::

    PYTHONPATH=src python -m pytest tests/engine/test_operators_math.py -q
"""

from __future__ import annotations

import numpy as np
import pytest

from assay.engine import operators as op


# ---------------------------------------------------------------------------
# abs / log / sign / sqrt
# ---------------------------------------------------------------------------


def test_abs():
    np.testing.assert_array_equal(op.op_abs(np.array([-2.0, 3.0, 0.0])), [2.0, 3.0, 0.0])
    assert np.isnan(op.op_abs(np.array([np.nan]))[0])


def test_log_domain():
    out = op.op_log(np.array([np.e, 1.0, 0.0, -1.0]))
    np.testing.assert_allclose(out[:2], [1.0, 0.0])
    assert np.isnan(out[2]) and np.isnan(out[3])  # x <= 0 -> NaN


def test_sign():
    np.testing.assert_array_equal(op.op_sign(np.array([-3.0, 0.0, 5.0])), [-1.0, 0.0, 1.0])
    assert np.isnan(op.op_sign(np.array([np.nan]))[0])


def test_sqrt_domain():
    out = op.op_sqrt(np.array([4.0, 0.0, -1.0]))
    np.testing.assert_allclose(out[:2], [2.0, 0.0])
    assert np.isnan(out[2])


# ---------------------------------------------------------------------------
# signed_power vs pow
# ---------------------------------------------------------------------------


def test_signed_power_preserves_sign():
    np.testing.assert_allclose(op.signed_power(np.array([-2.0, 3.0]), 2), [-4.0, 9.0])


def test_pow_does_not_preserve_sign():
    np.testing.assert_allclose(op.op_pow(np.array([-2.0, 3.0]), 2), [4.0, 9.0])


def test_pow_and_signed_power_matrix_exponent():
    base = np.array([2.0, 3.0, 4.0])
    exp = np.array([3.0, 2.0, 0.5])
    np.testing.assert_allclose(op.op_pow(base, exp), [8.0, 9.0, 2.0])
    np.testing.assert_allclose(op.signed_power(base, exp), [8.0, 9.0, 2.0])


def test_pow_negative_base_fractional_is_nan():
    assert np.isnan(op.op_pow(np.array([-2.0]), 0.5)[0])


# ---------------------------------------------------------------------------
# clip / elem_min / elem_max
# ---------------------------------------------------------------------------


def test_clip():
    np.testing.assert_array_equal(op.op_clip(np.array([0.0, 5.0, 10.0]), 2, 8), [2.0, 5.0, 8.0])


def test_elem_min_max_and_nan_propagation():
    a = np.array([1.0, 5.0, np.nan])
    b = np.array([3.0, 2.0, 0.0])
    np.testing.assert_array_equal(op.elem_min(a, b)[:2], [1.0, 2.0])
    np.testing.assert_array_equal(op.elem_max(a, b)[:2], [3.0, 5.0])
    assert np.isnan(op.elem_min(a, b)[2]) and np.isnan(op.elem_max(a, b)[2])


# ---------------------------------------------------------------------------
# where / safe_div / sigmoid
# ---------------------------------------------------------------------------


def test_where_selects_and_propagates_nan():
    cond = np.array([1.0, 0.0, np.nan])
    a = np.array([5.0, 5.0, 5.0])
    b = np.array([9.0, 9.0, 9.0])
    out = op.op_where(cond, a, b)
    np.testing.assert_array_equal(out[:2], [5.0, 9.0])
    assert np.isnan(out[2])  # NaN condition -> NaN


def test_where_with_scalar_branches():
    cond = np.array([[1.0, 0.0]])
    np.testing.assert_array_equal(op.op_where(cond, 1.0, -1.0), [[1.0, -1.0]])


def test_safe_div():
    out = op.safe_div(np.array([1.0, 1.0]), np.array([2.0, 0.0]), fill=-1.0)
    np.testing.assert_array_equal(out, [0.5, -1.0])
    # default fill is 0
    assert op.safe_div(np.array([1.0]), np.array([0.0]))[0] == 0.0


def test_sigmoid():
    assert op.op_sigmoid(np.array([0.0]))[0] == pytest.approx(0.5)
    out = op.op_sigmoid(np.array([-10.0, 0.0, 10.0]))
    assert out[0] < out[1] < out[2]
    assert (out > 0).all() and (out < 1).all()


# ---------------------------------------------------------------------------
# fillna
# ---------------------------------------------------------------------------


def test_fillna_zero():
    np.testing.assert_array_equal(op.op_fillna(np.array([[1.0, np.nan, 3.0]]), "zero"), [[1.0, 0.0, 3.0]])


def test_fillna_median_cross_sectional():
    out = op.op_fillna(np.array([[1.0, np.nan, 3.0]]), "median")
    np.testing.assert_array_equal(out, [[1.0, 2.0, 3.0]])  # median of {1,3}


def test_fillna_ffill_along_time():
    x = np.array([[1.0], [np.nan], [np.nan], [4.0]])
    np.testing.assert_array_equal(op.op_fillna(x, "ffill")[:, 0], [1.0, 1.0, 1.0, 4.0])


def test_fillna_bad_method():
    with pytest.raises(ValueError):
        op.op_fillna(np.array([[1.0]]), "bogus")


# ---------------------------------------------------------------------------
# arithmetic
# ---------------------------------------------------------------------------


def test_arithmetic_and_broadcast():
    a = np.array([[1.0, 2.0]])
    b = np.array([[3.0, 4.0]])
    np.testing.assert_array_equal(op.op_add(a, b), [[4.0, 6.0]])
    np.testing.assert_array_equal(op.op_sub(a, b), [[-2.0, -2.0]])
    np.testing.assert_array_equal(op.op_mul(a, 2.0), [[2.0, 4.0]])      # scalar broadcast
    np.testing.assert_array_equal(op.op_div(a, 2.0), [[0.5, 1.0]])
    np.testing.assert_array_equal(op.op_neg(a), [[-1.0, -2.0]])


def test_div_by_zero_is_inf_not_error():
    out = op.op_div(np.array([1.0]), np.array([0.0]))
    assert np.isinf(out[0])  # plain division; use safe_div for a fill


# ---------------------------------------------------------------------------
# comparisons
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fn, expected",
    [
        (op.op_lt, [1.0, 0.0, 0.0]),
        (op.op_le, [1.0, 1.0, 0.0]),
        (op.op_gt, [0.0, 0.0, 1.0]),
        (op.op_ge, [0.0, 1.0, 1.0]),
        (op.op_eq, [0.0, 1.0, 0.0]),
        (op.op_ne, [1.0, 0.0, 1.0]),
    ],
)
def test_comparisons(fn, expected):
    a = np.array([1.0, 2.0, 3.0])
    b = np.array([2.0, 2.0, 2.0])
    np.testing.assert_array_equal(fn(a, b), expected)


def test_comparison_returns_float_and_propagates_nan():
    out = op.op_gt(np.array([5.0, np.nan]), np.array([0.0, 0.0]))
    assert out.dtype == np.float64
    assert out[0] == 1.0 and np.isnan(out[1])


# ---------------------------------------------------------------------------
# logical or
# ---------------------------------------------------------------------------


def test_or_truth_table():
    a = np.array([1.0, 1.0, 0.0, 0.0])
    b = np.array([1.0, 0.0, 1.0, 0.0])
    np.testing.assert_array_equal(op.op_or(a, b), [1.0, 1.0, 1.0, 0.0])


def test_or_nan_handling():
    # NaN treated as false; only NaN || NaN -> NaN
    out = op.op_or(np.array([np.nan, np.nan]), np.array([1.0, np.nan]))
    assert out[0] == 1.0 and np.isnan(out[1])


# ---------------------------------------------------------------------------
# review-driven: signed_power's defining behavior, or edge cases, saturation, NaN cmp
# ---------------------------------------------------------------------------


def test_signed_power_negative_base_fractional_stays_real():
    # the core distinction: signed_power keeps a negative base REAL (sign-preserved),
    # whereas plain pow goes NaN on the same input.
    np.testing.assert_allclose(op.signed_power(np.array([-4.0, -2.0]), 0.5), [-2.0, -np.sqrt(2)])
    assert np.isnan(op.op_pow(np.array([-4.0]), 0.5)[0])


def test_or_nan_is_false_and_truthy_coercion():
    # 0 || NaN and NaN || 0 are both 0.0 (NaN treated as false), not NaN
    np.testing.assert_array_equal(op.op_or(np.array([0.0, np.nan]), np.array([np.nan, 0.0])), [0.0, 0.0])
    # any non-zero value (negative or fractional) is truthy
    np.testing.assert_array_equal(op.op_or(np.array([2.0, 0.0]), np.array([-3.0, 0.5])), [1.0, 1.0])


def test_sigmoid_saturation_stays_in_open_interval():
    out = op.op_sigmoid(np.array([-1000.0, 1000.0]))
    assert out[0] >= 0.0 and out[1] <= 1.0
    assert out[0] == pytest.approx(0.0, abs=1e-12)
    assert out[1] == pytest.approx(1.0, abs=1e-12)


def test_equality_on_nan_propagates_not_ieee():
    # kernel overrides IEEE: NaN == NaN -> NaN (not False); NaN != x -> NaN (not True)
    assert np.isnan(op.op_eq(np.array([np.nan]), np.array([np.nan]))[0])
    assert np.isnan(op.op_ne(np.array([np.nan]), np.array([1.0]))[0])
