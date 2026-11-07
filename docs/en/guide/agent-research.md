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

State lives at `<data_dir>/research/workspace.sqlite3`. After restarting, use
`ws.list_sessions()` and `ws.get_session(id)` to recover the goal, dataset, notes,
all attempts and remaining budget. Every new request has its own experiment ID,
even when the expression is identical to an earlier attempt.

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

Start `assay serve-mcp`. Fifteen research tools complement the existing eleven evaluation/library tools:

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
| `assay_experiment_get` | Inspect the complete report, diagnostics and error |
| `assay_experiment_compare` | Compare metrics and specification compatibility |
| `assay_experiment_abandon` | Explicitly close an interrupted attempt |

An agent discovers live universes, registers a dataset, starts or resumes a session,
reads prior attempts, lints candidates, executes an experiment, inspects evidence,
and records its next decision. It can then revise, call the existing portfolio
tools, or stop. There is no provider call or fixed search algorithm in the workspace.

## Retry, failure and recovery

An attempt transitions from `running` to `completed` or `failed`; explicit recovery
may change `running` to `abandoned`. Completed means the evaluator returned without
a failure mode, not that the factor is profitable or statistically validated.

Use a stable `request_id` per attempt. Retrying it returns saved state without
executing again, including when still running. Different inputs under the same
request ID are rejected. SQLite reservations enforce budgets across processes;
failed and abandoned attempts consume budget. Terminal experiments are not
overwritten by subsequent runs, and notes are appended through the API.

A worker interruption leaves a running record. Confirm the worker has stopped,
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

Running attempts cannot be migrated: confirm their worker stopped, explicitly abandon
them, and export again. External `replay_of` references are retained as provenance;
the source session is not automatically imported. When the source is available,
its status, expression, hypothesis and specification must match, in either import order. A missing external source must be
imported separately if it needs to be inspected. Hashes do not authenticate historical
reports. Importing records does not transfer a running agent, its search tree or data.

Import does not disable the source workspace. Budgets apply within each workspace;
choose where to continue after migration. This is snapshot copying, not distributed
worker coordination or a global budget service.
