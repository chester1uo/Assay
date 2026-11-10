# Reference agent and offline demo

Assay's server exposes tools and persists research records. The optional reference
client in `assay.agent` lets a Chat Completions-compatible model choose tools through
real stdio or authenticated HTTP MCP. It is replaceable: other MCP clients can use
the same research state.

## Connect to the public server

Set the model's `ASSAY_AGENT_API_KEY` and Assay's separate `ASSAY_MCP_API_KEY` through
your secret manager. `MODEL_BASE_URL` and `MODEL_NAME` select your model provider:

```bash
python -m assay.agent \
  --base-url "$MODEL_BASE_URL" --model "$MODEL_NAME" \
  --task-file task.txt --output ./remote-agent-run \
  --mcp-url https://assay.qfinverse.com/mcp/ \
  --mcp-api-key-env ASSAY_MCP_API_KEY --mcp-timeout 300
```

Choose either `--mcp-url` or `--server-command`. Plain HTTP is accepted only for
loopback testing. The server owns datasets, budgets, attempts and handoff notes;
the local client retains the model conversation and receipts. Before model calls,
check the endpoint with the read-only [deployment checker](deployment.md).

## Run the offline demo

```bash
pip install '.[mcp]'
python -m assay.demo --output ./demo-workspace
```

The output directory must not already exist. This generates synthetic OHLCV,
executes two factors and a diagnostic failure, restores the session after restart,
and writes `manifest.json` and a portable `report.html`. No model call or vendor
credential is needed. Synthetic prices do not establish investment performance.

## Let a model operate the tools

Prepare a new workspace, then write a text task describing the research goal:

```bash
python -m assay.demo --prepare-only --output ./agent-workspace
export ASSAY_DATA_DIR="$PWD/agent-workspace"
# Set DEEPSEEK_API_KEY through your normal secret manager or shell environment.
python -m assay.agent \
  --base-url https://api.deepseek.com --model deepseek-flash \
  --api-key-env DEEPSEEK_API_KEY --extra-body '{"thinking":{"type":"disabled"}}' \
  --task-file task.txt --output ./agent-run \
  --max-turns 8 --max-tool-calls 16 --max-tokens 4096 \
  --server-command python -m assay.mcp.server
```

For example, `task.txt` can ask the agent to discover the synthetic dataset, create a
session with a three-attempt budget, test reversal hypotheses, inspect failures and
save a handoff note. The client sends only the selected MCP tool schemas and tool
results to the configured provider. Research text and reports may therefore leave
your machine. API keys remain in environment variables and request headers.

`--server-command` must be the last option. Install the MCP extra in the interpreter
used for both the client and server. `--extra-body` supplies provider-specific
settings; it cannot override the model, conversation, tool schema or execution caps.
Returned model identity must match the requested name unless explicit
`--allow-returned-model` values are provided. Changing an alias is not evidence of
model equivalence. Provider availability and protocol support must be verified for
the selected deployment; this client does not make all models interchangeable.

## Inspect the run

- `tools.json`: tool schemas discovered from the server.
- `conversation.json`: the messages and tool outputs sent through the loop.
- `run.json`: requested configuration, actual response identities, raw received
  responses, tool results, timing, reported usage and terminal status.

The client does not retry HTTP failures or follow redirects. Invalid/truncated
responses are retained; they do not execute tool calls. Unknown usage is marked
incomplete. A model response without tool calls ends with `model_finished`, which
means the model stopped, not that it achieved the user's objective. Check persisted
experiments, notes and reports to evaluate completion. The reference client does
not resume a partially recorded conversation; use a new client run and an existing
research session for a deliberate handoff. Session budgets remain server-enforced.

Routine experiment execution returns compact feedback to the model, while receipts
retain the full report. The agent can explicitly request `assay_experiment_get` for
metric series. Session discovery uses compact research context rather than full
session exports. This reduces repeated report payloads without changing scores.

The reference client normalizes structured JSON and a single JSON text block from
MCP before constructing model feedback. Mixed content stays intact. The receipt
keeps the original transport payload separately from `model_result`; MCP failures
reach the model with an explicit `isError` flag. Compaction happens in this optional
client, not in the server: other clients can inspect the full tool response.
