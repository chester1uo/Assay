# Agent-native factor research

Assay provides durable research state and an execution environment for external
agents. The caller chooses its model, prompts and search strategy. Existing
evaluation APIs and temporary `assay.Session` panel caches remain available.

## Python workflow

For an offline smoke demo with synthetic prices and two simulated agent clients:
`PYTHONPATH=src python scripts/demo_agent_research.py`. It uses the real evaluator,
retains a failed attempt, reconstructs the service, and continues a saved session.
This exercises lifecycle behavior; it does not validate real LLM compatibility.

Install `pip install -e '.[mcp,perf]'` and prepare market data using
[Getting Started](getting-started.md). Dataset registration does not download data.

```python
import assay

assay.init()  # ASSAY_DATA_DIR and market configuration
ws = assay.research_workspace()
dataset = ws.register_dataset(
    "nasdaq-reversal-2024", "NASDAQ100", ["2024-01-01", "2024-12-31"],
    horizons=[1, 5, 10], execution="next_open", adj="split",
)
session = ws.create_session(dataset["id"], "Study short-term reversal",
                            agent="agent-a", max_experiments=20)
first = ws.evaluate(
    session["id"], "-cs_rank(ts_returns(close,5))", "Recent winners may reverse",
    request_id="reversal-5d-01", agent="agent-a",
)
ws.add_note(session["id"], "Compare shorter lookbacks next", agent="agent-a")
second = ws.evaluate(
    session["id"], "-cs_rank(ts_returns(close,3))", "Shorter windows may reduce delay",
    request_id="reversal-3d-01", parent_id=first["id"], agent="agent-b",
)
print(ws.compare_experiments([first["id"], second["id"]]))
```

In local or service-key mode, state lives at `<data_dir>/research/workspace.sqlite3`. After restarting, use
`ws.list_sessions()` and `ws.get_session(id)` to recover the goal, dataset, notes,
all attempts and remaining budget. Every new request has its own experiment ID,
even when the expression is identical to an earlier attempt.

## Factor selection and portfolio evidence

New built-in `assay` datasets with next-day execution run both factor diagnostics
and a portfolio backtest. `report.portfolio` contains cost-adjusted NAV, return,
Sharpe, drawdown, trading turnover and cost drag. Portfolio settings are frozen in
the dataset hash. For example, pass `portfolio={"rebalance_type": "weekly"}` when
registering a dataset. `portfolio={"enabled": False}` selects diagnostics only;
explicit IC scoring windows and same-day `close` diagnostics use this mode by
default. Existing saved dataset specifications retain their original settings.
Reports record absent impact/capital inputs and held positions with missing quotes.

Every parseable attempt registers its canonical factor in the workspace's
FactorDB registry in the same SQLite transaction. A factor can have many attempts
and many sessions; the complete reports remain in the experiment history. The
registry's latest score is a discovery view, not the experimental history.

```python
decision = ws.decide_candidate(
    second["id"], "accepted", "Keep the shorter lookback for further research",
    request_id="select-reversal-3d", agent="agent-b",
)
print(ws.list_candidates(session["id"]))
print(ws.factor_history(second["factor_id"]))
```

Decisions are append-only and scoped to a factor within one session. Supported
states are `candidate`, `accepted`, `rejected` and `archived`. Each decision points
to an experiment and records its reason, author and retry identity. Acceptance
requires a completed experiment. Decisions do not consume experiment budget and
survive restart, export and import. Handoff context omits rejected and archived
factors from its active candidate ranking.

`assay_candidate_decide`, `assay_candidate_list` and `assay_factor_history` expose
these operations over MCP. The workbench displays portfolio metrics and records
human selection decisions through the same REST workspace. `fdb_query` and
`fdb_factor` discover both the research registry and the optional compute registry
with `source="all"` (default), `"research"` or `"compute"`. Compute submission and
node-value storage still require the configured FactorDB/ADES backend; discovery
does not copy nodes or merge the two physical databases.

`next_open` executes after the signal date and attributes overnight returns to the
old book and intraday returns to the executed book. `next_close` enters at the next
day's close, including in IC labels. Corrected daily evaluation settings carry
`evaluation_protocol="assay-daily-v2"`; old `next_close` datasets must be registered
again. Their stored reports remain readable. Compute score keys also version this
execution convention, so old labels cannot satisfy new requests from cache.

Observed panel hashes include prices, membership and group labels. Precomputed
subtrees retain pre-admission rolling history, and membership is applied when
computing cross-sectional operators and final outputs. Matching replay hashes
verify the observed input content; they do not create a physical market-data archive.

