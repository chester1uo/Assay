"""Comprehensive tests for the factor diagnostics system.

Covers all three stages — parse, execute, output — the stable error codes, the
located caret snippets, the structured ``to_dict`` payload, the ``failure_mode``
mapping, ``lint`` (panel-free) and ``FactorEngine.diagnose`` (full pipeline).
Run with::

    PYTHONPATH=src python -m pytest tests/engine/test_diagnostics.py -q
"""

from __future__ import annotations

import datetime as dt
import json

import numpy as np
import polars as pl
import pytest

from assay.engine import FactorEngine, Severity, Stage, lint
from assay.engine.ast import FieldNode, OpNode
from assay.engine import diagnostics as dg


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

T, N = 30, 6


@pytest.fixture(scope="module")
def engine():
    rng = np.random.default_rng(0)
    dates = [dt.date(2024, 1, 1) + dt.timedelta(days=i) for i in range(T)]
    syms = [f"S{j}" for j in range(N)]
    close = 100 + rng.normal(size=(T, N)).cumsum(axis=0)
    vol = 1e6 + rng.normal(size=(T, N)) * 1e4
    panel = pl.DataFrame(
        {
            "date": np.repeat(np.array(dates), N),
            "symbol": syms * T,
            "close": close.reshape(-1),
            "volume": vol.reshape(-1),
        }
    )
    return FactorEngine(panel, group_data=None)


def names(fd):
    return {d.code.name for d in fd.diagnostics}


def first_error(fd):
    return fd.errors[0]


# ---------------------------------------------------------------------------
# catalog integrity
# ---------------------------------------------------------------------------


def test_catalog_codes_unique_and_well_formed():
    ids = [c.id for c in dg.CATALOG.values()]
    assert len(ids) == len(set(ids))  # unique code ids
    for c in dg.CATALOG.values():
        assert c.id.startswith("ASSAY-")
        assert isinstance(c.severity, Severity) and isinstance(c.stage, Stage)
        assert c.hint  # every code carries a default suggestion


# ---------------------------------------------------------------------------
# PARSE stage (panel-free via lint)
# ---------------------------------------------------------------------------

PARSE_CASES = [
    ("", "EMPTY_EXPRESSION"),
    ("   ", "EMPTY_EXPRESSION"),
    ("close @ open", "UNEXPECTED_CHARACTER"),
    ("ts_mean(close 5)", "UNEXPECTED_TOKEN"),
    ("ts_mean(close,", "UNEXPECTED_EOF"),
    ("close + ", "UNEXPECTED_EOF"),
    ("cs_rank(close))", "TRAILING_TOKENS"),
    ("ts_meen(close, 5)", "UNKNOWN_OPERATOR"),
    ("notafunc(close)", "UNKNOWN_OPERATOR"),
    ("ts_corr(close, volume)", "OPERATOR_ARITY"),
    ("ts_mean(close)", "OPERATOR_ARITY"),
    ("rank(close, 5, 6)", "OPERATOR_ARITY"),
    ("ts_mean(close, close)", "INVALID_WINDOW"),
    ("ts_corr(close, volume, open)", "INVALID_WINDOW"),
    ("cs_neutralize(close, 5)", "INVALID_ARGUMENT"),
]


@pytest.mark.parametrize("expr, code", PARSE_CASES)
def test_parse_stage_codes(expr, code):
    fd = lint(expr)
    assert fd.status == "error"
    assert fd.stage_reached == "parse"
    assert fd.failure_mode == "SYNTAX_ERROR"
    assert len(fd.errors) == 1  # exactly one diagnostic — no spurious extras
    assert first_error(fd).code.name == code


def test_parse_error_has_location_and_snippet():
    fd = lint("ts_corr(close, volume)")
    err = first_error(fd)
    assert err.span == (0, 7)  # points at 'ts_corr'
    snippet = err.snippet()
    assert "^^^^^^^" in snippet
    # caret sits under the operator
    expr_line, caret_line = snippet.splitlines()
    assert caret_line.index("^") == 0


def test_parse_unexpected_character_points_at_char():
    fd = lint("close @ open")
    err = first_error(fd)
    assert err.code.name == "UNEXPECTED_CHARACTER"
    assert err.span == (6, 7)  # the '@'


