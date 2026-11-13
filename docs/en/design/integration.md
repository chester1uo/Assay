# Integrated Assay architecture

> This document describes the current local architecture and retains historical
> integration evidence. See the [project overview](../../cn/project-overview.md)
> for the distinction between local and deployed versions.

Assay combines the upstream factor computation platform with a persistent research
workspace. Both live in this repository; QFinVerse is an optional data producer,
not a service that must be installed for the offline research workflow.

## Complete capability map

1. **Data and computation:** prepared US/CN/HK data and an optional QFinVerse daily
   panel, Python/qlib expression syntax, a shared AST, factor scoring, diagnostics,
   factor combinations, and portfolio backtests.
2. **Shared factor infrastructure:** FactorDB identities, deduplication, values,
   novelty queries, and batch jobs; optional ADES controllers and workers execute
   job slices with leases and heartbeats.
3. **Persistent research:** dataset specifications, goals, attempt budgets,
   successful/failed experiments, revisions, notes, context, explicit replay, and
   integrity-checked export/import.
4. **Agent access:** 40 MCP tools, a replaceable reference client with request/tool
   limits and raw receipts, SDK and REST. The agent supplies the research policy.
5. **Human access:** the four-area workbench in the integrated sidebar, accounts
   and keys, Doc Hub, and existing factor and portfolio pages.

| Task | Entry points | Records |
| --- | --- | --- |
| Individual evaluation/diagnosis | `assay_evaluate`, `assay_lint`, `/v1/factor/*` | FactorReport and existing factor library |
| Persistent hypothesis testing | `assay_research_*`, `assay_experiment_*`, `/v1/research/*` | Session, independent attempts, notes, manifests |
| Shared batch computation | `fdb_submit`, `fdb_job`, `fdb_query`, `/v1/fdb/*` | FactorDB factors, specs, jobs |
| Distributed FactorDB execution | `/v1/ades/*` and worker processes | Leased slices and results |
| Human inspection/intervention | `/#/research`, console pages, `/doc/` | The same research records as SDK/MCP |

A session workflow registers a dataset, creates a goal, inspects context, evaluates
candidates, retains failures, links revisions, writes notes and exports records.
The FactorDB submit/job/query workflow can use ADES but does not automatically
produce those session records. Selecting the `factordb_ades` research backend links
a session attempt to a durable FactorDB job and its versioned result receipt.

## Responsibilities

| Component | Responsibility | Retained implementation |
| --- | --- | --- |
| Factor engine and portfolio evaluation | Parse expressions, compute panels, score factors, simulate portfolios | Upstream engine, vectorized operations, optional Rust kernels and OSQP/Clarabel solvers |
| Factor infrastructure | Canonical factor identities, stored values, novelty queries, asynchronous jobs | Upstream FactorDB and its eight MCP operations; private accounts use separate registries |
| Distributed execution | Lease factor-computation slices to workers | Upstream ADES controller, worker, heartbeat and task queue |
| Research workspace | Dataset specifications, goals, attempt budgets, hypotheses, failed evaluations, parent links, notes, manifests | Persistent SQLite workspace and backend protocol |
| Agent and human clients | Choose research actions, inspect outcomes and continue sessions | Reference agent, 21 research MCP operations, SDK/REST and browser workbench |
| Application shell | Authentication, navigation and documentation | Upstream account/session authentication and sidebar, with the research page added |

The MCP server has 40 operations: 11 evaluation/library tools, eight FactorDB tools,
and 21 research tools. HTTP MCP is mounted at `/mcp/`; each API application owns
an independent HTTP lifecycle. Stdio remains available for local agent clients.

## Distinct records and budgets

A FactorDB factor is a canonical expression and its computed evidence. Service keys
use a shared workspace, while private accounts use separate registries. A
research attempt records one session decision, including failure and provenance.
Repeated attempts remain separate records even when their expressions match.
The existing factor library remains available as well. Integrating the modules
does not silently merge their schemas or delete historical records.

The default research backend calls the in-process evaluator. The opt-in
`factordb_ades` backend submits the same research attempt to FactorDB/ADES and
stores its job ID, factor ID, spec, owner and logical data version. Call
`assay_experiment_reconcile` after a timeout or restart; repeated reconciliation
preserves submission identity and spends no additional attempt budget. Queue
completion/final failure receipts survive interruption before registry updates;
registry application records progress and compute usage once per task.

The workspace separately enforces a per-session attempt budget. FactorDB daily
compute usage is charged on the receipt's UTC completion day. With account
authentication enabled, private tasks are attributed to the account; service keys
retain the shared service workspace. This budget is not a reservation of future
compute or a hard CPU, memory, or storage quota.

Persistent research sessions differ from temporary evaluator panel sessions.
Reconciliation recovers asynchronous execution evidence; it does not restart a
worker or restore a model conversation. Synchronous interrupted attempts still
require inspection and explicit abandonment. Live asynchronous attempts cannot
be abandoned; cancellation uses `assay_experiment_cancel` with a saved reason.
Cancellation fences leases and delayed dispatch, and rejects late results; it does
not interrupt native computation already running on a remote worker. Accepted
completion wins a race with cancellation. The attempt charge remains consumed. See the asynchronous recipe in
[Agent research](../guide/agent-research.md).

## Authentication and data

Research REST routes use the existing `get_api_key` dependency, inheriting service
keys, personal API keys, account cookies and same-origin mutation checks. The
local implementation provides private per-account research stores when account
authentication is enabled; service keys retain the shared workspace. Market data,
the ADES queue and trusted workers can still be shared. The current deployed
version uses a shared research workspace. Manual browser writes remain labeled
`human:webui`.

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

## Validation and paper scope

The integrated suite reports 1,146 passing tests, three skips and zero failures.
Two optional unbuilt Rust test modules and one optional scikit-learn test are
skipped; native throughput and production performance gains are not reported.
The real browser regression covers failure, revision, notes, original manifest
bytes, fresh-workspace import and reload, separately from transport-race fixtures.
An installed wheel is checked outside the repository, including actual HTTP MCP.

The earlier model evidence contains 24 tasks, four configurations and three model
families, each task completing two new evaluations. These inventories use earlier
frozen sources and do not evaluate FactorDB, ADES or the integrated HTTP service.
The system paper describes component boundaries, inspectable workflows and this
versioned evidence. Real-task benefits, researcher time savings, deployment and
framework comparisons remain unmeasured. Further state ablations are optional
analysis, rather than a default prerequisite for a system paper.

The subsequent asynchronous bridge passes 1,160 tests with three optional skips;
real-worker and fault-injection checks are retained under `artifacts/async-research-20261108/`.

The cancellation extension passes 1,177 tests with three optional skips. Evidence
is retained under `artifacts/research-cancellation-20261108/`. Discarded in-flight
worker compute is not measured by accepted-result usage accounting.