Run `scripts/validate_research_loop.py --data-root PREPARED_STORE --output NEW_DIR`
to check the loop against real prepared data. Outputs include session manifests,
source and implementation hashes, cash/share accounting reconciliation, and
separate timings for complete research attempts, fresh factor matrices, shared
panel/CSE matrix batches and exact result-cache hits. Matrix throughput is not
end-to-end backtest throughput. The independent ledger checks NAV using recorded
fills; portfolio selection and vendor data completeness need separate validation.

## Scoring windows and expression contracts

`period` is the data-read interval, including warmup and forward-label buffers.
Register `score_period` and sorted `score_dates` to compute headline IC/RankIC only
on independently frozen trading dates. Explicit scoring currently supports one
horizon. Missing dates, null scores, misaligned arrays or extra in-window dates
produce a retained `INCOMPLETE_SCORE_COVERAGE` failure, never an aggregate fallback.

The experiment's `report` contains scoped metrics and dates; `panel_report` retains
the original backend evidence. Turnover, quintile returns and decay are set to null
in the scoped report because daily IC arrays cannot recompute those metrics.

```python
dataset = ws.register_dataset(
    "reversal-scoped", "NASDAQ100", ["2024-01-01", "2024-08-02"], horizons=[1],
    score_period=["2024-07-01", "2024-07-02"],
    score_dates=["2024-07-01", "2024-07-02"],
    expression_policy={
        "fields": ["close", "volume"],
        "operators": ["ts_returns", "ts_mean", "cs_rank", "neg"],
        "max_window": 60, "max_cumulative_lookback": 100,
    },
)
```

Obtain the expected trading dates from a verified calendar before registration.
Both the scoring scope and expression policy participate in `spec_hash`.
The workspace enforces field/operator whitelists and nested window budgets before
calling the backend. Rejected candidates remain failed attempts and consume one
attempt unit, but do not execute a market evaluation. These window-budget rules
match the stable AlphaBench OHLCV grammar; they are not an exact finite-history
claim for recursive filters such as EMA. The same contract applies through SDK
and MCP. Dataset registration specifies warmup/buffer bounds; it does not infer
or download extra history automatically.

## MCP workflow

Start `assay serve-mcp`. Twenty research tools complement the existing eleven evaluation/library tools:

| Tool | Purpose |
|---|---|
| `assay_backend_list` | Discover adapters and optionally check availability |
| `assay_research_context` | Read compact, evidence-linked handoff context |
| `assay_research_import` | Restore a manifest into another workspace without resetting budget |
| `assay_research_export` | Export full session state with a SHA-256 integrity hash |
| `assay_experiment_replay` | Explicitly rerun a terminal attempt under the same spec |
| `assay_dataset_register` | Register fixed daily evaluation settings |
| `assay_dataset_list` | Discover registered datasets |
| `assay_research_session_create` | Start a goal with an attempt budget |
| `assay_research_session_list` | Discover saved research sessions |
| `assay_research_session_get` | Recover context, notes and attempts |
| `assay_research_note` | Record decisions and handoff instructions |
| `assay_experiment_run` | Evaluate and persist a hypothesis and expression |
| `assay_experiment_get` | Read compact metrics, costs and failures; page through detailed evidence |
| `assay_experiment_compare` | Compare metrics and specification compatibility |
| `assay_experiment_abandon` | Explicitly close an interrupted synchronous attempt |
| `assay_candidate_decide` | Append an evidence-linked acceptance, rejection, archive or reopening |
| `assay_candidate_list` | List canonical candidates and their current session decisions |
| `assay_factor_history` | Page a factor's attempt summaries and decision history |
| `assay_experiment_reconcile` | Recover/poll a durable asynchronous attempt |
| `assay_experiment_cancel` | Persist a reason and cancel asynchronous task acceptance |

An agent discovers live universes, registers a dataset, starts or resumes a session,
reads prior attempts, lints candidates, executes an experiment, inspects evidence,
and records its next decision. It can then revise, call the existing portfolio
tools, or stop. There is no provider call or fixed search algorithm in the workspace.

`assay_experiment_get(id)` now returns a compact summary by default. Request
`section="trades"`, `"positions"`, `"nav"`, `"ic"` or `"diagnostics"` with
`offset` and `limit` (1–100) to inspect details. Pages include `total` and
`next_offset`. This keeps large backtests within model context limits. The Python
workspace and REST experiment endpoint retain full reports. For long sessions,
use `assay_research_context` for model input; session export and the complete
session tool return bulk evidence for storage and analysis.
`assay_factor_history` also accepts `offset` and `limit`, paging both chronological
lists. Its `history_page` reports separate experiment and decision totals.

