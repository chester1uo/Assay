# WebUI

A **zero-install, zero-build** web app served directly by the FastAPI backend — vanilla JS +
hand-rolled SVG charts, no framework, no bundler, no CDN. Lives at
[`src/assay/api/static/`](../../src/assay/api/static/).

> The production React/Vite/Recharts/Monaco stack in [the WebUI design doc](../design/webui.md)
> is the documented target; this shipped UI is the runnable equivalent (npm isn't available in
> the build environment).

## Run

```bash
python -m assay.cli serve-api --port 8000
# open http://localhost:8000
```

It auto-adopts the ingested data range as the default evaluation period (from
`/v1/system/status`), so factors evaluate over dates that actually have data. Hard-refresh
after upgrading to pick up new assets.

## Screens

- **Dashboard** — data status bar, KPI cards, factor leaderboard, data-calendar heatmap.
- **Factor Library** — filterable list, factor detail, correlation-matrix heatmap + prune preview.
- **Single Factor Test** — a lightweight expression editor with **live lint/AST** (via
  `POST /v1/factor/lint`), a qlib↔Python convert button, config row, and **progressive
  SSE-streamed charts** (IC time series, decay, group returns, IC heatmap) plus a summary panel
  with save-to-library.

## Usage tips

- Evaluating needs ingested data. With a short window factor (e.g. `cs_rank(close)`) you'll see
  charts immediately; a 20-day-window factor needs ≥ 20 days of history or it's all-NaN (the UI
  shows the diagnostic rather than failing silently).
- Optional API key: stored in `localStorage.assay_api_key` and sent as `X-API-Key` (only needed
  if the server sets `ASSAY_API_KEYS`).

## Not wired (no backend)

The live agent feed, Alpha-Space UMAP map, Lineage DAG, and IC-heatmap library mode are gated
as "not available yet" rather than faked — those need backend endpoints / data that don't exist.
