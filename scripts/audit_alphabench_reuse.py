"""Check smoke artifacts and all candidate DSL policies, without model calls."""
from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import math
import sys
from pathlib import Path
from statistics import mean
from types import SimpleNamespace

from smoke_alphabench_reuse import ROOT, sha, worker


async def audit(training, holdout, output, abroot, ab):
    atomic = ab.provenance.atomic_json
    read = lambda path: json.loads(path.read_text())
    frozen = read(training / "frozen-selections.json")
    summary = read(holdout / "summary.json")
    if ab.provenance.payload_hash(frozen["selections"]) != frozen["selection_hash"]:
        raise ValueError("selection hash is inconsistent")
    if summary["selection_hash"] != frozen["selection_hash"]:
        raise ValueError("holdout selection hash differs from training freeze")
    plan = read(holdout / "plan.json")
    for path, expected in plan.get("evidence", {}).items():
        if sha(path) != expected:
            raise ValueError(f"recovery evidence changed: {path}")
    state = read(training / "training-final.json")
    dsl = ab.dsl_registry.mode_spec("dsl_v1")
    candidates = [e["expr"] for e in state["experiments"] if e["status"] == "completed"]
    candidates += [e for selected in frozen["selections"].values() for e in selected]
    policy_rows = []
    async with worker(output, abroot, atomic, "policy-audit") as tools:
        for expr in candidates:
            lint = await tools.call("assay_lint", expr=expr)
            if not lint.get("ast"):
                raise ValueError("MCP lint did not provide AST evidence")
            lookback, windows = ab.linting._lookback(lint["ast"])
            valid = (lint["diagnostics"]["ok"] and set(lint["fields"]) <= set(dsl["fields"])
                     and set(lint["operators"]) <= set(dsl["operators"])
                     and all(1 <= w <= dsl["max_window"] for w in windows)
                     and lookback <= dsl["max_cumulative_lookback"])
            policy_rows.append(dict(expr=expr, cumulative_lookback=lookback, windows=windows, valid=bool(valid)))
    atomic(output / "candidate-policy.json", policy_rows)
    if not all(r["valid"] for r in policy_rows):
        raise ValueError("candidate policy validation failed; original search requires investigation")
    manifest = read(holdout / "holdout-manifest.json")["episodes"][0]
    expected_days = manifest["oos_trading_dates"]
    checked = []
    for strategy, selected in frozen["selections"].items():
        rows = read(holdout / f"{strategy}-holdout-evaluation.json")["results"]
        if [r["expression"] for r in rows] != selected:
            raise ValueError("frozen expressions were changed or reordered")
        for row in rows:
            report = row["report"]
            pairs = [(day, value) for day, value in zip(report["dates"], report["rank_ic_series"])
                     if manifest["oos_start"] <= day <= manifest["oos_end"]]
            if [day for day, _ in pairs] != expected_days:
                raise ValueError("holdout date coverage mismatch")
            values = [float(value) for _, value in pairs]
            if not all(math.isfinite(v) for v in values) or not math.isclose(mean(values), row["oos_rank_ic"], abs_tol=1e-15):
                raise ValueError("stored OOS score does not equal the OOS-only series mean")
            for key, value in summary["frozen_axis"].items():
                if report[key] != value:
                    raise ValueError("holdout axis mismatch")
            if report["universe_as_of"] != manifest["decision_date"] or report["data_as_of"] != manifest["label_end"]:
                raise ValueError("holdout knowledge-time provenance mismatch")
            checked.append(dict(strategy=strategy, expr=row["expression"], dates=len(values), recomputed_oos_rank_ic=mean(values)))
    atomic(output / "oos-recalculation.json", checked)
    calls = [read(p) for p in sorted((training / "provider-calls").glob("*.json"))]
    if len({c["metadata"]["request_id"] for c in calls}) != 3:
        raise ValueError("provider request identities are incomplete or duplicated")
    usage = {key: sum(c["metadata"]["usage"][key] for c in calls)
             for key in ("prompt_tokens", "completion_tokens", "total_tokens")}
    if usage != summary["reported_usage_all_unique_requests"]:
        raise ValueError("provider usage summary mismatch")
    if any(c["metadata"]["transport_retry_count"] != 0 for c in calls):
        raise ValueError("unexpected automatic provider retry")
    source_checks = [read(root / "alphabench-source-after.json") for root in (training, holdout)]
    if not all(r["reused_sources_unchanged"] and r["git_status_unchanged"] for r in source_checks):
        raise ValueError("AlphaBench source or repository status changed")
    result = dict(status="passed", provider_calls=0, policy_cases=len(policy_rows),
        training_candidates=len([e for e in state["experiments"] if e["status"] == "completed"]),
        selected_candidates=len(checked), max_cumulative_lookback=max(r["cumulative_lookback"] for r in policy_rows),
        holdout_dates_per_factor=len(expected_days), usage=usage, alphabench_sources_unchanged=True,
        caveat="Artifact consistency check, not an independent scientific efficacy study",
        policy_enforced_before_execution=all(e.get("policy_check", {}).get("ok")
            for e in state["experiments"] if e["status"] == "completed"))
    session_path = holdout / "holdout-session.json"
    if session_path.exists():
        session = read(session_path)
        if session["dataset"].get("score_period"):
            for experiment in session["experiments"]:
                report = experiment["report"]
                if not report.get("scoring_complete") or report.get("dates") != expected_days:
                    raise ValueError("durable research headline is not scoped to the expected OOS dates")
                if report["rank_ic"] != mean(report["rank_ic_series"]):
                    raise ValueError("durable research headline differs from its OOS series")
                if "panel_report" not in experiment or report["turnover_1d"] is not None:
                    raise ValueError("panel evidence or unscoped metric separation is missing")
            result["durable_score_scopes_verified"] = True
    atomic(output / "checks.json", result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--training", type=Path, required=True)
    parser.add_argument("--holdout", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--alphabench-root", type=Path, default=ROOT.parent / "alphabench-v2")
    args = parser.parse_args()
    output, abroot = args.output.resolve(), args.alphabench_root.resolve()
    if not output.is_relative_to(ROOT) or output == ROOT or output.is_relative_to(abroot) or output.exists():
        parser.error("output must be a new directory inside Assay")
    output.mkdir(parents=True)
    sys.path.insert(0, str(abroot / "src"))
    names = ("provenance", "dsl_registry", "linting")
    ab = SimpleNamespace(**{n: importlib.import_module(f"alphabench_v2.{n}") for n in names})
    print(json.dumps(asyncio.run(audit(args.training.resolve(), args.holdout.resolve(), output, abroot, ab)), indent=2))


if __name__ == "__main__":
    main()