## Retry, failure and recovery

An attempt transitions from `running` to `completed` or `failed`; explicit recovery
may change synchronous `running` to `abandoned`, or asynchronous `running` to `cancelled`. Completed means the evaluator returned without
a failure mode, not that the factor is profitable or statistically validated.

Use a stable `request_id` per attempt. Retrying it returns saved state without
executing again, including when still running. Different inputs under the same
request ID are rejected. SQLite reservations enforce budgets across processes;
Failed, abandoned and cancelled attempts consume budget. Terminal experiments are not
overwritten by subsequent runs, and notes are appended through the API.

A synchronous worker interruption leaves a running record. Confirm the worker has stopped,
abandon the attempt with a reason, then retry with a new request ID. Abandoning a
record does not cancel a live backend. Backend exceptions and diagnostic failures
are retained for the next agent to inspect.

## Alternative backends and evidence boundaries

Implement `ResearchBackend.evaluate(expr, dataset) -> dict`, returning a JSON-safe
factor report (`failure_mode`, metrics and optional diagnostics). Then call
`ws.register_backend("team", my_backend)` and register datasets with `backend="team"`.
Backend names are persisted; objects and credentials must be re-registered per
serving process. QFV can be integrated at this boundary; this change does not
install a QFV execution adapter.

Custom datasets can include JSON-safe `backend_options`, which participate in
the specification hash. The built-in evaluator rejects unsupported options.
`assay.integrations.alphabench.AlphaBenchBackend` is an optional adapter to
AlphaBench V2's patched `localhost:8002` Assay service; it verifies the registered
endpoint, frozen universe date, symbol-axis hash/count and data cutoff before
exposing successful reports. It requires AlphaBench's Python package at runtime.

Datasets are immutable **specifications**, not immutable copies of market data.
Their SHA-256 `spec_hash` covers backend name, universe, dates, cutoff, horizons,
execution, adjustment and optional snapshot reference. It excludes market-data
contents, backend code and model behavior. Matching specs do not certify unchanged
underlying data or out-of-sample validity. Comparisons retain failed rows and flag
mismatched specs, without ranking incompatible experiments.

The built-in Assay backend cannot resolve snapshot IDs and rejects them. Custom
backends must resolve and verify any supplied `snapshot_id`. Existing end-date
universe selection is unchanged. Train/test isolation, selection-bias control and
portfolio validation remain explicit research choices.

Budgets cover evaluator attempts, not tokens, wall time or portfolio tool calls.
Automatic general-purpose agent runners, built-in token billing and dedicated
research-session WebUI/REST surfaces are not implemented in this version.

## Discovery, handoff and explicit reruns

Call `assay_backend_list(probe=True)` before choosing a backend. The response separates
declared capabilities from checked availability and shows backend names referenced
by persisted datasets even when their adapters have not been registered after restart.
Local checks inspect prepared files; remote checks inspect service inventory. Neither
certifies coverage of a requested period. Adapters may optionally implement
`describe() -> dict` and `probe() -> dict`; plain `evaluate()` adapters remain supported.

Use `assay_research_context(session_id, limit=10)` for handoff: it returns remaining
budget, failure counts, normalized expression repeats, notes and candidate summaries
without metric time series. Each list is limited to 1–50 rows, with truncation flags.
Individual text fields are not token-limited. Ranking uses absolute RankIC and is
descriptive; syntactic duplicates do not measure factor correlation.

```python
backends = ws.list_backends(probe=True)
context = ws.research_context(session_id, limit=10)
manifest = ws.export_session(session_id)
assert ws.verify_manifest(manifest)
rerun = ws.replay_experiment(experiment_id, session_id,
                            request_id="explicit-rerun-1", agent="next-agent")
```

The export reads one consistent SQLite snapshot and has a stable hash until records
change. It includes complete research records, not market-data bytes or adapter code.
The hash checks integrity, not source authenticity; use `assay_research_import` to restore records into another workspace. A replay preserves the source expression, hypothesis and spec hash,
records `replay_of`, and spends a new budget unit. It can target another session with
an identical dataset specification. Reusing its request ID returns the saved attempt.
Mutable data may change the result. This does not restore an agent's search tree.

## Migrating a session

