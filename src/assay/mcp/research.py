"""Research-lifecycle MCP tools. The external agent owns its planning strategy."""
from __future__ import annotations


def register_research_tools(mcp, get_service):
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

        Each list is limited to 1–50 rows; full time series remain in experiment_get.
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
        """Rerun a terminal attempt with its original expression and identical dataset spec.

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
        """Append a durable research decision, failed direction, or next-step handoff note."""
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
    def assay_experiment_get(experiment_id: str) -> dict:
        """Inspect a saved attempt, including its hypothesis, full report, diagnostics and error."""
        return get_service().research.get_experiment(experiment_id)

    @mcp.tool()
    def assay_experiment_compare(experiment_ids: list[str]) -> dict:
        """Compare attempts, retaining failed rows and flagging mismatched evaluation specs.

        Comparable means identical registered settings; it does not certify immutable
        data, absence of selection bias, or out-of-sample performance.
        """
        return get_service().research.compare_experiments(experiment_ids)

    @mcp.tool()
    def assay_experiment_abandon(experiment_id: str, reason: str) -> dict:
        """Explicitly close an interrupted running attempt after checking its worker is stopped.

        Budget is not refunded. Rerun using a new request_id and reference this attempt
        as parent_id. This changes the record; it does not cancel a running backend.
        """
        return get_service().research.abandon_experiment(experiment_id, reason)
