"""Build compact handoff context using stored evidence, without an LLM call."""
from __future__ import annotations

from collections import Counter
import hashlib
import json
import math


def build_context(state: dict, backend: dict, limit: int) -> dict:
    from assay.engine import parse

    groups = {}
    failures = Counter()
    views = []
    for attempt in state["experiments"]:
        report = attempt.get("report") or {}
        expr = attempt["expr"]
        try:
            canonical = str(parse(expr))
        except (ValueError, TypeError):
            canonical = expr.strip()
        groups.setdefault(canonical, []).append(attempt["id"])
        reason = report.get("failure_mode") or (attempt.get("error") or {}).get("type")
        if attempt["status"] in {"failed", "abandoned"}:
            failures[reason or attempt["status"]] += 1
        views.append(dict(id=attempt["id"], expr=expr, hypothesis=attempt["hypothesis"],
                          status=attempt["status"], agent=attempt["agent"],
                          parent_id=attempt.get("parent_id"), replay_of=attempt.get("replay_of"),
                          failure_mode=reason, suggestion=report.get("suggestion"),
                          metrics={k: report.get(k) for k in ("rank_ic", "rank_icir", "turnover_1d")}))

    def finite(value):
        return type(value) in (int, float) and math.isfinite(value)

    ranked = sorted((v for v in views if v["status"] == "completed" and finite(v["metrics"]["rank_ic"])),
                    key=lambda v: (-abs(v["metrics"]["rank_ic"]), v["id"]))
    running = [v for v in views if v["status"] == "running"]
    failed = [v for v in views if v["status"] in {"failed", "abandoned"}]
    repeated = [dict(canonical=expr, count=len(ids), experiment_ids=ids[-limit:])
                for expr, ids in groups.items() if len(ids) > 1]
    repeated.sort(key=lambda r: (-r["count"], r["canonical"]))
    actions = []
    if not backend["registered"]:
        actions.append("Register this dataset's backend in the serving process before evaluating.")
    if running:
        actions.append("Inspect running attempts; abandon only after confirming the original worker stopped.")
    if state["remaining_experiments"] == 0:
        actions.append("Attempt budget is exhausted; review evidence or create a new research session.")
    else:
        actions.append("Review prior failures and normalized expression repeats before proposing another candidate.")
    return dict(session_id=state["id"], goal=state["goal"], dataset=state["dataset"], backend=backend,
                state_revision=hashlib.sha256(json.dumps(state, sort_keys=True, allow_nan=False).encode()).hexdigest(),
                remaining_experiments=state["remaining_experiments"],
                counts=dict(attempts=len(views), statuses=dict(Counter(v["status"] for v in views)),
                            unique_canonical_expressions=len(groups), repeated_expression_groups=len(repeated)),
                recent_attempts=views[-limit:], top_candidates=ranked[:limit], ranked_by="absolute_rank_ic",
                failed_attempts=failed[-limit:], failure_counts=dict(failures), running_attempts=running[-limit:],
                repeated_expressions=repeated[:limit], notes=state["notes"][-limit:],
                truncated=dict(recent_attempts=len(views) > limit, top_candidates=len(ranked) > limit,
                               failed_attempts=len(failed) > limit, running_attempts=len(running) > limit,
                               repeated_expressions=len(repeated) > limit, notes=len(state["notes"]) > limit),
                next_actions=actions,
                caveat="Normalized expression repeats are syntactic, not measured factor correlation. Ranking is descriptive, not statistical validation.")
