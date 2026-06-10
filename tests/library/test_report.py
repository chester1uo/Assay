"""Tests for the FactorReport contract — engineering-docs section 7.2.

Covers the machine-readable protocol the agent loop consumes: JSON-safe
``to_dict`` (NaN/inf -> None, tuples -> lists, nested Lineage/diagnostics
flattened), the ``from_dict`` inverse for scalar fields, ``json.dumps`` of the
serialised form, the stable ``compute_factor_id`` hash, and the
``FactorSummary.from_report`` projection.

Offline only — no network or ingested data required. Run with::

    PYTHONPATH=src python -m pytest tests/library/test_report.py -q
"""

from __future__ import annotations

import json
import math

import pytest

from assay.library import FactorReport, FactorSummary, Lineage


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def make_report(**overrides) -> FactorReport:
    """A fully-populated report (incl. optional detail) for round-trip tests."""
    kwargs = dict(
        factor_id="abc123",
        expr="ts_mean(close, 5)",
        expr_canonical="ts_mean(close,5)",
        ic=0.04,
        icir=0.5,
        rank_ic=0.06,
        rank_icir=0.8,
        ic_by_horizon={1: 0.02, 5: 0.04, 10: 0.03},
        decay_halflife_days=7,
        turnover_1d=0.12,
        redundancy_score=0.6,
        most_similar_factor="xyz789",
        lookahead_detected=False,
        failure_mode=None,
        suggestion="try a longer window",
        eval_period=("2020-01-01", "2020-12-31"),
        universe_id="SP500",
        n_dates=252,
        n_symbols=500,
        execution="next_open",
        neutralize=["sector"],
        lineage=Lineage(source="AGENT", prompt_hash="ph", adj_version="v1"),
        ic_series=[0.01, 0.02, 0.03],
        rank_ic_series=[0.02, 0.03, 0.04],
        dates=["2020-01-01", "2020-01-02", "2020-01-03"],
        quintile_returns=[0.01, 0.02, 0.03, 0.04, 0.05],
        duration_ms=12.5,
    )
    kwargs.update(overrides)
    return FactorReport(**kwargs)


# ---------------------------------------------------------------------------
# compute_factor_id: stability & determinism
# ---------------------------------------------------------------------------
def test_compute_factor_id_stable_and_deterministic():
    """Same canonical expr -> same 16-hex id across calls; different expr -> different id."""
    expr = "ts_rank(close,20)"
    fid = FactorReport.compute_factor_id(expr)
    assert fid == FactorReport.compute_factor_id(expr)  # stable
    assert len(fid) == 16
    assert all(c in "0123456789abcdef" for c in fid)  # lowercase hex
    assert FactorReport.compute_factor_id("a") != FactorReport.compute_factor_id("b")


def test_compute_factor_id_matches_sha256_prefix():
    """The id is exactly the SHA-256[:16] hex of the canonical expression."""
    import hashlib

    expr = "cs_rank(ts_mean(close,10))"
    expected = hashlib.sha256(expr.encode("utf-8")).hexdigest()[:16]
    assert FactorReport.compute_factor_id(expr) == expected


# ---------------------------------------------------------------------------
# to_dict: JSON-safety (NaN/inf -> None, tuples -> lists)
# ---------------------------------------------------------------------------
def test_to_dict_nan_and_inf_become_none():
    """Non-finite metric floats map to None so the payload is JSON-safe."""
    r = make_report(ic=float("nan"), icir=float("inf"), turnover_1d=float("-inf"))
    d = r.to_dict()
    assert d["ic"] is None
    assert d["icir"] is None
    assert d["turnover_1d"] is None
    # finite values pass through unchanged
    assert d["rank_ic"] == pytest.approx(0.06)


def test_to_dict_nan_inside_series_become_none():
    """NaN inside the IC series is cleaned element-wise to None."""
    r = make_report(ic_series=[0.1, float("nan"), 0.2])
    assert r.to_dict()["ic_series"] == [0.1, None, 0.2]


def test_to_dict_tuple_to_list_and_horizon_keys_int():
    """eval_period tuple becomes a list; ic_by_horizon keys stay ints."""
    d = make_report().to_dict()
    assert d["eval_period"] == ["2020-01-01", "2020-12-31"]
    assert isinstance(d["eval_period"], list)
    assert set(d["ic_by_horizon"].keys()) == {1, 5, 10}


def test_to_dict_lineage_flattened():
    """Nested Lineage is flattened into a plain dict via its to_dict."""
    d = make_report().to_dict()
    assert d["lineage"]["source"] == "AGENT"
    assert d["lineage"]["prompt_hash"] == "ph"
    assert isinstance(d["lineage"], dict)