def test_lint_ok_expression_has_no_diagnostics():
    fd = lint("cs_rank(ts_corr(close, volume, 20))")
    assert fd.ok and fd.status == "ok"
    assert fd.diagnostics == []
    assert fd.result is None  # lint does not evaluate


# ---------------------------------------------------------------------------
# EXECUTE stage (needs the engine/panel)
# ---------------------------------------------------------------------------


def test_unknown_field(engine):
    fd = engine.diagnose("ts_mean(vwap, 5)")
    assert fd.status == "error" and fd.stage_reached == "execute"
    err = first_error(fd)
    assert err.code.name == "UNKNOWN_FIELD"
    assert err.span == dg.locate("ts_mean(vwap, 5)", "vwap")
    assert err.context["field"] == "vwap"
    assert "close" in err.context["available"]


def test_multiple_unknown_fields_all_reported(engine):
    fd = engine.diagnose("ts_corr(foo, bar, 10)")
    assert {d.context.get("field") for d in fd.errors} == {"foo", "bar"}


def test_unregistered_operator_via_prebuilt_ast(engine):
    # parse() rejects unknown names, so feed a hand-built AST to reach E002
    node = OpNode("not_a_real_op", (FieldNode("close"),))
    fd = engine.diagnose(node)
    assert first_error(fd).code.name == "UNREGISTERED_OPERATOR"


@pytest.mark.parametrize(
    "expr, code",
    [
        ("ts_std(close, 1)", "INVALID_OPERATOR_PARAM"),
        ("ts_rank(close, 1)", "INVALID_OPERATOR_PARAM"),
        ("cs_winsorize(close, 0.9)", "INVALID_OPERATOR_PARAM"),
        ("fillna(close, 'bogus')", "INVALID_OPERATOR_PARAM"),
        ("ts_mean(close, 0)", "INVALID_OPERATOR_PARAM"),  # window must be >= 1
    ],
)
def test_execute_runtime_param_errors(engine, expr, code):
    fd = engine.diagnose(expr)
    assert fd.status == "error" and fd.stage_reached == "execute"
    assert first_error(fd).code.name == code
    assert fd.failure_mode == "RUNTIME_ERROR"


def test_lookahead_negative_delay_is_its_own_failure_mode(engine):
    # a negative shift peeks into the future -> LOOKAHEAD, not a generic param error
    fd = engine.diagnose("ts_delay(close, -1)")
    err = first_error(fd)
    assert err.code.name == "LOOKAHEAD_SHIFT"
    assert fd.failure_mode == "LOOKAHEAD"


def test_error_type_records_original_exception(engine):
    fd = engine.diagnose("ts_std(close, 1)")  # kernel raises a plain ValueError
    assert first_error(fd).context["error_type"] == "ValueError"


def test_operator_runtime_error_generic(engine):
    from assay.engine import operators

    def boom(x):
        raise RuntimeError("kaboom")

    operators.register("boom", boom, 1, 1, category="custom")
    try:
        fd = engine.diagnose("boom(close)")
    finally:
        operators.unregister("boom")
    err = first_error(fd)
    assert err.code.name == "OPERATOR_RUNTIME_ERROR"
    assert err.context["error_type"] == "RuntimeError" and err.context["operator"] == "boom"


def test_no_data_classification():
    d = dg.from_runtime_error(ValueError("cannot build a FactorEngine on an empty panel"), "x")
    assert d.code.name == "NO_DATA"


def test_missing_group_data(engine):
    fd = engine.diagnose("cs_neutralize(close, 'sector')")
    err = first_error(fd)
    assert err.code.name == "MISSING_GROUP_DATA"
    assert err.context.get("operator") == "cs_neutralize"
    assert err.span == dg.locate("cs_neutralize(close, 'sector')", "cs_neutralize")


def test_group_data_present_makes_it_ok():
    rng = np.random.default_rng(1)
    dates = [dt.date(2024, 1, 1) + dt.timedelta(days=i) for i in range(40)]
    syms = [f"S{j}" for j in range(8)]
    close = 100 + rng.normal(size=(40, 8)).cumsum(axis=0)
    panel = pl.DataFrame(
        {"date": np.repeat(np.array(dates), 8), "symbol": syms * 40, "close": close.reshape(-1)}
    )
    eng = FactorEngine(panel, group_data={"sector": {s: "A" if i % 2 else "B" for i, s in enumerate(syms)}})
    fd = eng.diagnose("cs_neutralize(close, 'sector')")
    assert fd.ok and fd.stage_reached == "output"