Call `target.import_session(manifest)` or MCP `assay_research_import(manifest)` to
restore a version-1 manifest. The importer validates integrity, structure, dataset
specification hash, attempt identities, lineage and budget before writing one atomic
transaction. It retains original IDs, request IDs, notes, timestamps and all terminal
attempts. Importing the exact same state again is idempotent. Existing conflicting
records, including a session that has advanced since export, are rejected without
overwriting or merging. Backends need not be registered to read imported records;
register the matching backend and make its data available before evaluating again.

Running attempts cannot be migrated. Reconcile or cancel asynchronous attempts;
for synchronous attempts, confirm the worker stopped and explicitly abandon them.
Export after the records reach terminal states. External `replay_of` references are retained as provenance;
the source session is not automatically imported. When the source is available,
its status, expression, hypothesis and specification must match, in either import order. A missing external source must be
imported separately if it needs to be inspected. Hashes do not authenticate historical
reports. Importing records does not transfer a running agent, its search tree or data.

Import does not disable the source workspace. Budgets apply within each workspace;
choose where to continue after migration. This is snapshot copying, not distributed
worker coordination or a global budget service.

## Durable asynchronous research

Configure the FactorDB controller with `ASSAY_FACTORDB_DIR`,
`ASSAY_QFV_PANEL_DIR`, `ASSAY_ADES=1`, and separate worker credentials
(`ASSAY_WORKER_KEYS`). Run an ADES worker against the same panel version.
Probe `assay_backend_list(probe=true)` and copy its `factordb_ades` data version
into `snapshot_id` when registering the dataset. The SDK equivalent is:

```python
ws = service.research
version = ws.backends["factordb_ades"].probe()["data_version"]
dataset = ws.register_dataset(
    "panel-reversal", "TOP500", ["2024-12-02", "2025-01-24"],
    backend="factordb_ades", snapshot_id=version, adj="total", horizons=[1],
)
session = ws.create_session(dataset["id"], "Test short-term reversal", max_experiments=6)
attempt = ws.evaluate(session["id"], "-ts_delta(close, 2)",
                      "Recent moves may reverse", request_id="candidate-1")
# Later, including after constructing a new service/workspace process:
attempt = ws.reconcile_experiment(attempt["id"])
```

Use dates/universe supported by your own panel. This backend requires total
adjustment and `as_of=end`, and rejects score windows and custom backend options.
It pins the logical panel version, without hashing or freezing physical files.
Results contain aggregate FactorDB diagnostics, not the full synchronous report.
A changed controller version rejects a new submission; an already submitted
attempt keeps its original result version.

The MCP operation is `assay_experiment_reconcile(experiment_id)`; REST exposes
`POST /v1/research/experiments/{id}/reconcile`. The workbench offers
**Reconcile asynchronous jobs**. Reconciliation is explicit: reads do not poll the
backend. A transient error retains a running attempt and includes `code`,
`retryable`, and `recommended_action`. Correct deterministic input failures in a
new attempt, linking the failed attempt as its parent. Active asynchronous jobs
cannot be abandoned; use tracked cancellation instead. Keep registry, queue and research databases
when restarting; exporting a completed manifest preserves records, not worker
processes or physical market data.

## Portfolio accounting protocol

Portfolio targets are fractions of pre-trade NAV. Executed quantities are preserved when transaction costs are charged to cash; resulting weights are recalculated against post-fee NAV. Long-only, unlevered books reserve cash for costs. Explicitly leveraged or long-short books can borrow. New datasets and portfolio attempts record `portfolio_accounting_protocol: cash-funded-v1`, also discoverable in the Assay backend capabilities. Existing sessions can continue using their original dataset: historical records remain unchanged and new attempts record the current protocol. Comparisons flag different accounting protocols as not directly comparable; replay requires the original protocol.

Set `portfolio.initial_capital` to a positive amount in the market currency to apply minimum commissions and enable trailing liquidity for newly registered datasets. At each rebalance, participation is the order value divided by the average unadjusted daily close × volume over the preceding `adv_window` exchange sessions (20 by default); the execution day is excluded. It uses the current portfolio NAV, caps fills at `max_adv_fraction`, and applies the configured slippage model to filled participation. Missing or incomplete history blocks affected orders. `partial_fill_handling="force"` bypasses the capacity cap and produces a warning. Reported liquidity evidence includes coverage, blocked orders and whether impact was applied. Market-data vendors can revise historical bars; this calculation alone does not guarantee point-in-time revision history.

