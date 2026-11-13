"""Research-lifecycle MCP tools. The external agent owns its planning strategy."""
from __future__ import annotations


def register_research_tools(mcp, get_service):
    from mcp.types import ToolAnnotations

    @mcp.tool()
    def assay_checkpoint_save(session_id: str, name: str, algorithm: str, state: dict,
                              request_id: str, expected_revision: int,
                              expected_state_revision: str | None = None) -> dict:
        """Save JSON search state under a versioned name without spending evaluation budget.

        Use expected_revision=0 for a new stream; later writes compare the last
        revision. Reuse the same request_id and inputs after a timeout. The state
        can contain a tree, population and random-generator state, but must not
        include API keys. Payloads are limited to 1 MiB. Attempt IDs and statuses
        are captured so a later reader can detect intervening research changes.
        Set expected_state_revision from research_context for atomic state checking.
        """
        return get_service().research.save_checkpoint(session_id, name, algorithm, state,
            request_id=request_id, expected_revision=expected_revision, expected_state_revision=expected_state_revision)

    @mcp.tool()
    def assay_checkpoint_get(session_id: str, name: str, revision: int | None = None) -> dict:
        """Get a checkpoint and report research-state changes since it was saved.

        A stale checkpoint may need reconciliation before the client resumes.
        Its JSON is caller-supplied algorithm state, not executable code.
        """
        return get_service().research.get_checkpoint(session_id, name, revision=revision)

    @mcp.tool()
    def assay_checkpoint_list(session_id: str) -> dict:
        """List the latest metadata for each checkpoint stream without returning state payloads."""
        return {'checkpoints': get_service().research.list_checkpoints(session_id)}

    @mcp.tool()
    def assay_program_create(name: str, goal: str, request_id: str) -> dict:
        """Create an idempotent research program for linked sessions across data windows."""
        return get_service().research.create_program(name, goal, request_id=request_id)

    @mcp.tool()
    def assay_program_attach_session(program_id: str, session_id: str, label: str = '') -> dict:
        """Link an existing session and its fixed dataset to a program; identical retries are safe."""
        return get_service().research.attach_program_session(program_id, session_id, label=label)

    @mcp.tool()
    def assay_program_get(program_id: str) -> dict:
        """Inspect a program and its linked sessions and dataset specifications."""
        return get_service().research.get_program(program_id)

    @mcp.tool()
    def assay_program_list() -> dict:
        """Discover durable research programs in the current workspace."""
        return {'programs': get_service().research.list_programs()}

    @mcp.tool()
    def assay_program_export(program_id: str) -> dict:
        """Export a consistent program and its sessions, checkpoints, and reassessment links.

        Market data and backend code are separate; the hash does not authenticate authorship.
        """
        return get_service().research.export_program(program_id)

    @mcp.tool()
    def assay_program_import(manifest: dict) -> dict:
        """Atomically restore a program manifest. Identical retries are idempotent.

        All attempts must be terminal. Existing conflicting identities are rejected.
        """
        return get_service().research.import_program(manifest)

    @mcp.tool()
    def assay_program_factor_history(program_id: str, factor_id: str, offset: int = 0, limit: int = 20) -> dict:
        """Inspect a canonical factor's recorded attempts across a program's windows.

        Settings and exact windows are returned per attempt. Different windows
        do not establish point-in-time validity or statistical factor decay.
        """
        return get_service().research.program_factor_history(program_id, factor_id, offset=offset, limit=limit)

    @mcp.tool()
    def assay_program_candidates(program_id: str, offset: int = 0, limit: int = 20) -> dict:
        """List canonical factors across a program and their session-specific decisions.

        Decision states are not automatically propagated between evaluation windows.
        """
        return get_service().research.program_candidates(program_id, offset=offset, limit=limit)

    @mcp.tool()
    def assay_program_reassess(program_id: str, source_experiment_id: str, target_session_id: str,
                               request_id: str, agent: str = 'unspecified') -> dict:
        """Evaluate a completed factor in another linked session and retain the source link.

        The target uses its own dataset and consumes its own budget. Reuse the
        request_id after a timeout. A changed dataset is not an identical replay.
        """
        return get_service().research.reassess_program_factor(program_id, source_experiment_id,
            target_session_id, request_id=request_id, agent=agent)

    @mcp.tool()
    def assay_program_compare_windows(program_id: str, baseline_experiment_id: str, target_experiment_id: str,
                                      drop_threshold: float) -> dict:
        """Compare a factor's signed RankIC on ordered, non-overlapping windows.

        Uses the baseline sign and caller's positive absolute RankIC drop threshold.
        The flag is descriptive, not a statistical or causal decay determination.
        Non-overlapping score dates do not verify label availability or an embargo.
        """
        return get_service().research.compare_program_windows(program_id, baseline_experiment_id,
            target_experiment_id, drop_threshold=drop_threshold)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True,destructiveHint=False,
                                         idempotentHint=True,openWorldHint=False))
    def assay_research_audit(session_id: str, offset: int = 0, limit: int = 20,
                             expected_manifest_hash: str | None = None) -> dict:
        """Audit all saved session evidence for a human or agent judge, without execution.

        Checks hashes, budgets, lineage/decisions, metric-series arithmetic, dates,
        portfolio assumptions and local registry links. Failed experiments remain
        in the counts. Findings and candidate evidence are paginated (limit 1–100);
        overall errors cover the full snapshot. Pin expected_manifest_hash when
        reading further pages. Free-text notes and conclusions are not fact-checked;
        the audit does not prove authorship, replay or profitability.
        """
        return get_service().research.audit_session(session_id,offset=offset,limit=limit,
                                                    expected_manifest_hash=expected_manifest_hash)

    @mcp.tool()
    def assay_research_import(manifest: dict) -> dict:
        """Import a hashed session manifest atomically, preserving IDs and spent budget.

        Only terminal attempts can be imported. Exact repeated imports are
        idempotent; conflicting local records are rejected. Register the backend
        and provide market data separately before continuing research. Hash
        integrity does not establish trust in the author or historical reports.
        """
        return get_service().research.import_session(manifest)

    @mcp.tool()
    def assay_backend_list(probe: bool = False) -> dict:
        """Discover installed and referenced research backends and declared capabilities.

        probe=True performs read-only availability checks. Availability does not
        certify coverage of a requested dataset period or immutable market data.
        """
        return {"backends": get_service().research.list_backends(probe=probe)}

    @mcp.tool()
    def assay_research_context(session_id: str, limit: int = 10) -> dict:
        """Read compact handoff context: budget, failures, candidates, repeats and notes.

        Each list is limited to 1–50 rows; inspect time series with experiment_get pages.
        Candidate ranking is descriptive, and expression repeats are syntactic.
        """
        return get_service().research.research_context(session_id, limit=limit)

    @mcp.tool()
    def assay_research_export(session_id: str) -> dict:
        """Export a consistent session manifest with a SHA-256 integrity hash.

        Includes research records, not market data or executable backend code.
        The hash detects changes; it does not authenticate the source.
        """
        return get_service().research.export_session(session_id)

    @mcp.tool()
    def assay_experiment_replay(
        experiment_id: str, session_id: str, request_id: str, agent: str = "unspecified",
    ) -> dict:
        """Rerun a terminal attempt with its original dataset and accounting protocol.

        Creates a linked attempt and consumes budget. Reusing request_id is
        idempotent. Mutable market data may produce different results.
        """
        return get_service().research.replay_experiment(
            experiment_id, session_id, request_id=request_id, agent=agent,
        )

    @mcp.tool()
    def assay_dataset_register(
        name: str, universe: str, period: list[str], as_of: str | None = None,
        horizons: list[int] | None = None, execution: str = "next_open",
        adj: str = "split", backend: str = "assay", snapshot_id: str | None = None,
        description: str = "",
        backend_options: dict | None = None,
        score_period: list[str] | None = None, score_dates: list[str] | None = None,
        expression_policy: dict | None = None,
        portfolio: dict | None = None,
    ) -> dict:
        """Register a fixed daily evaluation dataset specification, without downloading data.

        Discover available universes first. Dataset registration does not verify data
        availability or freeze the physical store. Custom backends must be installed
        in the serving process; snapshot verification is the backend's responsibility.
        """
        return get_service().research.register_dataset(
            name, universe, period, backend=backend, as_of=as_of, horizons=horizons,
            execution=execution, adj=adj, snapshot_id=snapshot_id, description=description,
            backend_options=backend_options,
            score_period=score_period, score_dates=score_dates, expression_policy=expression_policy,
            portfolio=portfolio,
        )

    @mcp.tool()
    def assay_dataset_list() -> dict:
        """List registered dataset IDs, fixed evaluation settings and specification hashes."""
        return {"datasets": get_service().research.list_datasets()}

    @mcp.tool()
    def assay_research_session_create(
        dataset_id: str, goal: str, agent: str = "unspecified", max_experiments: int = 20,
    ) -> dict:
        """Start a durable research session with a goal and an attempt budget.

        This is distinct from temporary panel-cache sessions. Failed and abandoned
        attempts consume budget. Any agent may resume the session by reading its ID.
        """
        return get_service().research.create_session(
            dataset_id, goal, agent=agent, max_experiments=max_experiments,
        )

    @mcp.tool()
    def assay_research_session_list() -> dict:
        """Discover persisted research sessions after restart or an agent handoff."""
        return {"sessions": get_service().research.list_sessions()}

    @mcp.tool()
    def assay_research_session_get(session_id: str) -> dict:
        """Resume context: read goal, dataset, notes, all attempts and remaining budget.

        Inspect prior failures before proposing new expressions. A running attempt
        may still be executing or may have been interrupted; do not blindly retry.
        """
        return get_service().research.get_session(session_id)

    @mcp.tool()
    def assay_research_note(session_id: str, text: str, agent: str = "unspecified") -> dict:
        """Append a durable research decision, failed direction, or next-step handoff note.

        ``agent`` is a caller-supplied label, not an authenticated model identity.
        """
        return get_service().research.add_note(session_id, text, agent=agent)

    @mcp.tool()
    def assay_experiment_run(
        session_id: str, expr: str, hypothesis: str, request_id: str,
        agent: str = "unspecified", parent_id: str | None = None,
    ) -> dict:
        """Evaluate a hypothesis under the session's fixed dataset settings and save the attempt.

        Choose request_id once per attempt; reuse it after a timeout to avoid double
        execution. A new request_id spends another budget unit. parent_id connects a
        revision to an earlier experiment. Failed reports and backend exceptions are
        preserved. This tool performs one evaluation; the agent decides what to try next.
        """
        return get_service().research.evaluate(
            session_id, expr, hypothesis, request_id=request_id, agent=agent, parent_id=parent_id,
        )

    @mcp.tool()
    def assay_experiment_reconcile(experiment_id: str) -> dict:
        """Poll/recover the same asynchronous job, retaining budget and request identity.

        Reconcile after a timeout or restart instead of starting a new attempt.
        This does not retry a terminal failure or cancel a live worker.
        """
        from assay.research_errors import ResearchError
        try:
            return get_service().research.reconcile_experiment(experiment_id)
        except ResearchError as exc:
            return {"error":exc.to_dict()}

    @mcp.tool()
    def assay_experiment_cancel(experiment_id: str, reason: str) -> dict:
        """Durably request async cancellation, retaining the attempt budget charge.

        Queued tasks are stopped and leases fenced; remote computation already
        running may finish, but late results are rejected. Accepted completion
        wins a race with cancellation. Reconcile a pending cancellation after
        a timeout or backend outage. Repeated requests retain the first reason.
        """
        from assay.research_errors import ResearchError
        try:
            return get_service().research.cancel_experiment(experiment_id, reason)
        except ResearchError as exc:
            return {"error": exc.to_dict()}

    @mcp.tool()
    def assay_experiment_get(
        experiment_id: str, section: str = "summary", offset: int = 0, limit: int = 20,
    ) -> dict:
        """Inspect a saved attempt with compact metrics, costs, diagnostics and error.

        Default summary omits arrays and trade details. Use section trades,
        positions, nav, ic or diagnostics for pages (offset >= 0, limit 1–100).
        Full original evidence remains in the research store and session export.
        """
        from assay.research_inspection import inspect_experiment
        return inspect_experiment(get_service().research.get_experiment(experiment_id),
                                  section=section, offset=offset, limit=limit)

    @mcp.tool()
    def assay_experiment_compare(experiment_ids: list[str]) -> dict:
        """Compare attempts, retaining failed rows and flagging mismatched evaluation specs.

        Comparable requires identical registered settings and accounting protocols; it
        does not certify immutable data, absence of selection bias, or out-of-sample performance.
        """
        return get_service().research.compare_experiments(experiment_ids)

    @mcp.tool()
    def assay_experiment_abandon(experiment_id: str, reason: str) -> dict:
        """Explicitly close an interrupted running attempt after checking its worker is stopped.

        Budget is not refunded. Rerun using a new request_id and reference this attempt
        as parent_id. This changes the record; it does not cancel a running backend.
        """
        return get_service().research.abandon_experiment(experiment_id, reason)

    @mcp.tool()
    def assay_candidate_decide(experiment_id: str, decision: str, reason: str,
                               request_id: str, agent: str = 'unspecified') -> dict:
        """Accept, reject, archive or reopen a candidate using a saved experiment as evidence.

        Decisions are per session and append-only. Only completed experiments can
        support acceptance. Reuse request_id after a timeout; this spends no experiment budget.
        """
        return get_service().research.decide_candidate(experiment_id, decision, reason,
                                                       request_id=request_id, agent=agent)

    @mcp.tool()
    def assay_candidate_list(session_id: str) -> dict:
        """List canonical factors, latest session decisions and supporting attempt IDs."""
        return {'candidates': get_service().research.list_candidates(session_id)}

    @mcp.tool()
    def assay_factor_history(
        factor_id: str, session_id: str | None = None, offset: int = 0, limit: int = 20,
    ) -> dict:
        """Page a factor's attempt summaries and decisions in chronological order.

        Both lists use offset/limit (1–100); history_page reports separate totals
        and the next offset. Use experiment_get for detailed evidence pages.
        """
        from assay.research_inspection import inspect_factor_history
        return inspect_factor_history(get_service().research.factor_history(factor_id, session_id=session_id),
                                      offset=offset, limit=limit)
