# Assay project overview

Assay is an agent-facing environment for factor research, combining market-data
readers, expression evaluation, portfolio backtests, durable research sessions,
factor lifecycle records, and inspection tools. External agents choose the model,
hypotheses, and research policy; Assay executes and records their experiments.
QFinVerse is an optional market-data source, and other backends can be integrated.

## Research workflow

Register a dataset and start or resume a research session. The agent reads prior
context, lints and evaluates a candidate, inspects the resulting metrics or
failure, and saves its decision or handoff note. Sessions retain experiment IDs,
budgets, parent links, and exportable records. Research audit examines those
records; it does not prove investment performance or automatically rerun them.

The default research evaluator runs in process. The optional `factordb_ades`
backend links an experiment to a FactorDB job executed by ADES workers. A direct
FactorDB job is distinct from a research session. A temporary panel session is
also distinct from a durable research session and must be recreated after restart.

## Interfaces and capabilities

| Area | Entry point |
|---|---|
| Agent tools | [MCP](guide/mcp-server.md), including dataset, experiment, FactorDB and audit tools |
| Programmatic access | [REST](guide/rest-api.md), [Python SDK](guide/python-sdk.md), CLI |
| Human research | [Research workbench](guide/research-workbench.md) and WebUI |
| Execution | [Portfolio backtesting](guide/portfolio-backtest.md), factor evaluation, data readers |
| Persistence | [Agent research](guide/agent-research.md), [FactorDB](design/factordb.md), session manifests |

The public service is at https://assay.qfinverse.com/, with MCP at `/mcp/` and
REST under `/v1/`. Check `/.well-known/assay.json` for the deployed version,
tool discovery, authentication, and workspace scope. Personal keys and model
provider keys serve different purposes; Assay does not require a model key on
the server. The [deployment guide](guide/deployment.md) explains reviewer access.

## Version and evidence boundaries

The repository working tree and a deployed service can be different versions.
At the 2026-11-13 inspection, the public build was `03bab24` and used a shared
research workspace. Later local changes include private account workspaces,
trailing-liquidity inputs, and idempotent ADES submissions. The changes require
an explicit release before they are available on that service. Account-level
hard CPU, memory, and storage quotas are not implemented.

Dataset configuration hashes do not freeze a physical market-data snapshot.
The published benchmarks and agent studies each record their version and scope:
fixed-order replay is not a complete autonomous workflow, and synthetic agent
traces do not establish trading profitability. See the [Chinese project status
overview](../cn/project-overview.md) for the detailed local evidence inventory.