US and HK volumes default to shares. For A-shares, set `liquidity_volume_multiplier` explicitly: use 100 for a Tushare volume in lots, or 1 if volume is already in shares. For example: `portfolio={"initial_capital": 1000000, "liquidity_volume_multiplier": 100}`. An explicit `liquidity_model: null` disables automatic liquidity. Without capital or participation input, capacity and market impact are not applied; the report warns. Existing frozen dataset settings are preserved. The REST portfolio endpoint enables the model for inline requests with capital; a nested `config` is replayed verbatim and must explicitly include `liquidity_model` and the volume multiplier.

## Account workspaces and shared resources

With account authentication enabled, REST and HTTP MCP use the authenticated account identity to select a private persistent research workspace; a user's login and personal API keys access the same workspace. A service key accesses the original shared service workspace, so keep it as a privileged integration credential. The first upgrade adds a persistent random identity to each account; existing browser sessions must sign in again. Account database and workspace data should be backed up and restored together. Earlier experimental account directories keyed by numeric user ID are not automatically assigned to the new identities; an administrator must verify ownership and migrate any needed records explicitly. Old shared sessions remain there; an administrator can export an individual session and import it into the correct account after checking ownership. Local Python and unauthenticated deployments retain a shared local workspace.

Research datasets, sessions, experiments, notes, the research factor registry, the factor library, saved combinations, temporary panel sessions and calculation caches are account-scoped. Personal workspaces can reuse their own research panels and cached values. Market data and physical compute remain shared. The original library and combinations remain accessible to the service key; account workspaces begin with their own empty stores. With FactorDB and ADES configured, a personal account can use its own FactorDB registry and the `factordb_ades` research backend. Jobs, scores, factor metadata, novelty fingerprints and compute usage remain account-scoped; the ADES queue and workers are shared. Workers and server operators can see the expressions they execute and must be trusted. `ASSAY_FACTORDB_BUDGET_S` can set a per-owner daily compute budget, checked before submission; hard CPU, memory and storage quotas are not provided. Temporary panel sessions live in one process: route requests for a session to the same worker, and recreate it after a restart. Durable research sessions, saved factors and combinations persist across workers and restarts.

To enable private distributed research, configure `ASSAY_FACTORDB_DIR`, `ASSAY_QFV_PANEL_DIR` and `ASSAY_ADES=1` on the controller, and connect workers using `ASSAY_WORKER_KEYS`. A personal account can call `/v1/fdb/markets` or `assay_backend_list(probe=true)` to obtain the data version, then register a research dataset with `backend="factordb_ades"`, `snapshot_id` set to that version, `adj="total"` and `as_of` equal to the end date. Submit and reconcile attempts through the usual research REST or MCP tools. Personal `/v1/fdb/*` and `fdb_*` calls use the same private compute registry. Without ADES, these compute operations report that the private backend is unavailable; the local Assay research backend remains usable. Private ADES jobs require updated workers that support lease tokens. For ordinary ADES-backed `fdb_submit`, provide a client-generated `request_id` and reuse it with identical inputs when retrying. REST `POST /v1/fdb/submit` and MCP `fdb_submit` share the same account-scoped request record. A repeated key with different inputs returns a conflict. Without a key, a retry can create another job; in non-ADES mode the new `request_id` option is not supported.

## Cancel asynchronous research

```python
result = ws.cancel_experiment(attempt["id"], "Research direction changed")
# After an outage/restart, reconciliation resumes the saved cancellation intent.
result = ws.reconcile_experiment(attempt["id"])
```

MCP exposes `assay_experiment_cancel(experiment_id, reason)`. REST exposes
`POST /v1/research/experiments/{id}/cancel` with `{"reason": "..."}`. In the
workbench, select a running experiment, expand **Cancel an asynchronous experiment**,
enter its reason and click **Cancel selected asynchronous experiment**.

The first reason is retained across retries. The adapter publishes
`supports_cancellation=true`. Cancellation stops queued work and revokes leases,
including a dispatch delayed until after cancellation. Remote native computation
already running may finish; its results are rejected. A completion accepted first
remains completed. Temporary failures keep the cancellation intent in a running
record; reconcile it instead of submitting a replacement under the same request.

A cancelled attempt is terminal and exportable, retains its attempt-budget charge,
and can be the parent of a new revision. Cancellation creates no factor score and
does not cache a failed evaluation. Daily compute usage counts accepted result
receipts; discarded in-flight computation is not metered. Synchronous backends
return `CANCELLATION_UNSUPPORTED`; inspect the worker before explicitly abandoning
an interrupted synchronous record.
