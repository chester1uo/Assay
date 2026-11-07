# Research workbench

Open `/#/research` on an Assay Web API server. The page reads the same durable
research workspace as MCP and the SDK. It shows the research objective, stored
experiment activity, parent/replay relationships, full evidence and handoff notes.
It does not invent an agent trace or send the browser's objective to an LLM.

## Local synthetic walkthrough

From the repository root, in an environment with Assay's API and MCP extras:

```bash
pip install '.[api,mcp]'
python -m assay.demo --prepare-only --output ./workbench-data
export ASSAY_DATA_DIR="$PWD/workbench-data"
python -m uvicorn assay.api.app:app --host 127.0.0.1 --port 8014
```

Visit `http://127.0.0.1:8014/#/research`. The output directory must be new.
The dataset is generated synthetic OHLCV, not historical market evidence.

1. Open **Start a new session**, choose the synthetic dataset, enter a research
   goal and an attempt budget, and create the session.
2. Open **Evaluate a candidate**. Enter a hypothesis and the intentionally invalid
   expression `unknown_operator(close)`. Its failure is saved and consumes one
   attempt, matching the MCP experiment contract.
3. Select that failed attempt as the revision parent. Submit
   `-cs_rank(ts_returns(close,5))` with a revised hypothesis. Inspect its actual
   scores and the parent relationship.
4. Save a handoff note and download the manifest. Reload the page to verify that
   the persisted research remains available.

All browser-created sessions, experiments and notes are attributed to
`human:webui`. This walkthrough is a real evaluator demonstration driven by
human actions; it is not autonomous model evidence. A completed evaluation does
not establish profitability or validate the accompanying hypothesis.

## Observe an agent session

Run an MCP agent against the same `ASSAY_DATA_DIR`, following the reference-agent
guide. Select its research session in the workbench. Auto-refresh polls saved
state every three seconds; **Refresh sessions** also discovers newly created
sessions. Disabling auto-refresh stops polling after any in-flight read.
The page does not start, pause, or resume the agent's conversation. Experiment
records are distinct from a complete model/tool execution trace.

Click an experiment to inspect the stored payload. Parent relationships are
clickable; external replay references remain labeled as references. The dataset
configuration belongs to the research session. The top bar's legacy factor-page
universe/period selectors are hidden on this page.

## Request and access behavior

Research REST routes live under `/v1/research` and use the existing API-key
configuration. When `ASSAY_API_KEYS` is set, the Web UI uses its existing
`assay_api_key` local-storage header mechanism. There is no new authorization
bypass for reading research data or downloading a manifest.

Evaluation runs in the API's thread pool; concurrent reads can display a running
attempt. Retrying unchanged inputs after a transport failure retains the same
request ID while the page is open. A returned `running` record retains that ID
until a later refresh observes a terminal state. Reloading discards client retry
state: inspect persisted attempts before submitting again. There is no automatic
worker takeover or durable background-job scheduler in this UI.

## Browser regression

With a server using synthetic data, install Playwright and a compatible Chromium
in a separate test environment, then run:

```bash
python scripts/check_research_workbench_browser.py \
  --base-url http://127.0.0.1:8014 --output artifacts/workbench-browser
```

The test saves a real workflow screenshot and manifest, verifies its integrity,
and imports the downloaded file into a fresh workspace. Downloads preserve the
server JSON text so numeric representations covered by the hash remain intact. Separate mock-transport
fixtures test pending-request idempotency and slow-list selection races; those
fixtures are never presented as real model or market experiments.
