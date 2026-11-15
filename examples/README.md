# Synthetic MCP research walkthrough

This walkthrough runs Assay locally on generated OHLCV data. A scripted MCP
client creates a research session, evaluates a factor, records a failed attempt
and a handoff note, and saves a search checkpoint. It then starts a new MCP
server process, restores the session and checkpoint, evaluates a revision, and
compares the two completed attempts. It does not call an LLM or a market-data
provider. Results are synthetic and do not measure trading performance.

## Install and run

Use Python 3.10 or later in a new environment, with Git and internet access for
installation. The example was verified with Python 3.12. From a checkout of this
repository:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install '.[api,mcp,perf]'
python -m assay.demo --prepare-only --output ./demo-workspace
python examples/mcp_research_walkthrough.py \
  --data ./demo-workspace --output ./mcp-walkthrough.json
```

Both output paths must be new. The second command produces a JSON trace with
session, experiment, checkpoint, and comparison records. Its internal checks
verify the retained success, failure, note, budget, checkpoint, and parent link
across the server restart. Each run creates a fresh session; use a new JSON path
to rerun it on the same synthetic workspace.

## Inspect the workbench

In the same environment and checkout, start the local web server:

```bash
export ASSAY_DATA_DIR="$PWD/demo-workspace"
assay serve-api --host 127.0.0.1 --port 8000
```

Open <http://127.0.0.1:8000/#/research>, select the session named
**Explore synthetic short-horizon reversal**, and inspect its experiments and
handoff note. The workbench and MCP server read the same research workspace.
The local walkthrough requires no Assay account or API key. Keep this local
server bound to `127.0.0.1`; authentication and configuration are required for
public deployments.

To connect an MCP-compatible client instead of the scripted walkthrough, run
`python -m assay.mcp.server` with `ASSAY_DATA_DIR` set to the prepared workspace.
A model-powered client additionally needs its own model provider credentials.
See [Reference agent and offline demo](../docs/en/guide/reference-agent.md).
