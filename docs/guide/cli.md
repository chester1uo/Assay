# CLI Reference

All commands run as `python -m assay.cli <subcommand>` (or `assay <subcommand>` once
`pip install -e .`'d — the console scripts are `assay` and `assay-data`). Commands resolve
config from the environment / `.env`; only data-touching commands need MASSIVE credentials.

## Data pipeline

| Command | Purpose |
|---|---|
| `discover` | List MASSIVE flat-file prefixes (connectivity sanity check) |
| `prepare-nasdaq100 --start S --end E` | Full prepare: universe → corp-actions → prices. Flags: `--skip-universe/-corp-actions/-prices` |
| `universe --index NASDAQ100 --start S --end E` | Build `universe_snapshots` |
| `corp-actions --start S --end E [--symbols A,B]` | Fetch splits & dividends |
| `prices --start S --end E [--symbols A,B]` | Download & normalize day aggregates |
| `status` | Show ingested rows, date range, partitions |
| `verify --start S --end E [--adj split]` | Read a PIT panel and print a summary |

## Factor engine

| Command | Purpose |
|---|---|
| `parse '<expr>'` | Parse a factor expression → dialect, AST, fields, operators (no data needed) |
| `eval '<expr>' --start S --end E [--as-of D] [--adj split] [--index NASDAQ100]` | Evaluate the factor matrix over the PIT panel; print coverage + top/bottom names |

## Backtest & library (SDK-backed)

| Command | Purpose |
|---|---|
| `run '<expr>' --start S --end E [...]` | Full scored `FactorReport` summary (rank_ic, rank_icir, decay, failure_mode, suggestion) |
| `batch <file|exprs...> --start S --end E [--output results.parquet]` | Batch-evaluate (one expr per line in a file; `#` comments skipped), sorted by rank_icir |
| `report <factor_id>` | Pretty-print a saved factor from the library |
| `library list/get/delete/prune [...]` | Manage the factor library (e.g. `library list --sort rank_icir --limit 20`) |
| `portfolio '<expr>' --start S --end E [--rebalance monthly] [--weight-method signal_prop] [--market US] [--long-short] [--output pf.json]` | Run a portfolio backtest; print total/annual return, Sharpe, max drawdown, turnover, cost drag |

## Servers

| Command | Purpose |
|---|---|
| `serve-api [--host 0.0.0.0] [--port 8000]` | Run the FastAPI REST API + WebUI (uvicorn) |
| `serve-mcp [--transport stdio|sse|http] [--port 8001]` | Run the MCP server for LLM agents |

## Examples

```bash
# prepare + inspect
python -m assay.cli prepare-nasdaq100 --start 2025-01-01 --end 2026-06-09
python -m assay.cli status

# evaluate
python -m assay.cli parse 'cs_rank(ts_corr(close, volume, 20))'
python -m assay.cli run   'cs_rank(ts_corr(close, volume, 20))' --start 2025-01-02 --end 2026-06-09

# batch from a file
printf 'ts_returns(close, 20)\nts_corr(close, volume, 20)\n' > factors.txt
python -m assay.cli batch factors.txt --start 2025-01-02 --end 2026-06-09 --output out.parquet

# portfolio
python -m assay.cli portfolio 'cs_rank(ts_returns(close, 20))' \
    --start 2025-01-02 --end 2026-06-09 --rebalance monthly --weight-method signal_prop

# serve
python -m assay.cli serve-api --port 8000        # http://localhost:8000
```

Add `-v` before any subcommand for debug logging.
