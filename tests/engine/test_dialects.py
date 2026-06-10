"""qlib vs function-call dialect equivalence.

Both front-end syntaxes lower to the *same* unified AST and the *same* operator
backend (engineering-docs section 4.1). For a broad set of operator pairs we
assert (a) identical ``struct_hash`` (same tree) and (b) identical evaluation on
a shared panel. Run with::

    PYTHONPATH=src python -m pytest tests/engine/test_dialects.py -q
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import polars as pl
import pytest

from assay.engine import FactorEngine, detect_dialect, parse

# (qlib expression, function-call expression) — must be exactly equivalent.
PAIRS = [
    ("$close", "close"),
    ("Ref($close, 5)", "ts_delay(close, 5)"),
    ("Ref($close, 5)", "delay(close, 5)"),
    ("Delta($close, 1)", "ts_delta(close, 1)"),
    ("Delta($close, 1)", "delta(close, 1)"),
    ("Mean($close, 20)", "ts_mean(close, 20)"),
    ("Sum($volume, 5)", "ts_sum(volume, 5)"),
    ("Std($close, 10)", "ts_std(close, 10)"),
    ("Std($close, 10)", "stddev(close, 10)"),
    ("Product($close, 3)", "ts_product(close, 3)"),
    ("Corr($close, $volume, 10)", "ts_corr(close, volume, 10)"),
    ("Corr($close, $volume, 10)", "correlation(close, volume, 10)"),
    ("Cov($close, $volume, 10)", "ts_cov(close, volume, 10)"),
    ("EMA($close, 12)", "ts_ema(close, 12)"),
    ("DEMA($close, 12)", "ts_dema(close, 12)"),
    ("WMA($close, 5)", "ts_decay_linear(close, 5)"),
    ("WMA($close, 5)", "decay_linear(close, 5)"),
    ("Rank($close)", "cs_rank(close)"),
    ("Rank($close)", "rank(close)"),
    ("Min($close, 5)", "ts_min(close, 5)"),
    ("Max($close, 5)", "ts_max(close, 5)"),
    ("IdxMax($close, 5)", "ts_argmax(close, 5)"),
    ("IdxMin($close, 5)", "ts_argmin(close, 5)"),
    ("Power($close, 2)", "pow(close, 2)"),
    ("Abs($close - $open)", "abs(close - open)"),
    ("Log($close)", "log(close)"),
    ("Sqrt($close)", "sqrt(close)"),
    ("Sign($close - $open)", "sign(close - open)"),
    ("Clip($close, 0, 200)", "clip(close, 0, 200)"),
    ("Sigmoid($close - $open)", "sigmoid(close - open)"),
    ("If($close > $open, $close, $open)", "where(close > open, close, open)"),
    ("($close - $open) / $open", "(close - open) / open"),
    ("($close > $open)", "(close > open)"),
    ("Rank(Corr($close, $volume, 10))", "cs_rank(ts_corr(close, volume, 10))"),
    ("Mean(Ref($close, 1), 10)", "ts_mean(ts_delay(close, 1), 10)"),
]


@pytest.mark.parametrize("qlib_expr, func_expr", PAIRS)
def test_same_ast(qlib_expr, func_expr):
    assert parse(qlib_expr).struct_hash() == parse(func_expr).struct_hash()


def test_dialect_detection():
    for qlib_expr, _ in PAIRS:
        assert detect_dialect(qlib_expr) == "qlib"
    # function-call expressions that don't use $ or CamelCase qlib ops
    for _, func_expr in PAIRS:
        if "$" not in func_expr and detect_dialect(func_expr) != "qlib":
            assert detect_dialect(func_expr) == "func"
    assert detect_dialect("Mean(close, 5)") == "qlib"          # CamelCase op, no $
    assert detect_dialect("ts_corr(close, volume, 20)") == "func"


# ---------------------------------------------------------------------------
# both dialects must evaluate to the SAME numbers
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def engine():
    rng = np.random.default_rng(0)
    T, N = 40, 8
    dates = [dt.date(2024, 1, 1) + dt.timedelta(days=i) for i in range(T)]
    syms = [f"S{j}" for j in range(N)]
    close = 100 + rng.normal(size=(T, N)).cumsum(axis=0)
    panel = pl.DataFrame(
        {
            "date": np.repeat(np.array(dates), N),
            "symbol": syms * T,
            "open": (close + rng.normal(size=(T, N))).reshape(-1),
            "close": close.reshape(-1),
            "volume": (1e6 + rng.normal(size=(T, N)) * 1e4).reshape(-1),
        }
    )
    return FactorEngine(panel)


@pytest.mark.parametrize("qlib_expr, func_expr", PAIRS)
def test_same_evaluation(engine, qlib_expr, func_expr):
    a = engine.evaluate(qlib_expr).values
    b = engine.evaluate(func_expr).values
    np.testing.assert_allclose(a, b, equal_nan=True, rtol=1e-12)


def test_dollar_prefix_is_stripped_and_lowercased():
    assert parse("$Close").struct_hash() == parse("close").struct_hash()
    assert parse("$VWAP").struct_hash() == parse("vwap").struct_hash()


def test_full_alpha_in_both_dialects_match(engine):
    # a non-trivial composite expressed both ways
    qlib = "Rank(Corr($close, $volume, 10)) - (Mean($close, 5) / $close)"
    func = "cs_rank(ts_corr(close, volume, 10)) - (ts_mean(close, 5) / close)"
    assert parse(qlib).struct_hash() == parse(func).struct_hash()
    np.testing.assert_allclose(
        engine.evaluate(qlib).values, engine.evaluate(func).values, equal_nan=True, rtol=1e-12
    )


# ---------------------------------------------------------------------------
# review-driven: overloaded-name resolution (the most bug-prone parser path)
# ---------------------------------------------------------------------------


def test_rank_resolves_by_arity():
    assert parse("rank(close)").op == "cs_rank"                 # 1 arg -> cross-sectional
    assert parse("Rank($close)").op == "cs_rank"
    assert parse("Rank(close, 5)").op == "ts_rank"              # 2 args -> time-series
    assert parse("Ts_Rank(close, 5)").op == "ts_rank"


def test_min_max_resolve_by_second_arg_type():
    # literal 2nd arg -> rolling (time-series); expression 2nd arg -> element-wise
    assert parse("min(close, 5)").op == "ts_min"
    assert parse("max(close, 10)").op == "ts_max"
    assert parse("Min(close, open)").op == "elem_min"
    assert parse("max(close, vwap)").op == "elem_max"


def test_window_floor_coercion_normalizes_dialects():
    # non-integer windows floor; int and float literals collapse to the same tree
    assert parse("ts_mean(close, 7.9)").struct_hash() == parse("Mean($close, 7)").struct_hash()
    assert parse("correlation(close, volume, 4.99)").struct_hash() == parse("Corr($close, $volume, 4)").struct_hash()