def test_to_dict_json_dumps_succeeds_with_nan():
    """json.dumps over a report carrying NaN/inf must not raise (they were cleaned)."""
    r = make_report(ic=float("nan"), rank_icir=float("inf"), ic_series=[float("nan"), 1.0])
    s = json.dumps(r.to_dict())  # would raise allow_nan default only on real NaN
    # confirm no literal NaN/Infinity tokens leaked into the JSON text
    assert "NaN" not in s
    assert "Infinity" not in s
    # to_json convenience wrapper agrees with json.dumps(to_dict())
    assert json.loads(r.to_json()) == json.loads(s)


# ---------------------------------------------------------------------------
# round-trip: to_dict -> from_dict
# ---------------------------------------------------------------------------
def test_round_trip_preserves_scalar_fields():
    """from_dict(to_dict(r)) reproduces identity, metrics and context fields."""
    r = make_report()
    r2 = FactorReport.from_dict(r.to_dict())
    assert r2.factor_id == r.factor_id
    assert r2.expr == r.expr
    assert r2.expr_canonical == r.expr_canonical
    assert r2.ic == pytest.approx(r.ic)
    assert r2.rank_icir == pytest.approx(r.rank_icir)
    assert r2.ic_by_horizon == r.ic_by_horizon
    assert r2.decay_halflife_days == r.decay_halflife_days
    assert r2.universe_id == r.universe_id
    assert r2.n_dates == r.n_dates
    assert r2.neutralize == r.neutralize
    assert r2.suggestion == r.suggestion


def test_round_trip_eval_period_stays_tuple():
    """eval_period survives the list<->tuple boundary as a tuple."""
    r2 = FactorReport.from_dict(make_report().to_dict())
    assert r2.eval_period == ("2020-01-01", "2020-12-31")
    assert isinstance(r2.eval_period, tuple)


def test_round_trip_lineage_rebuilt():
    """Lineage is rebuilt as a Lineage object with its source preserved."""
    r2 = FactorReport.from_dict(make_report().to_dict())
    assert isinstance(r2.lineage, Lineage)
    assert r2.lineage.source == "AGENT"
    assert r2.lineage.prompt_hash == "ph"


def test_round_trip_via_json_text():
    """Full JSON text round-trip (dumps -> loads -> from_dict) is lossless for scalars."""
    r = make_report()
    r2 = FactorReport.from_dict(json.loads(r.to_json()))
    assert r2.to_dict() == r.to_dict()


def test_from_dict_tolerates_missing_optional_keys():
    """Older/minimal payloads still load: absent keys fall back to defaults."""
    minimal = {
        "factor_id": "m1",
        "expr": "close",
        "expr_canonical": "close",
        "ic": 0.0,
        "icir": 0.0,
        "rank_ic": 0.0,
        "rank_icir": 0.0,
    }
    r = FactorReport.from_dict(minimal)
    assert r.factor_id == "m1"
    assert r.ic_by_horizon == {}
    assert r.eval_period == ("", "")
    assert r.execution == "next_open"
    assert r.neutralize is None
    assert isinstance(r.lineage, Lineage)
    assert r.lineage.source == "AGENT"


# ---------------------------------------------------------------------------
# Lineage round-trip
# ---------------------------------------------------------------------------
def test_lineage_round_trip_and_empty_default():
    """Lineage.from_dict inverts to_dict; None/empty -> defaults (source=AGENT)."""
    lin = Lineage(prompt_hash="h", data_snapshot_id="snap", source="HUMAN")
    assert Lineage.from_dict(lin.to_dict()) == lin
    assert Lineage.from_dict(None) == Lineage()
    assert Lineage.from_dict({}).source == "AGENT"


# ---------------------------------------------------------------------------
# FactorSummary.from_report
# ---------------------------------------------------------------------------
def test_factor_summary_from_report_projects_headline_fields():
    """from_report copies identity + headline quality and pulls source from lineage."""
    r = make_report()
    s = FactorSummary.from_report(r)
    assert s.factor_id == r.factor_id
    assert s.expr == r.expr
    assert s.rank_ic == pytest.approx(r.rank_ic)
    assert s.rank_icir == pytest.approx(r.rank_icir)
    assert s.ic == pytest.approx(r.ic)
    assert s.decay_halflife_days == r.decay_halflife_days
    assert s.redundancy_score == pytest.approx(r.redundancy_score)
    assert s.turnover_1d == pytest.approx(r.turnover_1d)
    assert s.failure_mode == r.failure_mode
    assert s.source == r.lineage.source  # source comes from lineage
    assert s.universe_id == r.universe_id


def test_factor_summary_round_trip():
    """FactorSummary survives to_dict/from_dict and json.dumps (NaN -> None)."""
    s = FactorSummary.from_report(make_report(ic=float("nan")))
    d = s.to_dict()
    assert d["ic"] is None  # NaN cleaned
    json.dumps(d)  # must not raise
    s2 = FactorSummary.from_dict(d)
    assert s2.factor_id == s.factor_id
    assert s2.rank_ic == pytest.approx(s.rank_ic)
    assert s2.source == s.source
