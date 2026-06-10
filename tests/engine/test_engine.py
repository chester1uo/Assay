"""Tests for the factor execution engine: parsing, operators, evaluation.

Offline only — no network or ingested data required. Run with::

    PYTHONPATH=src python -m pytest tests/engine/test_engine.py -q
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import polars as pl
import pytest

from assay.engine import (
    FactorEngine,
    ParseError,
    detect_dialect,
    iter_fields,
    iter_ops,
    operators,
    parse,
)
from assay.engine.ast import FieldNode, LitNode, OpNode


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def make_panel(close: np.ndarray, *, extra: dict[str, np.ndarray] | None = None) -> pl.DataFrame:
    """Build a long (date, symbol, close[, extra...]) panel from a (T, N) matrix."""
    t, n = close.shape
    dates = [dt.date(2024, 1, 1) + dt.timedelta(days=i) for i in range(t)]
    symbols = [f"S{j}" for j in range(n)]
    data = {
        "date": np.repeat(np.array(dates), n),
        "symbol": symbols * t,
        "close": close.reshape(-1).astype(float),
    }
    for name, mat in (extra or {}).items():
        data[name] = mat.reshape(-1).astype(float)
    return pl.DataFrame(data)


def evaluate(expr, close, **extra):
    eng = FactorEngine(make_panel(close, extra=extra or None))
    return eng.evaluate(expr).values


# ---------------------------------------------------------------------------
# parsing: dialect equivalence, detection, aliases, macros
# ---------------------------------------------------------------------------

# (qlib, function-call/Alpha-101) pairs that MUST lower to the same AST.
EQUIVALENT_PAIRS = [
    ("Ref($close, 5)", "ts_delay(close, 5)"),
    ("Ref($close, 5)", "delay(close, 5)"),
    ("Mean($close, 20)", "ts_mean(close, 20)"),
    ("Std($close, 20)", "stddev(close, 20)"),
    ("Corr($close, $volume, 20)", "correlation(close, volume, 20)"),
    ("Rank($close)", "rank(close)"),
    ("EMA($close, 12)", "ts_ema(close, 12)"),
    ("Delta($close, 1)", "ts_delta(close, 1)"),
    ("Mean($volume, 20)", "adv20"),
    ("Abs($close - $open)", "abs(close - open)"),
    ("Sign($close - $open)", "sign(close - open)"),
]


@pytest.mark.parametrize("qlib_expr, func_expr", EQUIVALENT_PAIRS)
def test_dialects_lower_to_same_ast(qlib_expr, func_expr):
    assert parse(qlib_expr).struct_hash() == parse(func_expr).struct_hash()


def test_detect_dialect():
    assert detect_dialect("Corr($close, $volume, 20)") == "qlib"
    assert detect_dialect("Mean($close, 5)") == "qlib"  # CamelCase qlib op, no $
    assert detect_dialect("ts_corr(close, volume, 20)") == "func"
    assert detect_dialect("(returns < 0) ? close : volume") == "func"


def test_float_windows_floor():
    # "101 Formulaic Alphas": a non-integer number of days d is converted to floor(d).
    node = parse("ts_decay_linear(ts_corr(vwap, volume, 3.93), 7.89)")
    assert node == OpNode(
        "ts_decay_linear",
        (OpNode("ts_corr", (FieldNode("vwap"), FieldNode("volume"), LitNode(3))), LitNode(7)),
    )
    assert parse("adv5.85").struct_hash() == parse("ts_mean(volume, 5)").struct_hash()


def test_adv_macro_and_returns_field():
    assert parse("adv20").struct_hash() == parse("ts_mean(volume, 20)").struct_hash()
    assert parse("returns").struct_hash() == parse("ts_returns(close, 1)").struct_hash()
    assert parse("cap").struct_hash() == parse("market_cap").struct_hash()


def test_rank_arity_disambiguation():
    # rank(x) -> cross-sectional; rank(x, d) -> time-series
    assert parse("rank(close)").op == "cs_rank"
    assert parse("Rank($close)").op == "cs_rank"
    assert parse("Ts_Rank(close, 5)").op == "ts_rank"
    assert parse("rank(close, 5)").op == "ts_rank"


def test_min_max_rolling_vs_elementwise():
    # min(x, <int>) is rolling; min(x, y) is element-wise.
    assert parse("min(close, 5)").op == "ts_min"
    assert parse("max(close, 10)").op == "ts_max"
    assert parse("min(close, open)").op == "elem_min"
    assert parse("max(close, volume)").op == "elem_max"


def test_signed_power_distinct_from_pow():
    assert parse("SignedPower(close, 2)").op == "signed_power"
    assert parse("pow(close, 2)").op == "pow"
    assert parse("SignedPower(close, 2)").struct_hash() != parse("pow(close, 2)").struct_hash()


def test_indneutralize_group_argument():
    a = parse("IndNeutralize(vwap, IndClass.sector)")
    b = parse("cs_neutralize(vwap, 'sector')")
    assert a == b == OpNode("cs_neutralize", (FieldNode("vwap"), LitNode("sector")))


def test_caret_is_pow():
    # ^ is the paper's plain power operator (distinct from signed_power).
    assert parse("(high * low)^0.5") == OpNode(
        "pow", (OpNode("mul", (FieldNode("high"), FieldNode("low"))), LitNode(0.5))
    )
    # exponent may itself be an expression (e.g. rank(...)^rank(...))
    assert parse("rank(close)^rank(open)").op == "pow"


def test_logical_or():
    node = parse("(close > open) || (volume == 1)")
    assert node == OpNode(
        "or",
        (
            OpNode("gt", (FieldNode("close"), FieldNode("open"))),
            OpNode("eq", (FieldNode("volume"), LitNode(1))),
        ),
    )


def test_pow_and_or_evaluate():
    close = np.array([[2.0, 3.0], [4.0, 5.0]])
    np.testing.assert_allclose(evaluate("close^2", close), close**2)
    # element-wise exponent matrix
    out = evaluate("signed_power(close, close)", close)
    np.testing.assert_allclose(out, np.sign(close) * np.abs(close) ** close)
    orr = evaluate("(close > 3) || (close < 1)", close)
    np.testing.assert_allclose(orr, [[0.0, 0.0], [1.0, 1.0]])


def test_ternary_is_where():
    node = parse("(close < open) ? close : open")
    assert node == OpNode(
        "where",
        (OpNode("lt", (FieldNode("close"), FieldNode("open"))), FieldNode("close"), FieldNode("open")),
    )


def test_iter_fields_and_ops():
    node = parse("cs_rank(ts_corr(close, volume, 20)) - 0.5")
    assert iter_fields(node) == {"close", "volume"}
    assert iter_ops(node) == {"sub", "cs_rank", "ts_corr"}


# ---------------------------------------------------------------------------
# parse errors
# ---------------------------------------------------------------------------


def test_parse_errors():
    with pytest.raises(ParseError):
        parse("ts_mean(close")  # unbalanced paren
    with pytest.raises(ParseError):
        parse("totally_unknown_op(close, 5)")  # unknown operator name
    with pytest.raises(ValueError):
        parse("ts_corr(close, 20)")  # wrong arity (needs 3 args)
    with pytest.raises(ParseError):
        parse("")  # empty


# ---------------------------------------------------------------------------
# operator kernels: exact numeric correctness on small arrays
# ---------------------------------------------------------------------------


def test_ts_delay_and_returns():
    x = np.array([[1.0], [2.0], [4.0], [8.0]])  # one symbol, geometric
    d = operators.ts_delay(x, 1)
    assert np.isnan(d[0, 0])
    assert d[1, 0] == 1.0 and d[3, 0] == 4.0
    r = operators.ts_returns(x, 1)
    np.testing.assert_allclose(r[1:, 0], [1.0, 1.0, 1.0])  # doubles each step


def test_ts_mean_std_warmup():
    x = np.arange(1, 6, dtype=float).reshape(-1, 1)  # 1,2,3,4,5
    m = operators.ts_mean(x, 3)
    assert np.isnan(m[0, 0]) and np.isnan(m[1, 0])  # warm-up
    np.testing.assert_allclose(m[2:, 0], [2.0, 3.0, 4.0])
    s = operators.ts_std(x, 3)
    np.testing.assert_allclose(s[2:, 0], [1.0, 1.0, 1.0])  # sample std of consecutive ints


def test_ts_corr_matches_numpy():
    rng = np.random.default_rng(1)
    x = rng.normal(size=(30, 1))
    y = rng.normal(size=(30, 1))
    got = operators.ts_corr(x, y, 10)
    expected = np.corrcoef(x[20:30, 0], y[20:30, 0])[0, 1]
    np.testing.assert_allclose(got[29, 0], expected, rtol=1e-10)


def test_ts_corr_zero_variance_is_nan():
    x = np.ones((5, 1))
    y = np.arange(5, dtype=float).reshape(-1, 1)
    assert np.isnan(operators.ts_corr(x, y, 3)[4, 0])  # x has no variance


def test_ts_rank_and_argmax():
    x = np.arange(1, 11, dtype=float).reshape(-1, 1)  # strictly increasing
    rank = operators.ts_rank(x, 5)
    assert rank[9, 0] == 1.0  # current is the max of its window
    am = operators.ts_argmax(x, 5)
    assert am[9, 0] == 0.0  # max is today (0 days ago)
    amin = operators.ts_argmin(x, 5)
    assert amin[9, 0] == 4.0  # min is the oldest in the window


def test_ts_decay_linear_constant():
    x = np.full((6, 1), 7.0)
    out = operators.ts_decay_linear(x, 4)
    np.testing.assert_allclose(out[3:, 0], 7.0)  # weighted mean of a constant


def test_cs_rank_zscore():
    x = np.array([[10.0, 20.0, 30.0]])  # one date, three symbols
    np.testing.assert_allclose(operators.cs_rank(x)[0], [0.0, 0.5, 1.0])
    z = operators.cs_zscore(x)[0]
    np.testing.assert_allclose(z.mean(), 0.0, atol=1e-12)


def test_cs_rank_handles_nan():
    x = np.array([[10.0, np.nan, 30.0]])
    r = operators.cs_rank(x)[0]
    assert np.isnan(r[1])
    np.testing.assert_allclose([r[0], r[2]], [0.0, 1.0])


def test_signed_power_and_pow():
    x = np.array([[-2.0, 3.0]])
    np.testing.assert_allclose(operators.signed_power(x, 2), [[-4.0, 9.0]])
    np.testing.assert_allclose(operators.op_pow(x, 2), [[4.0, 9.0]])


def test_where_and_safe_div():
    cond = np.array([[1.0, 0.0]])
    a = np.array([[5.0, 5.0]])
    b = np.array([[9.0, 9.0]])
    np.testing.assert_allclose(operators.op_where(cond, a, b), [[5.0, 9.0]])
    np.testing.assert_allclose(operators.safe_div(np.array([[1.0]]), np.array([[0.0]]), -1.0), [[-1.0]])


def test_comparison_nan_propagates():
    a = np.array([[1.0, np.nan]])
    b = np.array([[0.0, 0.0]])
    out = operators.op_gt(a, b)
    assert out[0, 0] == 1.0 and np.isnan(out[0, 1])


# ---------------------------------------------------------------------------
# end-to-end evaluation through FactorEngine
# ---------------------------------------------------------------------------


def test_evaluate_cs_rank_in_unit_interval():
    rng = np.random.default_rng(2)
    close = 100 + rng.normal(size=(40, 6)).cumsum(axis=0)
    out = evaluate("cs_rank(ts_returns(close, 5))", close)
    finite = out[np.isfinite(out)]
    assert finite.size > 0
    assert finite.min() >= 0.0 and finite.max() <= 1.0


def test_evaluate_shape_and_frame_roundtrip():
    close = np.arange(1, 31, dtype=float).reshape(10, 3)
    eng = FactorEngine(make_panel(close))
    res = eng.evaluate("ts_mean(close, 3)")
    assert res.shape == (10, 3)
    frame = res.to_frame()
    assert frame.columns == ["date", "symbol", "factor"]
    assert frame.height == 30


def test_evaluate_constant_broadcasts():
    close = np.ones((4, 2))
    out = evaluate("close * 0 + 1", close)
    assert out.shape == (4, 2)
    np.testing.assert_allclose(out, 1.0)


def test_unknown_field_raises():
    with pytest.raises(ValueError, match="not in the panel"):
        evaluate("ts_mean(vwap, 3)", np.ones((5, 2)))


# ---------------------------------------------------------------------------
# group operators
# ---------------------------------------------------------------------------


def test_cs_neutralize_subtracts_group_mean():
    close = np.array([[1.0, 3.0, 10.0, 30.0]])  # groups: A={0,1}, B={2,3}
    panel = make_panel(close)
    groups = {"sector": {"S0": "A", "S1": "A", "S2": "B", "S3": "B"}}
    out = FactorEngine(panel, group_data=groups).evaluate("cs_neutralize(close, 'sector')").values
    # within-group demean: A mean 2 -> [-1, 1]; B mean 20 -> [-10, 10]
    np.testing.assert_allclose(out[0], [-1.0, 1.0, -10.0, 10.0])


def test_group_op_without_data_raises():
    with pytest.raises(ValueError, match="group data"):
        evaluate("cs_neutralize(close, 'sector')", np.ones((3, 2)))


# ---------------------------------------------------------------------------
# Alpha-101 coverage: the four worked examples from the compat doc
# ---------------------------------------------------------------------------

ALPHA_EXAMPLES = {
    "alpha1": "rank(Ts_ArgMax(SignedPower(((returns < 0) ? stddev(returns, 20) : close), 2.), 5)) - 0.5",
    "alpha7": "(adv20 < volume) ? ((-1 * ts_rank(abs(delta(close, 7)), 60)) * sign(delta(close, 7))) : (-1 * 1)",
    "alpha98": (
        "rank(decay_linear(correlation(vwap, sum(adv5, 26.47), 4.58), 7.18)) - "
        "rank(decay_linear(Ts_Rank(Ts_ArgMin(correlation(rank(open), rank(adv15), 20.82), 8.63), 6.96), 8.07))"
    ),
}


@pytest.mark.parametrize("name, expr", list(ALPHA_EXAMPLES.items()))
def test_alpha101_examples_parse(name, expr):
    node = parse(expr)
    assert all(operators.is_registered(op) for op in iter_ops(node))


def test_alpha101_examples_evaluate():
    rng = np.random.default_rng(3)
    t, n = 90, 12
    close = 100 + rng.normal(size=(t, n)).cumsum(axis=0)
    extra = {
        "open": close + rng.normal(size=(t, n)),
        "vwap": close + rng.normal(size=(t, n)) * 0.2,
        "volume": 1e6 + rng.normal(size=(t, n)) * 1e4,
    }
    eng = FactorEngine(make_panel(close, extra=extra))
    for expr in ALPHA_EXAMPLES.values():
        res = eng.evaluate(expr)
        assert res.shape == (t, n)
        assert np.isfinite(res.values).any()  # at least the warm rows resolve
