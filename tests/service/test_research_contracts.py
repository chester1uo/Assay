"""Frozen scoring scopes and pre-execution expression policies prevent leakage."""
from __future__ import annotations

from copy import deepcopy

import pytest

from assay.research import ResearchWorkspace
from assay.research_policy import check_expression, normalize_policy


POLICY = dict(fields=["close", "volume"], operators=["ts_mean", "ts_returns", "ts_quantile", "ts_dema", "cs_rank"],
              max_window=60, max_cumulative_lookback=100)


class Backend:
    def __init__(self):
        self.calls = 0
        self.report = dict(failure_mode=None, rank_ic=0.99, rank_icir=99, ic=0.8, icir=80,
            dates=["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04"],
            rank_ic_series=[0.99, -0.1, 0.3, None], ic_series=[0.8, -0.2, 0.4, None],
            n_dates=4, turnover_1d=0.8, decay_halflife_days=9, quintile_returns=[0.8],
            ic_by_horizon={1: 0.99}, eval_period=["2024-01-01", "2024-01-04"],
            future_backend_aggregate=123)

    def evaluate(self, expr, dataset):
        self.calls += 1
        return deepcopy(self.report)


@pytest.fixture
def workspace(tmp_path):
    backend = Backend()
    ws = ResearchWorkspace(tmp_path / "research.sqlite3", {"test": backend})
    dataset = ws.register_dataset("scoped", "TEST", ["2024-01-01", "2024-01-04"], backend="test",
        horizons=[1], score_period=["2024-01-02", "2024-01-03"],
        score_dates=["2024-01-02", "2024-01-03"], expression_policy=POLICY)
    sid = ws.create_session(dataset["id"], "strict scoped evaluation")["id"]
    return ws, backend, dataset, sid


def test_headline_excludes_warmup_and_labels_and_survives_restart(workspace):
    ws, backend, dataset, sid = workspace
    attempt = ws.evaluate(sid, "ts_mean(close,5)", "scoring", request_id="first")
    report = attempt["report"]
    assert attempt["status"] == "completed" and report["scoring_complete"]
    assert report["rank_ic"] == pytest.approx(0.1)
    assert report["ic"] == pytest.approx(0.1)
    assert report["n_dates"] == 2
    assert report["dates"] == dataset["score_dates"]
    assert report["eval_period"] == dataset["score_period"]
    assert report["panel_period"] == dataset["period"]
    assert report["turnover_1d"] is None and report["quintile_returns"] is None
    assert report["decay_halflife_days"] is None
    assert attempt["panel_report"]["rank_ic"] == 0.99
    assert "future_backend_aggregate" not in report
    assert attempt["panel_report"]["future_backend_aggregate"] == 123
    assert attempt["policy_check"]["cumulative_lookback"] == 4
    assert ws.get_experiment(attempt["id"]) == attempt
    resumed = ResearchWorkspace(ws.path, {"test": backend})
    assert resumed.get_experiment(attempt["id"]) == attempt
    assert resumed.evaluate(sid, "ts_mean(close,5)", "scoring", request_id="first") == attempt
    assert backend.calls == 1


@pytest.mark.parametrize("change", ["missing_date", "missing_rank", "missing_ic", "duplicate_date", "missing_series", "misaligned_series", "extra_score_date"])
def test_incomplete_scores_never_fall_back_to_panel_aggregate(workspace, change):
    ws, backend, _, sid = workspace
    r = backend.report
    if change == "missing_date":
        for key in ("dates", "rank_ic_series", "ic_series"):
            del r[key][1]
    elif change == "missing_rank":
        r["rank_ic_series"][1] = None
    elif change == "missing_ic":
        r["ic_series"][1] = None
    elif change == "duplicate_date":
        r["dates"][1] = r["dates"][0]
    elif change == "missing_series":
        del r["rank_ic_series"]
    elif change == "misaligned_series":
        r["ic_series"].pop()
    else:
        r["dates"][2] = "2024-01-02T12:00:00"
    a = ws.evaluate(sid, "ts_mean(close,5)", "scoring", request_id="first")
    assert a["status"] == "failed"
    assert a["report"]["failure_mode"] == "INCOMPLETE_SCORE_COVERAGE"
    assert a["report"]["rank_ic"] is None and a["report"]["ic_by_horizon"] == {}
    assert a["panel_report"]["rank_ic"] == 0.99


@pytest.mark.parametrize("expr", ["ts_mean(close,61)", "ts_mean(ts_mean(close,60),60)", "ts_mean(open,5)", "ts_std(close,5)"])
def test_policy_rejection_is_recorded_before_backend_execution(workspace, expr):
    ws, backend, _, sid = workspace
    attempt = ws.evaluate(sid, expr, "invalid proposal", request_id="first")
    assert attempt["status"] == "failed"
    assert attempt["report"]["failure_mode"] == "EXPRESSION_POLICY_VIOLATION"
    assert attempt["policy_check"]["issues"]
    assert backend.calls == 0
    assert ws.get_session(sid)["remaining_experiments"] == 19


@pytest.mark.parametrize("expr,expected", [
    ("Mean(Ref($close,5),20)", 24), ("ts_mean(ts_returns(close,5),20)", 24),
    ("ts_quantile(close,20,0.8)", 19), ("ts_dema(close,20)", 38),
])
def test_canonical_window_semantics(expr, expected):
    policy = dict(POLICY, operators=POLICY["operators"] + ["ts_delay"])
    check = check_expression(expr, normalize_policy(policy))
    assert check["ok"]
    assert check["cumulative_lookback"] == expected


@pytest.mark.parametrize("kw", [
    dict(score_period=["2023-12-01", "2024-01-03"], score_dates=["2024-01-02"]),
    dict(score_period=["2024-01-02", "2024-01-03"], score_dates=[]),
    dict(score_period=["2024-01-02", "2024-01-03"], score_dates=["2024-01-03", "2024-01-02"]),
    dict(score_period=["2024-01-02", "2024-01-03"], score_dates=["2024-01-02"], horizons=[1, 5]),
    dict(expression_policy=dict(POLICY, max_window=0)),
])
def test_invalid_contract_rejected_on_registration(workspace, kw):
    ws, _, _, _ = workspace
    args = dict(backend="test", horizons=[1])
    args.update(kw)
    with pytest.raises(ValueError):
        ws.register_dataset("invalid", "TEST", ["2024-01-01", "2024-01-04"], **args)


def test_score_scope_and_policy_change_spec_identity(workspace):
    ws, _, first, _ = workspace
    second = ws.register_dataset("different", "TEST", first["period"], backend="test", horizons=[1],
        score_period=first["score_period"], score_dates=["2024-01-02"], expression_policy=POLICY)
    assert first["spec_hash"] != second["spec_hash"]
