# Integrated Assay architecture

Assay combines the upstream factor computation platform with a persistent research
workspace. Both live in this repository; QFinVerse is an optional data producer,
not a service that must be installed for the offline research workflow.

## Responsibilities

| Component | Responsibility | Retained implementation |
| --- | --- | --- |
| Factor engine and portfolio evaluation | Parse expressions, compute panels, score factors, simulate portfolios | Upstream engine, vectorized operations, optional Rust kernels and OSQP/Clarabel solvers |
| Shared factor infrastructure | Canonical factor identities, stored values, novelty queries, asynchronous jobs | Upstream FactorDB and its eight MCP operations |
| Distributed execution | Lease factor-computation slices to workers | Upstream ADES controller, worker, heartbeat and task queue |
| Research workspace | Dataset specifications, goals, attempt budgets, hypotheses, failed evaluations, parent links, notes, manifests | Persistent SQLite workspace and backend protocol |
| Agent and human clients | Choose research actions, inspect outcomes and continue sessions | Reference agent, 15 research MCP operations, SDK/REST and browser workbench |
| Application shell | Authentication, navigation and documentation | Upstream account/session authentication and sidebar, with the research page added |

The MCP server has 34 operations: 11 evaluation/library tools, eight FactorDB tools,
and 15 research tools. HTTP MCP is mounted at `/mcp/`; each API application owns
an independent HTTP lifecycle. Stdio remains available for local agent clients.

## Distinct records and budgets

A FactorDB factor is a shared, canonical expression and its computed evidence. A
research attempt records one session decision, including failure and provenance.
Repeated attempts remain separate records even when their expressions match.
The existing factor library remains available as well. Integrating the modules
does not silently merge their schemas or delete historical records.

The default research backend calls the in-process evaluator. It does not
implicitly submit a FactorDB job or route a research attempt through ADES. Agents
can discover and invoke both tool groups, but the job IDs and experiment IDs are
distinct. FactorDB's daily compute budget applies to FactorDB jobs; the workspace
separately enforces a per-session attempt budget. Neither is a global budget
coordinator for all backends.

Persistent research sessions also differ from the evaluator's temporary panel
sessions. Restarting a client preserves research records, not a live conversation
or running worker. An interrupted attempt must be inspected and explicitly
abandoned when appropriate; ADES task leases do not automatically recover these
research attempts.

## Authentication and data

Research REST routes use the existing `get_api_key` dependency, inheriting service
keys, personal API keys, account cookies and same-origin mutation checks. The
workspace is shared among authorized callers; accounts do not create private
per-user research stores. Manual browser writes remain labeled `human:webui`.

The built-in data store and upstream QFinVerse-panel reader are retained. Local
synthetic and prepared data remain usable without a QFinVerse installation.
Dataset specification hashes identify configuration, not immutable physical data.

## Integration provenance

The preserved local research implementation is commit `871a961`; the upstream
baseline is `103532807af5fdcbf8748d081ca953bf430a35be`. A backup branch,
`codex/pre-upstream-integration-20261108`, retains the complete local state before
integration. The integration branch is `codex/integrate-upstream-20261108`.

Historical model studies retain their original source snapshots, plans and
receipts. Integration does not rewrite their results or imply that the studies
evaluated FactorDB, ADES or the new HTTP endpoint. New live studies must freeze
the integrated source as a new inventory. Integration checks and the independent
audit are retained under `artifacts/upstream-integration-20261108/`.