# ---------------------------------------------------------------------------
# OUTPUT stage — unit tests of output_diagnostics on crafted matrices
# ---------------------------------------------------------------------------


def out_names(values, **kw):
    diags, stats = dg.output_diagnostics(np.asarray(values, float), **kw)
    return {d.code.name for d in diags}, stats


def test_output_all_nan():
    nms, stats = out_names(np.full((5, 4), np.nan))
    assert nms == {"ALL_NAN"}
    assert stats["coverage"] == 0.0


def test_output_all_inf_reports_breakdown():
    diags, _ = dg.output_diagnostics(np.full((5, 4), np.inf))
    assert diags[0].code.name == "ALL_NAN"
    assert diags[0].context["inf_count"] == 20 and diags[0].context["nan_count"] == 0


def test_output_high_nan_fraction():
    v = np.ones((10, 4))
    v[:7] = np.nan  # 70% NaN
    nms, stats = out_names(v)
    assert "HIGH_NAN_FRACTION" in nms
    assert stats["nan_fraction"] == pytest.approx(0.7)


def test_output_constant():
    nms, _ = out_names(np.full((6, 5), 3.0))
    assert "CONSTANT_OUTPUT" in nms


def test_output_constant_per_date_even_if_time_varies():
    # each date is internally constant across symbols (no cross-sectional signal)
    v = np.tile(np.arange(8).reshape(-1, 1), (1, 5)).astype(float)
    nms, _ = out_names(v)
    assert "CONSTANT_OUTPUT" in nms


def test_output_non_finite_when_some_inf():
    v = np.random.default_rng(7).normal(size=(6, 5))  # varied base isolates the code
    v[0, 0] = np.inf
    nms, stats = out_names(v)
    assert nms == {"NON_FINITE_VALUES"}  # exactly — not also CONSTANT
    assert stats["inf_count"] == 1


def test_output_extreme_values():
    v = np.random.default_rng(8).normal(size=(6, 5))
    v[0, 0] = 1e15
    nms, _ = out_names(v)
    assert nms == {"EXTREME_VALUES"}


def test_output_near_constant():
    # varies (so not CONSTANT) but the dynamic range is negligible vs the level
    v = 1000.0 + np.random.default_rng(9).normal(size=(10, 6)) * 1e-9
    nms, _ = out_names(v)
    assert "NEAR_CONSTANT_OUTPUT" in nms and "CONSTANT_OUTPUT" not in nms


def test_output_low_coverage():
    v = np.full((100, 100), np.nan)
    v[0, 0] = 1.0  # one finite cell -> coverage 1e-4
    nms, stats = out_names(v)
    assert "LOW_COVERAGE" in nms
    assert stats["coverage"] == pytest.approx(1e-4)


def test_output_one_dimensional_path():
    nms, stats = out_names(np.array([1.0, 2.0, np.nan, 4.0]))
    assert stats["n_symbols"] == 1
    assert isinstance(nms, set)  # the 1-D branch must not crash
    assert out_names(np.full(5, np.nan))[0] == {"ALL_NAN"}


def test_output_excessive_warmup():
    v = np.full((20, 5), np.nan)
    v[18:] = 1.0  # only the last 2 of 20 rows are valid
    nms, stats = out_names(v)
    assert "EXCESSIVE_WARMUP" in nms
    assert stats["warmup_rows"] == 18


def test_output_degenerate_cross_section():
    v = np.full((10, 5), np.nan)
    v[:, 0] = 1.0  # only one symbol ever has values -> no cross-section
    nms, _ = out_names(v)
    assert "DEGENERATE_CROSS_SECTION" in nms


def test_output_clean_factor_has_no_warnings():
    rng = np.random.default_rng(3)
    v = rng.normal(size=(20, 8))
    nms, stats = out_names(v)
    assert nms == set()
    assert stats["coverage"] == 1.0 and stats["constant_date_fraction"] == 0.0


# ---------------------------------------------------------------------------
# OUTPUT stage — integration through diagnose()
# ---------------------------------------------------------------------------


def test_diagnose_all_nan_long_window(engine):
    fd = engine.diagnose("ts_mean(close, 100)")  # window > history
    assert first_error(fd).code.name == "ALL_NAN"
    assert fd.failure_mode == "ALL_NAN" and not fd.ok


