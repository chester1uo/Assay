"""qlib-dialect coverage extensions (for AlphaBench-style factors).

Covers the parser/operator surface added so the engine can ingest the qlib
spellings AlphaBench generates: scientific-notation literals, function-form
arithmetic (``Add/Sub/Mul/Div``), element-wise ``Greater/Less``, comparison /
logical function forms, and the rolling operators ``Var/Med/Mad/Count/Quantile/
Slope/Resi/Rsquare``.
"""

from __future__ import annotations

import numpy as np
import pytest

from assay.engine import parse
from assay.engine.ast import LitNode
from assay.engine.operators import time_series as T


# -- scientific-notation literals --------------------------------------------
def test_scientific_notation_literal():
    node = parse("$close / ($high - $low + 1e-12)")
    assert node.struct_hash()  # parses
    # the epsilon folds to a float literal
    assert parse("1e-12") == LitNode(1e-12)
    assert parse("1.5e3") == LitNode(1500.0)
    assert parse("2E-6") == LitNode(2e-6)


# -- function-form arithmetic == infix form ----------------------------------
@pytest.mark.parametrize(
    "func, infix",
    [
        ("Add($close, $open)", "$close + $open"),
        ("Sub($close, $open)", "$close - $open"),
        ("Mul($close, $volume)", "$close * $volume"),
        ("Div($close, $open)", "$close / $open"),
    ],
)
def test_function_form_arithmetic_equivalence(func, infix):
    assert parse(func).struct_hash() == parse(infix).struct_hash()


def test_greater_less_map_to_elementwise():
    assert parse("Greater($open, $close)").struct_hash() == parse("elem_max($open, $close)").struct_hash()
    assert parse("Less($open, $close)").struct_hash() == parse("elem_min($open, $close)").struct_hash()


def test_comparison_and_logical_function_forms_parse():
    for e in ["Gt($close,$open)", "Lt($close,$open)", "Ge($close,$open)",
              "Le($close,$open)", "Eq($close,$open)", "Ne($close,$open)",
              "And(Gt($close,$open), Gt($volume,0))", "Or($close>$open, $volume>0)",
              "Not(Gt($close,$open))"]:
        assert parse(e).struct_hash()


# -- new rolling operators: numeric correctness ------------------------------
def _last(fn, *a):
    return float(fn(*a)[-1, 0])


def test_rolling_var_med_mad():
    x = np.array([1, 2, 3, 4, 5], dtype=float).reshape(5, 1)
    assert _last(T.ts_var, x, 5) == pytest.approx(2.5)        # sample variance (ddof=1)
    assert _last(T.ts_med, x, 5) == pytest.approx(3.0)
    assert _last(T.ts_mad, x, 5) == pytest.approx(1.2)        # mean(|x-mean|)


def test_rolling_quantile_and_count():
    x = np.array([1, 2, 3, 4, 5], dtype=float).reshape(5, 1)
    assert _last(T.ts_quantile, x, 5, 0.5) == pytest.approx(3.0)
    assert _last(T.ts_quantile, x, 5, 0.25) == pytest.approx(2.0)
    c = np.array([0, 1, 0, 2, 3], dtype=float).reshape(5, 1)
    assert _last(T.ts_count, c, 5) == pytest.approx(3.0)      # three nonzero


def test_rolling_regression_slope_rsquare_resi():
    y = (2 * np.arange(5) + 1.0).reshape(5, 1)                # perfect line y = 2t + 1
    assert _last(T.ts_slope, y, 5) == pytest.approx(2.0)
    assert _last(T.ts_rsquare, y, 5) == pytest.approx(1.0)
    assert _last(T.ts_resi, y, 5) == pytest.approx(0.0)       # residual on a perfect fit


def test_rolling_ops_need_min_window():
    x = np.array([1.0, 2.0]).reshape(2, 1)
    for fn in (T.ts_var, T.ts_slope, T.ts_rsquare):
        with pytest.raises(ValueError):
            fn(x, 1)


def test_rolling_window_is_nan_until_full():
    # full-window semantics: the first d-1 rows are NaN
    x = np.arange(1, 6, dtype=float).reshape(5, 1)
    out = T.ts_med(x, 3)
    assert np.isnan(out[:2, 0]).all()
    assert out[2, 0] == pytest.approx(2.0)


def test_alphabench_style_factor_end_to_end():
    # a representative AlphaBench expression exercises func-arith + sci-notation + slope
    e = "Mul(Slope($close, 10), Div(Sub($close, $open), Add($high - $low, 1e-12)))"
    assert parse(e).struct_hash()
