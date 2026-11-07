"""Correct OOS warmup using already frozen smoke selections; no provider calls.

The original failed pilot is retained. This recovery changes only the data-read
and score-extraction protocol, never expressions, direction or strategy selection.
"""
from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import os
import subprocess
import sys
import traceback
from pathlib import Path
from types import SimpleNamespace

from smoke_alphabench_reuse import ROOT, evaluate_frozen_holdout, report_contract, sha, worker


async def recover(source, output, abroot, ab):
    atomic = ab.provenance.atomic_json
    frozen_path = source / "frozen-selections.json"
    frozen = json.loads(frozen_path.read_text())
    selections = frozen["selections"]
    if ab.provenance.payload_hash(selections) != frozen["selection_hash"]:
        raise ValueError("frozen selections hash mismatch")
    original = json.loads((source / "plan.json").read_text())
    preflight = json.loads((source / "preflight.json").read_text())
    axis = preflight["frozen_axis"]
    references = [frozen_path, source / "training-final.json", source / "handoff-verification.json",
                  source / "mcts-trace.json", source / "failure.json"]
    provider_files = sorted((source / "provider-calls").glob("*.json"))
    references += provider_files
    external_paths = [p for p in (abroot / "src/alphabench_v2").glob("*.py")]
    external_paths += list((abroot / "configs").glob("*.json"))
    external_hashes = {str(p): sha(p) for p in external_paths}
    status_before = subprocess.check_output(["git", "-C", str(abroot), "status", "--porcelain=v1"], text=True)
    plan = dict(kind="assay_holdout_warmup_recovery", formal=False, provider_calls=0,
        source_run=str(source), evidence={str(p): sha(p) for p in references},
        selection_hash=frozen["selection_hash"], selections=selections, frozen_axis=axis,
        protocol_correction="180 calendar days non-scoring warmup plus label bars through August 2; score July dates only",
        no_selection_change=True, no_provider_feedback=True, original_failure_retained=True,
        external_source_hashes=external_hashes,
        local_source_hashes={str(ROOT / "scripts" / name): sha(ROOT / "scripts" / name) for name in
            ("recover_alphabench_holdout.py", "smoke_alphabench_reuse.py", "research_mcp_worker.py")})
    atomic(output / "plan.json", plan)
    atomic(output / "alphabench-source-before.json", dict(source_hashes=external_hashes, git_status=status_before))
    try:
        client = ab.assay.AssayClient("http://127.0.0.1:8002", os.environ.get("ASSAY_API_KEY"))
        provenance = await asyncio.to_thread(client.provenance)
        if provenance["health"].get("status") != "ok":
            raise ValueError("Assay health preflight failed")
        spec = ab.assay.EvaluationSpec(**{**original["train"], "period": tuple(original["train"]["period"]),
                                        "horizons": tuple(original["train"]["horizons"])})
        calibration = await asyncio.to_thread(client.evaluate, "cs_rank(ts_returns(close,5))", spec)
        report_contract(calibration, spec, axis)
        atomic(output / "preflight.json", dict(provenance=provenance, calibration=calibration))
        async with worker(output, abroot, atomic, "holdout-recovery") as tools:
            holdout = await evaluate_frozen_holdout(tools, output, ab, selections, axis, provenance)
        handoff = json.loads((source / "handoff-verification.json").read_text())
        training = json.loads((source / "training-final.json").read_text())
        trace = json.loads((source / "mcts-trace.json").read_text())
        calls = [json.loads(p.read_text()) for p in provider_files]
        usage = {key: sum((c["metadata"].get("usage") or {}).get(key, 0) for c in calls)
                 for key in ("prompt_tokens", "completion_tokens", "total_tokens")}
        summary = dict(status="completed", classification="engineering_smoke_after_warmup_correction", formal=False,
            training_run=str(source), corrected_holdout_run=str(output),
            training_attempts=len(training["experiments"]),
            successful_training_evaluations=sum(e["status"] == "completed" for e in training["experiments"]),
            diagnostic_failures=sum(e["status"] == "failed" for e in training["experiments"]),
            restart_and_idempotency_verified=handoff["identical_state"] and handoff["replay_same_experiment"],
            mcts_provider_phases=[c["phase"] for c in trace["calls"]],
            requested_model=original["model"]["model_id"], returned_models=sorted({c["metadata"]["returned_model"] for c in calls}),
            unique_provider_requests=len({c["metadata"]["request_id"] for c in calls}),
            recovery_provider_requests=0, reported_usage_all_unique_requests=usage,
            frozen_axis=axis, holdout_candidates=len(holdout),
            holdout_complete=sum(r["coverage_complete"] for r in holdout),
            holdout_date_counts=sorted({r["oos_rank_ic_n_dates"] for r in holdout}),
            selection_hash=frozen["selection_hash"], selections_unchanged=True,
            empirical_conclusion="integration evidence only; no framework superiority or profitability inference",
            caveats=original["caveats"])
        atomic(output / "summary.json", summary)
        return summary
    finally:
        after = {str(p): sha(p) for p in external_paths}
        status_after = subprocess.check_output(["git", "-C", str(abroot), "status", "--porcelain=v1"], text=True)
        atomic(output / "alphabench-source-after.json", dict(source_hashes=after, git_status=status_after,
            reused_sources_unchanged=external_hashes == after, git_status_unchanged=status_before == status_after))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--alphabench-root", type=Path, default=ROOT.parent / "alphabench-v2")
    args = parser.parse_args()
    source, output, abroot = args.source.resolve(), args.output.resolve(), args.alphabench_root.resolve()
    if not output.is_relative_to(ROOT) or output == ROOT or output.is_relative_to(abroot) or output.exists():
        parser.error("output must be a new directory beneath Assay")
    if not source.is_relative_to(ROOT):
        parser.error("source must be an Assay development run")
    output.mkdir(parents=True)
    sys.path.insert(0, str(abroot / "src"))
    names = ("assay", "provenance", "cli", "linting", "evaluate_generated", "dsl_registry")
    ab = SimpleNamespace(**{name: importlib.import_module(f"alphabench_v2.{name}") for name in names})
    ab.cli.load_env_file(abroot / ".env")
    try:
        summary = asyncio.run(recover(source, output, abroot, ab))
    except BaseException as exc:
        ab.provenance.atomic_json(output / "failure.json", dict(type=type(exc).__name__,
            message=str(exc), traceback="".join(traceback.format_exception(exc))))
        raise
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
