# MCP Server

The MCP server exposes Assay's evaluate→library loop as Model Context Protocol tools that LLM
agents call to drive autonomous alpha mining. Built on the high-level **FastMCP** API
([`src/assay/mcp/server.py`](../../../src/assay/mcp/server.py)).

## Run

```bash
python -m assay.mcp.server                          # stdio (Claude Desktop / local agents)
python -m assay.cli serve-mcp --transport sse --port 8001   # SSE/HTTP (remote agents)
```

The module imports with no credentials; the data store under the service is built lazily, so
only tools that touch price data need MASSIVE creds.

## Tools (11)

| Tool | Purpose |
|---|---|
| `assay_evaluate` | Evaluate one factor → `FactorReport` (qlib or Python syntax) |
| `assay_batch` | Evaluate many factors in parallel, sorted by `rank_icir` (prefer over a loop) |
| `assay_lint` | Data-free syntax check of an expression (dialect, fields, operators, diagnostics) |
| `assay_universes` | List available universes with symbol counts and default |
| `assay_portfolio_backtest` | Full portfolio backtest of a factor → compact `PortfolioReport` |
| `assay_library_list` | List library factors with filters (check redundancy before generating) |
| `assay_library_get` | Full `FactorReport` for one factor |
| `assay_library_save` | Save a report to the library |
| `assay_library_correlation` | Pairwise correlation between factor IDs |
| `assay_library_prune` | Identify / remove redundant factors |
| `assay_system_status` | Data freshness + cache statistics |

`assay_evaluate`'s description is enriched at runtime with the live operator schema
(`assay.engine.operator_schema()`), so agents see the available operators inline.

## Claude Desktop config

`~/.config/claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "assay": {
      "command": "python",
      "args": ["-m", "assay.mcp.server"],
      "env": { "ASSAY_DATA_DIR": "data_2025_2026h1" }
    }
  }
}
```

## Typical agent loop

```
assay_library_list(limit=10)         → what high-quality factors already exist?
assay_batch([...candidate exprs...]) → sorted results; top factor's rank_icir / redundancy
assay_library_correlation([...])     → is the new factor redundant with existing ones?
assay_library_save(best_report)      → keep the unique, high-ICIR factor
assay_library_prune(dry_run=true)    → propose removing dominated factors
```

Design contract: [architecture.md](../design/architecture.md) §6.
