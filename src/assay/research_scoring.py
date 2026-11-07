"""Score explicit trading dates, excluding warmup and forward-label buffers."""
from __future__ import annotations

from datetime import date
import math
from statistics import mean, stdev


def validate_score_window(period, horizons, score_period, score_dates):
    if score_period is None and score_dates is None:
        return None
    if not isinstance(score_period, list) or len(score_period) != 2:
        raise ValueError("score_period requires start and end ISO dates")
    start, end = [str(date.fromisoformat(d)) for d in score_period]
    if not period[0] <= start <= end <= period[1]:
        raise ValueError("score_period must be contained in the data-read period")
    if len(horizons) != 1:
        raise ValueError("explicit score windows currently require exactly one horizon")
    if not isinstance(score_dates, list) or not score_dates:
        raise ValueError("score_dates must freeze the expected trading dates")
    days = [str(date.fromisoformat(d)) for d in score_dates]
    if days != sorted(set(days)) or any(not start <= d <= end for d in days):
        raise ValueError("score_dates must be sorted, unique and inside score_period")
    return dict(score_period=[start, end], score_dates=days)


def apply_score_window(report: dict, dataset: dict) -> dict:
    """Produce a report whose headline metrics describe only the frozen score dates.

    Non-recomputable turnover/group/decay summaries are explicitly unavailable.
    The caller retains the full original panel report separately for provenance.
    """
    # Copy provenance explicitly. New backend aggregates must not silently leak
    # into scoped reports merely because a provider adds another metric field.
    metadata = (
        "factor_id", "expr", "expr_canonical", "universe_id", "market", "n_symbols",
        "universe_as_of", "universe_symbols_hash", "data_as_of", "execution", "adj",
        "neutralize", "lineage", "duration_ms", "failure_mode", "suggestion",
        "lookahead_detected", "diagnostics", "schema_version", "granularity",
    )
    result = {key: report[key] for key in metadata if key in report}
    expected = dataset["score_dates"]
    result.update(panel_period=list(dataset["period"]), eval_period=list(dataset["score_period"]),
                  score_period=list(dataset["score_period"]), expected_score_dates=list(expected),
                  ic=None, icir=None, rank_ic=None, rank_icir=None, ic_by_horizon={},
                  turnover_1d=None, decay_halflife_days=None, quintile_returns=None,
                  dates=[], ic_series=[], rank_ic_series=[], n_dates=0, scoring_complete=False,
                  unavailable_score_metrics=["turnover_1d", "decay_halflife_days", "quintile_returns"],
                  diagnostics_scope=("expression_policy" if report.get("failure_mode") == "EXPRESSION_POLICY_VIOLATION" else "panel"))
    if report.get("failure_mode"):
        return result
    dates = report.get("dates")
    rank = report.get("rank_ic_series")
    ic = report.get("ic_series")
    problem = None
    if not isinstance(dates, list) or not isinstance(rank, list) or not isinstance(ic, list):
        problem = "backend must return dates, rank_ic_series and ic_series"
    elif len(dates) != len(rank) or len(dates) != len(ic):
        problem = "backend metric series are not aligned with dates"
    elif any(not isinstance(d, str) for d in dates) or dates != sorted(set(dates)):
        problem = "backend dates must be unique and sorted"
    elif any(not dataset["period"][0] <= d <= dataset["period"][1] for d in dates):
        problem = "backend dates extend beyond the registered data-read period"
    if problem is None:
        pairs = [(d, x, y) for d, x, y in zip(dates, ic, rank)
                 if dataset["score_period"][0] <= d <= dataset["score_period"][1]]
        if [d for d, _, _ in pairs] != expected:
            problem = "observed score dates differ from the frozen trading calendar"
        elif any(type(v) not in (int, float) or not math.isfinite(v) for _, x, y in pairs for v in (x, y)):
            problem = "one or more score dates have missing or non-finite IC/RankIC"
    if problem:
        result.update(failure_mode="INCOMPLETE_SCORE_COVERAGE", suggestion=problem)
        return result

    def summarize(values):
        avg = mean(values)
        std = stdev(values) if len(values) > 1 else 0
        return avg, avg / std if std > 0 else None

    score_ic = [x for _, x, _ in pairs]
    score_rank = [y for _, _, y in pairs]
    avg_ic, icir = summarize(score_ic)
    avg_rank, rank_icir = summarize(score_rank)
    result.update(dates=list(expected), ic_series=score_ic, rank_ic_series=score_rank,
                  ic=avg_ic, icir=icir, rank_ic=avg_rank, rank_icir=rank_icir,
                  ic_by_horizon={str(dataset["horizons"][0]): avg_rank}, n_dates=len(expected),
                  scoring_complete=True)
    return result
