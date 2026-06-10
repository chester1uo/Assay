"""User-defined custom operators: register -> parse -> evaluate end-to-end.

Demonstrates the public extension API (``operators.op`` / ``operators.register``)
and verifies the parser resolves any registered name, in expressions, through the
engine. Run with::

    PYTHONPATH=src python -m pytest tests/engine/test_custom_operators.py -q
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import polars as pl
import pytest

from assay.engine import FactorEngine, ParseError, operators, parse


@pytest.fixture
def registry_guard():
    """Remove any operators registered during a test so the global registry is clean."""
    before = set(operators.all_specs())
    yield
    for name in set(operators.all_specs()) - before:
        operators.unregister(name)


def _panel(close, syms=None):
    t, n = close.shape
    syms = syms or [f"S{j}" for j in range(n)]
    dates = [dt.date(2024, 1, 1) + dt.timedelta(days=i) for i in range(t)]
    return pl.DataFrame(
        {
            "date": np.repeat(np.array(dates), n),
            "symbol": syms * t,
            "close": close.reshape(-1).astype(float),
        }
    )


# ---------------------------------------------------------------------------
# the documented example
# ---------------------------------------------------------------------------


def test_op_decorator_example_zscore(registry_guard):
    @operators.op("ts_zscore", 2, 2, category="custom", output_range="(-inf, inf)")
    def ts_zscore(x, d):
        return (x - operators.ts_mean(x, d)) / operators.ts_std(x, d)

    assert operators.is_registered("ts_zscore")
    # the parser now resolves the custom name (in a composite, both arities)
    node = parse("cs_rank(ts_zscore(close, 5))")
    assert "ts_zscore" in str(node)

    close = 100 + np.random.default_rng(0).normal(size=(20, 6)).cumsum(axis=0)
    got = FactorEngine(_panel(close)).evaluate("ts_zscore(close, 5)").values
    expected = (close - operators.ts_mean(close, 5)) / operators.ts_std(close, 5)
    np.testing.assert_allclose(got, expected, equal_nan=True, rtol=1e-12)


def test_register_function_form(registry_guard):
    def double_close(x):
        return np.asarray(x, dtype=float) * 2.0  # any (T,N)->(T,N) kernel

    operators.register("dbl", double_close, 1, 1, category="custom")
    close = np.array([[1.0, 2.0], [3.0, 4.0]])
    np.testing.assert_allclose(FactorEngine(_panel(close)).evaluate("dbl(close)").values, close * 2)


# ---------------------------------------------------------------------------
# parser / arity / lifecycle
# ---------------------------------------------------------------------------


def test_unknown_operator_until_registered(registry_guard):
    with pytest.raises(ParseError, match="unknown operator"):
        parse("myfactor(close, 3)")
    operators.register("myfactor", lambda x, d: operators.ts_mean(x, d), 2, 2)
    assert parse("myfactor(close, 3)").op == "myfactor"  # resolves once registered


def test_custom_operator_arity_is_checked(registry_guard):
    operators.register("needs_two", lambda x, d: x, 2, 2, category="custom")
    with pytest.raises(ValueError, match="takes 2"):
        parse("needs_two(close)")  # only 1 arg


def test_unregister_removes_operator(registry_guard):
    operators.register("temp_op", lambda x: x, 1, 1)
    assert operators.is_registered("temp_op")
    operators.unregister("temp_op")
    assert not operators.is_registered("temp_op")
    with pytest.raises(ParseError):
        parse("temp_op(close)")


def test_custom_operator_shows_in_live_schema(registry_guard):
    operators.register("myop", lambda x: x, 1, 1, category="custom", output_range="[0, 1]")
    schema = operators.operator_schema()
    assert schema["myop"]["category"] == "custom"
    assert "myop" in operators.all_specs()


# ---------------------------------------------------------------------------
# a context-aware (group) custom operator
# ---------------------------------------------------------------------------


def test_custom_needs_ctx_group_operator(registry_guard):
    @operators.op("cs_gdemean", 2, 2, needs_ctx=True, category="custom")
    def cs_gdemean(x, group, *, ctx):
        labels = ctx.require_groups(group)
        out = np.array(x, dtype=float).copy()
        for lab in set(labels):
            cols = [i for i, lv in enumerate(labels) if lv == lab]
            out[:, cols] = out[:, cols] - out[:, cols].mean(axis=1, keepdims=True)
        return out

    syms = ["S0", "S1", "S2", "S3"]
    close = np.array([[1.0, 3.0, 10.0, 30.0]])
    eng = FactorEngine(
        _panel(close, syms),
        group_data={"sector": {"S0": "A", "S1": "A", "S2": "B", "S3": "B"}},
    )
    out = eng.evaluate("cs_gdemean(close, 'sector')").values
    np.testing.assert_allclose(out[0], [-1.0, 1.0, -10.0, 10.0])  # demeaned within group