def test_diagnose_constant_is_warning_but_ok(engine):
    fd = engine.diagnose("close * 0 + 1")
    assert "CONSTANT_OUTPUT" in names(fd)
    assert fd.status == "warning" and fd.ok  # ran fine, just no signal
    assert fd.failure_mode == "CONSTANT"


def test_diagnose_high_nan_and_warmup(engine):
    fd = engine.diagnose("ts_mean(close, 20)")  # 19/30 rows warm up
    assert {"HIGH_NAN_FRACTION", "EXCESSIVE_WARMUP"} <= names(fd)
    assert fd.status == "warning" and fd.ok


def test_diagnose_division_by_zero_inf(engine):
    fd = engine.diagnose("1 / (close - close)")
    # all cells are +inf -> ALL_NAN with an inf-dominated breakdown
    err = first_error(fd)
    assert err.code.name == "ALL_NAN" and err.context["inf_count"] > 0


def test_diagnose_ok_attaches_result_and_stats(engine):
    fd = engine.diagnose("cs_rank(ts_returns(close, 5))")
    assert fd.ok and fd.status == "ok" and fd.failure_mode is None
    assert fd.result is not None and fd.result.shape == (T, N)
    assert 0 < fd.stats["coverage"] <= 1.0
    assert fd.stats["n_dates"] == T and fd.stats["n_symbols"] == N


# ---------------------------------------------------------------------------
# structured output for the agent
# ---------------------------------------------------------------------------


def test_to_dict_is_json_serialisable_and_structured(engine):
    fd = engine.diagnose("ts_corr(close, volume)")  # arity error
    d = fd.to_dict()
    text = json.dumps(d)  # must not raise
    assert json.loads(text)["status"] == "error"
    e = d["errors"][0]
    assert set(e) >= {"code", "name", "severity", "stage", "title", "message", "location", "context", "suggestion"}
    assert e["code"] == "ASSAY-P007"
    assert e["location"]["snippet"].count("^") == 7
    assert e["suggestion"]  # actionable


def test_diagnose_never_raises(engine):
    weird = ["", ")(", "ts_mean(", "@@@", "ts_std(close,1)", "ts_mean(close,100)",
             "cs_neutralize(close,'x')", "1/(close-close)", "close", "foo(bar)",
             "abs(" * 400 + "close" + ")" * 400]  # pathological nesting
    for e in weird:
        fd = engine.diagnose(e)  # must always return, never throw
        assert isinstance(fd, dg.FactorDiagnostics)
        assert fd.status in {"ok", "warning", "error"}


def test_deep_nesting_is_coded_not_recursion_error(engine):
    deep = "abs(" * 400 + "close" + ")" * 400
    fd = engine.diagnose(deep)  # must not raise RecursionError
    assert first_error(fd).code.name == "EXPRESSION_TOO_DEEP"
    assert lint(deep).errors[0].code.name == "EXPRESSION_TOO_DEEP"


def test_lint_accepts_ast_node_without_error():
    from assay.engine import parse
    node = parse("ts_mean(close, 5)")
    fd = lint(node)  # a pre-built AST has no syntax to check
    assert fd.ok and fd.diagnostics == []


def test_lint_does_not_validate_fields():
    # lint is panel-free: an unknown field is NOT an error here (only via diagnose)
    fd = lint("ts_mean(notafield, 5)")
    assert fd.ok


def test_diagnose_accepts_prebuilt_ast(engine):
    from assay.engine import parse
    node = parse("ts_mean(close, 5)")
    fd = engine.diagnose(node)
    assert fd.ok and fd.result is not None


# ---------------------------------------------------------------------------
# helpers: locate / caret_snippet
# ---------------------------------------------------------------------------


def test_locate_finds_field_and_dollar_field():
    assert dg.locate("ts_corr(close, volume, 20)", "close") == (8, 13)
    assert dg.locate("Corr($close, $volume, 20)", "close") == (5, 11)  # includes the $
    assert dg.locate("ts_mean(close, 5)", "open") is None


def test_caret_snippet_alignment():
    snip = dg.caret_snippet("abcdef", (2, 4))
    assert snip == "abcdef\n  ^^"
    assert dg.caret_snippet("x", None) is None
