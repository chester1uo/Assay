"""Bounded live integration smoke; all artifacts belong to Assay, not AlphaBench.

Reuses AlphaBench's provider, privacy/DSL contract, orientation, REST client and
native MCTS search. Two strategies share durable state over real stdio MCP.
This is development evidence, never a formal benchmark or efficacy comparison.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib
import json
import math
import os
import subprocess
import sys
import traceback
from contextlib import asynccontextmanager
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class MCPTools:
    def __init__(self, session, output, atomic_json):
        self.session, self.output, self.atomic_json = session, output, atomic_json
        self.ledger = []
        self.loop = asyncio.get_running_loop()

    async def call(self, tool_name, **arguments):
        row = dict(index=len(self.ledger), tool=tool_name, arguments=arguments, status="started")
        self.ledger.append(row)
        self.atomic_json(self.output, self.ledger)
        try:
            result = await self.session.call_tool(tool_name, arguments)
            if result.isError:
                raise RuntimeError("; ".join(c.text for c in result.content if c.type == "text"))
            value = result.structuredContent
            if value is None:
                value = json.loads(next(c.text for c in result.content if c.type == "text"))
            row.update(status="completed", result=value)
            return value
        except Exception as exc:
            row.update(status="failed", error=f"{type(exc).__name__}: {exc}")
            raise
        finally:
            self.atomic_json(self.output, self.ledger)

    def sync(self, tool_name, **arguments):
        return asyncio.run_coroutine_threadsafe(self.call(tool_name, **arguments), self.loop).result(timeout=240)


class ResearchClient:
    """Duck-typed AlphaBench Assay client that routes scoring through research MCP."""
    def __init__(self, tools, session_id, spec, prefix, *, return_panel_report=False):
        self.tools, self.session_id, self.spec, self.prefix = tools, session_id, spec, prefix
        self.attempts = []
        self.return_panel_report = return_panel_report

    def lint(self, expression):
        return self.tools.sync("assay_lint", expr=expression)

    def evaluate(self, expression, spec):
        if spec.as_dict() != self.spec.as_dict():
            raise ValueError("search attempted to change the frozen training specification")
        result = self.tools.sync(
            "assay_experiment_run", session_id=self.session_id, expr=expression,
            hypothesis=f"Candidate proposed by {self.prefix}; native output has no separate economic rationale",
            request_id=f"{self.prefix}-{len(self.attempts)}", agent=self.prefix,
        )
        self.attempts.append(result)
        if result["status"] != "completed":
            raise RuntimeError(f"experiment did not complete: {result['id']}")
        return result["panel_report"] if self.return_panel_report else result["report"]


class RecordedProvider:
    def __init__(self, client, output, ab, profile, allowed_models):
        self.client, self.output, self.ab, self.profile = client, output, ab, profile
        self.allowed_models = allowed_models
        self.calls = []
        self.supports_prompt_prefix_cache = False

    def replay(self, path, prompt, **kwargs):
        """Reuse an observed response explicitly; never bill a replacement request."""
        source = json.loads(path.read_text())
        if source.get("prompt_hash") != self.ab.provenance.payload_hash(prompt) or source.get("decoding") != kwargs:
            raise ValueError("replay source has different prompt or decoding")
        if source.get("metadata", {}).get("returned_model") not in self.allowed_models:
            raise ValueError("replay source has an unaccepted returned model identity")
        if source.get("status") not in {"response_received", "response_replayed"} and source.get("error") != "ValueError: provider returned an unregistered model identity":
            raise ValueError("replay source was not a complete provider response")
        if not isinstance(source.get("raw_response"), str):
            raise ValueError("replay source has no raw response")
        self.ab.privacy.validate_blind_prompt(prompt, profile=self.profile)
        row = dict(source, index=len(self.calls), status="response_replayed", replayed=True,
                   source_path=str(path), source_sha256=sha(path), source_status=source.get("status"))
        row.pop("error", None)
        self.calls.append(row)
        self.ab.provenance.atomic_json(self.output / f"{row['index']:02d}.json", row)
        return row["raw_response"], row["metadata"]

    def complete(self, prompt, **kwargs):
        if len(self.calls) >= 3:
            raise RuntimeError("live smoke hard limit: three provider requests")
        self.ab.privacy.validate_blind_prompt(prompt, profile=self.profile)
        row = dict(index=len(self.calls), prompt=prompt, prompt_hash=self.ab.provenance.payload_hash(prompt),
                   decoding=kwargs, status="started")
        self.calls.append(row)
        path = self.output / f"{row['index']:02d}.json"
        self.ab.provenance.atomic_json(path, row)
        try:
            raw, metadata = self.client.complete(prompt, **kwargs)
            row.update(status="response_received", raw_response=raw, metadata=metadata)
            self.ab.provenance.atomic_json(path, row)
            if metadata.get("returned_model") not in self.allowed_models:
                raise ValueError("provider returned an unregistered model identity")
            return raw, metadata
        except Exception as exc:
            row.update(status="failed", error=f"{type(exc).__name__}: {exc}")
            self.ab.provenance.atomic_json(path, row)
            raise


class ReplayProvider(RecordedProvider):
    """Strict ordered response replay. There is no live-provider fallback."""

    def __init__(self, source, output, ab, profile, allowed_models):
        super().__init__(None, output, ab, profile, allowed_models)
        self.sources = sorted((source / "provider-calls").glob("*.json"))
        if len(self.sources) != 3:
            raise ValueError("a complete replay requires exactly three recorded responses")
        self.source_hashes = {str(p): sha(p) for p in self.sources}

    def complete(self, prompt, **kwargs):
        index = len(self.calls)
        if index >= len(self.sources):
            raise RuntimeError("replay exhausted; no live provider call is permitted")
        path = self.sources[index]
        if sha(path) != self.source_hashes[str(path)]:
            raise ValueError("replay source changed after plan freeze")
        return self.replay(path, prompt, **kwargs)


@asynccontextmanager
async def worker(output, abroot, atomic_json, phase):
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(ROOT / "src"), str(abroot / "src")]),
               PYTHONDONTWRITEBYTECODE="1")
    params = StdioServerParameters(command=sys.executable,
        args=[str(ROOT / "scripts/research_mcp_worker.py"), "--workspace", str(output / "workspace")],
        env=env, cwd=str(ROOT))
    with (output / "mcp-worker.log").open("a") as log:
        async with stdio_client(params, errlog=log) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                available = await session.list_tools()
                atomic_json(output / f"mcp-tools-{phase}.json", [t.model_dump(mode="json") for t in available.tools])
                yield MCPTools(session, output / f"mcp-ledger-{phase}.json", atomic_json)


def report_contract(report, spec, axis=None):
    if report.get("failure_mode") or not math.isfinite(float(report.get("rank_ic"))):
        raise ValueError("preflight report has no valid training score")
    expected = dict(universe_id=spec.universe, eval_period=list(spec.period),
                    universe_as_of=spec.universe_as_of, data_as_of=spec.period[1])
    if axis:
        expected.update(axis)
    for key, value in expected.items():
        if report.get(key) != value:
            raise ValueError(f"preflight contract mismatch: {key}")
    if not report.get("universe_symbols_hash") or not report.get("n_symbols"):
        raise ValueError("preflight report lacks the frozen symbol axis")


async def evaluate_frozen_holdout(tools, output, ab, selections, axis, provenance):
    """Reuse strict AlphaBench OOS scoring, with non-scoring warmup and label bars."""
    atomic = ab.provenance.atomic_json
    score_start, score_end, label_end = "2024-07-01", "2024-07-31", "2024-08-02"
    warmup_days = 180
    warmup_start = str(date.fromisoformat(score_start) - timedelta(days=warmup_days))
    spec = ab.assay.EvaluationSpec("NASDAQ100", (warmup_start, label_end), label_end, (1,),
                                  universe_as_of="2024-06-28")
    days = sorted({row["date"] for row in provenance["data_calendar"]
                   if row["market"] == "US" and score_start <= row["date"] <= score_end})
    if not days:
        raise ValueError("holdout calendar is empty")
    episode_id = "assay-reuse-us-holdout"
    episode = dict(episode_id=episode_id, universe=spec.universe, decision_date=spec.universe_as_of,
        oos_start=score_start, oos_end=score_end, label_end=label_end, oos_trading_dates=days,
        horizon_trading_days=len(days), universe_snapshot=dict(symbol_count=axis["n_symbols"],
                                                              symbols_hash=axis["universe_symbols_hash"]))
    manifest = output / "holdout-manifest.json"
    atomic(manifest, dict(episodes=[episode], scoring_policy="only OOS dates; warmup and label bars excluded"))
    dataset = await tools.call("assay_dataset_register", name="holdout-warmup-panel", universe=spec.universe,
        period=list(spec.period), as_of=spec.as_of, horizons=[1], backend="alphabench",
        backend_options=dict(endpoint="http://127.0.0.1:8002", universe_as_of=spec.universe_as_of, **axis),
        score_period=[score_start, score_end], score_dates=days,
        expression_policy={k: ab.dsl_registry.mode_spec("dsl_v1")[k] for k in
                           ("fields", "operators", "max_window", "max_cumulative_lookback")})
    session = await tools.call("assay_research_session_create", dataset_id=dataset["id"],
        goal="Score frozen expressions on OOS dates only; panel evidence is retained separately",
        max_experiments=10, agent="holdout-evaluator")
    results = []
    for strategy, selected in selections.items():
        generation = output / f"{strategy}-holdout-generation.json"
        lint = output / f"{strategy}-holdout-lint.json"
        evaluation = output / f"{strategy}-holdout-evaluation.json"
        atomic(generation, dict(episode_id=episode_id, condition="no_context", expressions=selected, dsl_mode="dsl_v1"))
        proxy = ResearchClient(tools, session["id"], spec, f"holdout-{strategy}", return_panel_report=True)
        await asyncio.to_thread(ab.linting.lint_generation, generation, lint, proxy)
        await asyncio.to_thread(ab.evaluate_generated.evaluate_generation, generation_path=generation,
            lint_path=lint, manifest_path=manifest, output_path=evaluation, client=proxy,
            warmup_calendar_days=warmup_days, data_policy="strict_audit")
        evaluated = json.loads(evaluation.read_text())["results"]
        if len(evaluated) != 5 or len(proxy.attempts) != 5:
            raise ValueError("holdout did not retain all five frozen candidates")
        for row, attempt in zip(evaluated, proxy.attempts):
            valid = bool(row.get("coverage_complete")) and row.get("oos_rank_ic_n_dates") == len(days)
            valid = valid and row.get("oos_rank_ic") is not None and math.isfinite(float(row["oos_rank_ic"]))
            scoped = attempt["report"]
            valid = valid and scoped.get("scoring_complete") and scoped.get("dates") == days
            valid = valid and math.isclose(scoped["rank_ic"], row["oos_rank_ic"], abs_tol=1e-15)
            results.append(dict(strategy=strategy, attempt=attempt, coverage_complete=valid,
                oos_rank_ic=row.get("oos_rank_ic"), oos_rank_ic_n_dates=row.get("oos_rank_ic_n_dates")))
        atomic(output / "holdout.json", results)
    atomic(output / "holdout-session.json", await tools.call("assay_research_session_get", session_id=session["id"]))
    if not all(r["coverage_complete"] for r in results):
        raise ValueError("strict holdout coverage incomplete; inspect retained rows")
    return results


async def run(output, abroot, ab, model, accepted_models=None, replay_direct=None, replay_run=None):
    atomic = ab.provenance.atomic_json
    adapter, registry_hash = ab.model_registry.model_adapter(model)
    accepted_models = sorted(set(adapter["allowed_returned_models"] + (accepted_models or [])))
    profile, profile_hash = ab.privacy.load_privacy_profile()
    replay_provider = ReplayProvider(replay_run, output / "provider-calls", ab, profile, accepted_models) if replay_run else None
    # No source snapshot, company, ticker, calendar date, news or fundamental
    # record is exposed to the provider. Only DSL and aggregate training feedback.
    snapshot = dict(cutoff_utc="2024-06-28T20:00:00Z", modalities={}, provenance={"market": "US"})
    prompt = ab.generation.build_prompt(snapshot, k=5, privacy_profile=profile, dsl_mode="dsl_v1", market="US")
    train = ab.assay.EvaluationSpec("NASDAQ100", ("2024-01-02", "2024-06-28"), "2024-06-28", (1,),
                                    universe_as_of="2024-06-28")
    test = ab.assay.EvaluationSpec("NASDAQ100", ("2024-07-01", "2024-07-31"), "2024-07-31", (1,),
                                   universe_as_of="2024-06-28")
    client = ab.assay.AssayClient("http://127.0.0.1:8002", os.environ.get("ASSAY_API_KEY"))
    config = ab.mcts_participant.MCTSFormalConfig(num_trees=1, iterations=1, dsl_mode="dsl_v1")
    source_paths = [abroot / "src/alphabench_v2" / f"{name}.py" for name in
        ("assay", "generation", "privacy", "dsl_registry", "mcts_participant", "factor_orientation", "provenance", "http", "model_registry", "provider_route", "evaluate_generated", "linting", "data_policy", "release")]
    source_paths += [ROOT / "scripts/smoke_alphabench_reuse.py", ROOT / "scripts/research_mcp_worker.py",
                     ROOT / "src/assay/research.py", ROOT / "src/assay/mcp/research.py",
                     ROOT / "src/assay/integrations/alphabench.py"]
    source_paths += [ROOT / "src/assay" / f"{name}.py" for name in ("research_policy", "research_scoring")]
    source_paths += [abroot / "configs" / name for name in
                     ("dsl_registry_v1.json", "privacy_profiles_v1.json", "model_adapters_v1.json")]
    source_hashes = {str(p): sha(p) for p in source_paths}
    source_status = subprocess.check_output(["git", "-C", str(abroot), "status", "--porcelain=v1"], text=True)
    atomic(output / "alphabench-source-before.json", dict(source_hashes=source_hashes, git_status=source_status))
    plan = dict(kind="assay_alphabench_reuse_development_smoke", formal=False,
        purpose="MCP and durable-state integration; no framework efficacy estimate",
        strategies=["direct_generation", "native_mcts_one_expansion"], model=adapter,
        mode="recorded_provider_replay" if replay_run else "live_provider",
        replay_sources=replay_provider.source_hashes if replay_provider else None,
        accepted_returned_models=accepted_models,
        identity_policy="record actual returned identity; accepted aliases do not prove model equivalence",
        replay_direct=({"path": str(replay_direct), "sha256": sha(replay_direct)} if replay_direct else None),
        model_registry_hash=registry_hash, decoding=dict(temperature=0.7, max_tokens=8192, seed=17,
        transport_retries=0, request_timeout_seconds=120), mcts=config.as_dict(), provider_request_limit=3,
        train=train.as_dict(), holdout=test.as_dict(),
        holdout_warmup_calendar_days=180, holdout_label_end="2024-08-02",
        holdout_policy="freeze selections first; score July dates only, excluding warmup and label buffers",
        privacy=dict(profile=profile, profile_hash=profile_hash, market_context="none",
                     feedback="expression and aggregate training RankIC only"),
        base_prompt_hash=ab.provenance.payload_hash(prompt), source_hashes=source_hashes,
        output=str(output), caveats=["unequal strategy budgets; not an algorithm comparison",
            "one historical US episode; no statistical or profit conclusion",
            "frozen axis/config; market data contents are not immutable snapshots"])
    atomic(output / "plan.json", plan)
    atomic(output / "base-prompt.json", {"prompt": prompt})
    provenance = await asyncio.to_thread(client.provenance)
    if provenance["health"].get("status") != "ok" or not provenance["data_calendar"]:
        raise ValueError("Assay health/status/calendar preflight failed")
    calibration = await asyncio.to_thread(client.evaluate, "cs_rank(ts_returns(close,5))", train)
    report_contract(calibration, train)
    axis = {key: calibration[key] for key in ("n_symbols", "universe_symbols_hash")}
    atomic(output / "preflight.json", dict(provenance=provenance, calibration=calibration, frozen_axis=axis))
    if not replay_run and os.environ.get("ABV2_PROVIDER_ROUTE"):
        raise ValueError("explicit provider route override is incompatible with this frozen smoke plan")
    if replay_provider is None:
        provider = ab.generation.DeepSeekChatClient(
            api_key=os.environ.get(adapter["api_key_env"]), base_url=adapter["base_url"],
            provider=adapter["provider"], protocol=adapter["protocol"],
        )
        provider.transport_retries = 0
        provider.request_timeout = 120
        recorded = RecordedProvider(provider, output / "provider-calls", ab, profile, accepted_models)
    else:
        recorded = replay_provider
    options = dict(endpoint=client.base_url, universe_as_of=train.universe_as_of, **axis)
    atomic(output / "progress.json", {"stage": "preflight_passed"})
    try:
        async with worker(output, abroot, atomic, "initial") as tools:
            dataset = await tools.call("assay_dataset_register", name="training-us-pilot", universe=train.universe,
                period=list(train.period), as_of=train.as_of, horizons=[1], backend="alphabench", backend_options=options,
                expression_policy={k: ab.dsl_registry.mode_spec("dsl_v1")[k] for k in
                                   ("fields", "operators", "max_window", "max_cumulative_lookback")})
            session = await tools.call("assay_research_session_create", dataset_id=dataset["id"],
                goal="Discover diverse daily OHLCV factors using training-only feedback", agent="direct-generation",
                max_experiments=20)
            sid = session["id"]
            completion = recorded.complete if replay_direct is None else lambda prompt, **kw: recorded.replay(replay_direct, prompt, **kw)
            raw, metadata = await asyncio.to_thread(completion, prompt, model=model, seed=17,
                                                    temperature=0.7, max_tokens=8192)
            expressions = ab.generation.parse_expressions(raw, k=5)
            direct_client = ResearchClient(tools, sid, train, "direct-generation")
            candidates = []
            for expr in expressions:
                lint = await asyncio.to_thread(direct_client.lint, expr)
                if not lint.get("diagnostics", {}).get("ok"):
                    raise ValueError("direct candidate failed lint; no automatic correction authorized")
                report = await asyncio.to_thread(direct_client.evaluate, expr, train)
                candidates.append(dict(expression=expr, rank_ic=report["rank_ic"]))
            directed = [ab.factor_orientation.orient_factor(r["expression"], r["rank_ic"]) for r in candidates]
            direct_selection = [expr for expr, _ in directed]
            if len(set(direct_selection)) != 5:
                raise ValueError("direct selection collapsed after training-sign orientation")
            atomic(output / "direct-selection.json", dict(expressions=direct_selection,
                orientation=[record for _, record in directed], training_attempts=direct_client.attempts))
            await tools.call("assay_research_note", session_id=sid, agent="direct-generation",
                text="Five direct candidates evaluated on training only. Continue with MCTS using training feedback; holdout is unavailable.")
            state = await tools.call("assay_research_session_get", session_id=sid)
            atomic(output / "handoff-before.json", state)
            retry_args = dict(
                session_id=sid, expr=expressions[0],
                hypothesis="Candidate proposed by direct-generation; native output has no separate economic rationale",
                request_id="direct-generation-0", agent="direct-generation")

        # A fresh subprocess must discover state without inheriting Python objects.
        async with worker(output, abroot, atomic, "resumed") as tools:
            await tools.call("assay_research_session_list")
            restored = await tools.call("assay_research_session_get", session_id=sid)
            replay = await tools.call("assay_experiment_run", **retry_args)
            after_retry = await tools.call("assay_research_session_get", session_id=sid)
            if restored != state or after_retry != restored or replay != direct_client.attempts[0]:
                raise ValueError("restart or request-id replay changed durable research state")
            atomic(output / "handoff-verification.json", dict(identical_state=True,
                replay_same_experiment=True, restored_attempts=len(restored["experiments"]),
                remaining_budget=restored["remaining_experiments"]))
            # Native MCTS receives only predeclared DSL and its own training feedback.
            search_client = ResearchClient(tools, sid, train, "native-mcts")
            search = ab.mcts_participant.MCTSCellSearch(provider=recorded, assay=search_client, config=config, seed=17)
            tool_spec = dict(universe=train.universe, period_start=train.period[0], period_end=train.period[1],
                as_of=train.as_of, universe_as_of=train.universe_as_of, execution=train.execution,
                adj=train.adj, orient_training_sign=True)
            try:
                mcts_raw, mcts_metadata = await asyncio.to_thread(search.run, prompt=prompt, model=model, seed=17,
                    temperature=0.7, max_tokens=8192, tool_spec=tool_spec)
            finally:
                atomic(output / "mcts-trace.json", dict(calls=search.calls, evaluations=search.evaluations,
                    duplicate_events=search.duplicate_batch_events, repair_events=search.repair_events))
            mcts_selection = list(ab.generation.parse_expressions(mcts_raw, k=5))
            selections = {"direct_generation": direct_selection, "native_mcts": mcts_selection}
            # This selection artifact is committed before any holdout tool call.
            atomic(output / "frozen-selections.json", dict(selections=selections,
                selection_hash=ab.provenance.payload_hash(selections), mcts_metadata=mcts_metadata))
            holdout = await evaluate_frozen_holdout(tools, output, ab, selections, axis, provenance)
            # Exercise and retain a diagnostic failure after all search is frozen.
            failure = await tools.call("assay_experiment_run", session_id=sid,
                expr="unknown_operator(close)", hypothesis="Diagnostic retention probe, excluded from selection",
                request_id="diagnostic-probe", agent="integration-test")
            if failure["status"] != "failed" or not (failure.get("report") or {}).get("diagnostics"):
                raise ValueError("diagnostic failure was not retained")
            training_state = await tools.call("assay_research_session_get", session_id=sid)
            atomic(output / "training-final.json", training_state)
            comparison = await tools.call("assay_experiment_compare", experiment_ids=[
                direct_client.attempts[0]["id"], failure["id"]])
            atomic(output / "comparison.json", comparison)
        def usage_total(key, replayed=False):
            return sum((c.get("metadata", {}).get("usage") or {}).get(key, 0) or 0
                       for c in recorded.calls if bool(c.get("replayed")) == replayed)
        summary = dict(status="completed", kind="engineering_integration_smoke", formal=False,
            mode=plan["mode"],
            requested_model=model, returned_models=sorted({c["metadata"]["returned_model"] for c in recorded.calls}),
            provider_requests=sum(not c.get("replayed", False) for c in recorded.calls),
            reused_responses=sum(bool(c.get("replayed")) for c in recorded.calls), frozen_axis=axis,
            training_session_id=sid, training_attempts=len(training_state["experiments"]),
            restart_and_idempotency_verified=True, retained_diagnostic_failure=True,
            native_mcts_refinement_calls=sum(c["phase"].startswith("refinement") for c in search.calls),
            holdout_attempts=len(holdout), holdout_completed=sum(r["attempt"]["status"] == "completed" for r in holdout),
            usage={k: usage_total(k) for k in ("prompt_tokens", "completion_tokens", "total_tokens")},
            reused_response_usage={k: usage_total(k, replayed=True) for k in ("prompt_tokens", "completion_tokens", "total_tokens")},
            reported_usage_all_unique_requests={k: usage_total(k) + usage_total(k, replayed=True)
                for k in ("prompt_tokens", "completion_tokens", "total_tokens")},
            selection_hash=ab.provenance.payload_hash(selections),
            holdout_date_counts=sorted({r["oos_rank_ic_n_dates"] for r in holdout}),
            policy_enforced_before_execution=all(e.get("policy_check", {}).get("ok")
                for e in training_state["experiments"] if e["status"] == "completed"),
            score_scopes_enforced=all(r["attempt"]["report"].get("scoring_complete") for r in holdout),
            caveats=plan["caveats"], output=str(output))
        if summary["holdout_completed"] != 10 or summary["native_mcts_refinement_calls"] != 1:
            raise ValueError("smoke completed with missing expected evidence")
        atomic(output / "summary.json", summary)
        return summary
    finally:
        after = {str(p): sha(p) for p in source_paths}
        after_status = subprocess.check_output(["git", "-C", str(abroot), "status", "--porcelain=v1"], text=True)
        atomic(output / "alphabench-source-after.json", dict(source_hashes=after, git_status=after_status,
            reused_sources_unchanged=after == source_hashes, git_status_unchanged=source_status == after_status))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--alphabench-root", type=Path, default=ROOT.parent / "alphabench-v2")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", default="deepseek-v4-flash")
    parser.add_argument("--accept-returned-model", action="append", default=[], help="Local Assay identity policy; does not modify AlphaBench")
    replay = parser.add_mutually_exclusive_group()
    replay.add_argument("--replay-direct", type=Path, help="Reuse a complete recorded direct response with identical prompt and decoding")
    replay.add_argument("--replay-run", type=Path, help="Replay all three responses with zero new model calls; never falls back to live")
    args = parser.parse_args()
    output, abroot = args.output.resolve(), args.alphabench_root.resolve()
    if not output.is_relative_to(ROOT) or output == ROOT or output.is_relative_to(abroot):
        parser.error("output must be a new directory inside the Assay repository")
    if output.exists():
        parser.error("output already exists; provider attempts are never overwritten")
    output.mkdir(parents=True)
    sys.path.insert(0, str(abroot / "src"))
    from types import SimpleNamespace
    names = ("assay", "generation", "privacy", "provenance", "model_registry", "mcts_participant",
             "factor_orientation", "cli", "linting", "evaluate_generated", "dsl_registry")
    ab = SimpleNamespace(**{name: importlib.import_module(f"alphabench_v2.{name}") for name in names})
    ab.cli.load_env_file(abroot / ".env")
    try:
        summary = asyncio.run(run(output, abroot, ab, args.model, args.accept_returned_model,
                                  args.replay_direct.resolve() if args.replay_direct else None,
                                  args.replay_run.resolve() if args.replay_run else None))
    except BaseException as exc:
        ab.provenance.atomic_json(output / "failure.json", dict(status="failed", type=type(exc).__name__,
            message=str(exc), traceback="".join(traceback.format_exception(exc))))
        print(json.dumps({"status": "failed", "type": type(exc).__name__, "output": str(output)}))
        raise
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
