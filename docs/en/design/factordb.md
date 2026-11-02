# FactorDB — a factor store for large-scale, agent-driven research

Status: design + phase 1 (daily) implementation. Scope: Assay on the QFinVerse daily panel
(US), extended to minute bars in phase 2.

## 1. Why

Assay today evaluates a factor by loading a `(dates × symbols)` panel for one universe and
period, walking the expression, and scoring it. Results are cached by
`(expression, universe, period, adjustment)`. That works for one researcher; it does not
work for hundreds of agents proposing tens of thousands of factors:

| Problem | Effect |
|---|---|
| Results keyed by universe and period | `ts_mean(volume, 20)` on SP500 2016–2026 is recomputed for NASDAQ100, for 2018–2026, and for every agent |
| One process, one panel per request | no use of 32 cores; a TOP3000 batch is CPU-bound in one thread |
| float64 everywhere | twice the memory of the source data (float32) |
| Warm-up NaNs at the window start | a 60-day window loses 60 days at the start of every request |
| Agent tools mirror the human API | one blocking call per factor, large payloads, no dedup across agents, no budget |

## 2. Core idea: compute once on a global grid

Every factor expression splits into two kinds of work:

* **Column-separable** work — time-series and element-wise operators (`ts_*`, `+ - * /`,
  `log`, `where`, …). The value for security *s* on date *t* depends only on *s*'s own
  history up to *t*. It does **not** depend on the universe.
* **Row-coupled** work — cross-sectional operators (`cs_rank`, `cs_zscore`, `cs_neutralize`,
  group operators). The value depends on which securities are in the cross-section that day.

FactorDB stores the column-separable part once, on a **global grid**: every trading day of
the store × every security that was ever a member of the widest universe (TOP3000 ∪ index
members). A request for any universe and period slices rows and columns out of that grid,
then applies the row-coupled operators for that universe on the fly. Cross-sectional work
is a single pass per date and is cheap next to rolling windows.

```
cs_rank( ts_corr(close, volume, 20) )
         └──── stored on the grid ────┘   computed per request, on the universe's members
```

Expressions whose time-series operators sit **above** a cross-sectional one
(`ts_mean(cs_rank(x), 5)`) depend on the universe; their nodes are stored per universe
(the grid key includes the universe) and still shared across periods and agents.

### 2.1 Canonical form and identity

Before anything is computed an expression is parsed and canonicalised: commutative
operators (`add`, `mul`, `elem_min`, `elem_max`, `eq`, `ne`, `and`, `or`) sort their
arguments, numeric literals are normalised, and windows are floored as the engine does.
The structural hash of the canonical tree is the **factor id**. Two agents that write
`close/open` and `div(close, open)` get the same id; submitting a known id costs nothing.

### 2.2 Node store

Each stored node is a float32 matrix on the grid, split into **year blocks**
(`nodes/<hash>/<year>.npy`, memory-mapped). Year blocks make appends cheap (only the
current year is rewritten) and reads fast (a 2016–2026 slice touches 11 files, each read
by mmap). A manifest row per node records the canonical expression, the grid, the dates it
covers, bytes, compute cost and hit counts (used for eviction and for agents' "is this
already known" queries).

What gets stored:

* the **frontier** of each factor: the largest column-separable subtrees (the inputs of the
  first cross-sectional operator, or the whole factor if it has none);
* any column-separable subtree used by two or more factors in the store (popularity is
  counted per node).

Leaf fields are not copied; they are read from the QFinVerse panel's own year blocks.

### 2.3 Corporate actions and point-in-time correctness

The QFinVerse panel stores prices as traded and adjusts on read onto the basis of the
window's last day. FactorDB computes grid nodes on the basis of the store's last day *E*.
A request that ends at *E′ < E* must see values on *E′*'s basis. For a node that is
**homogeneous** in prices the two differ by a per-security constant:

* each field has a degree under splits and under dividends: `close/open/high/low` = (1, 1),
  `volume` = (−1, 0), `transactions`, `dvol`, `ret` = (0, 0);
* operators combine degrees (`mul` adds, `div` subtracts, `add/sub/min/max/where` need equal
  degrees, `ts_mean/ts_sum/ts_std/ts_delay/ts_min/ts_max/abs/neg` keep the degree,
  `ts_corr/ts_rank/ts_returns/cs_*/sign/comparisons` give (0, 0), `log/sqrt/pow` of a
  non-zero degree have none);
* a node with degree (a, b) is rescaled per security by `S^a · D^b`, where `S` and `D` are
  the cumulative split and dividend factors between *E′* and *E*.

Most alpha factors have degree (0, 0) and need no rescale. A node without a degree
(`log(close)`) is not served from the grid for a request that ends before *E*; FactorDB
falls back to the direct engine for it.

When the panel publishes a new version FactorDB **appends**: for every node, the new dates
are computed from the inputs over the new dates plus the node's look-back (the sum of its
nested windows; unbounded recurrences such as `ts_ema` are recomputed in full).
Securities with a new split or dividend have their whole column recomputed, because the
new event changes their adjusted history. On a typical day that is a few dozen columns out
of ~9,000.

## 3. Compute: column-sharded and parallel

A batch of expressions becomes one DAG (shared subtrees appear once). Missing frontier
nodes are computed by a pool of worker processes, each owning a slice of the security
axis:

1. the parent allocates the output year blocks as memory-mapped files;
2. each worker maps the input fields (QFinVerse year blocks, already memory-mapped),
   evaluates every missing node of the batch for its columns with an in-worker CSE memo,
   and writes into its column slice of the outputs;
3. nothing large crosses process boundaries; the OS page cache shares the inputs.

Time-series kernels are independent per column, so this scales with cores until memory
bandwidth runs out. Cross-sectional layers and scoring (IC, RankIC, decay, turnover) run
per date in the parent, vectorised.

Precision: inputs are float32; kernels compute in float64 within a worker; stored nodes are
float32. Scores agree with the direct engine to well under 1e-4 in IC.

## 4. Agent-native interface

Designed for many agents sharing one server:

| Principle | How |
|---|---|
| Batch first | submit hundreds of expressions in one call; get a job id |
| Idempotent | factor id = canonical hash; a known factor on a known spec returns its stored score |
| Shared across agents | one node store and one registry; in-flight duplicates join the running job |
| Small, stable payloads | top-k tables with fixed keys; values only on request, sampled |
| Actionable errors | every rejection carries a code, the location in the expression and a hint |
| Novelty built in | correlation against the library computed from stored values |
| Fair use | per-owner job queue and compute budget; one agent cannot starve the rest |
| Provenance | every factor records who proposed it, when, from which parent factors, with tags |

### 4.1 MCP tools (v2)

| Tool | Purpose |
|---|---|
| `fdb_submit` | Evaluate many expressions on a spec (universe, period, horizons). Returns the job id plus immediate verdicts: duplicates, rejects with hints, already-scored factors |
| `fdb_job` | Progress, ETA, the best factors so far, failure summary |
| `fdb_query` | Search scored factors: filters on metrics, tags, owner, novelty; sort; limit |
| `fdb_factor` | One factor: canonical form, scores per spec, diagnostics, lineage |
| `fdb_novelty` | Max correlation of an expression or id against the library, with the nearest factors |
| `fdb_values` | A cross-section snapshot (top/bottom names and values on a date) to sanity-check a factor |
| `fdb_operators` | Operator catalog with signatures, categories and degree rules |
| `fdb_budget` | Compute used and left for the caller |

The MCP server is also served over streamable HTTP at `/mcp` on the Assay site, behind the
same accounts and personal API keys as the REST API, so remote agents (Claude, Codex, …)
connect to one shared FactorDB.

### 4.2 REST

The same operations under `/v1/fdb/*` (`POST /submit`, `GET /jobs/{id}`, `GET /factors`,
`GET /factors/{id}`, `POST /novelty`, `GET /values`, `GET /operators`, `GET /budget`).

## 5. Minute bars (phase 2)

Minute data is two orders of magnitude larger (MASSIVE minute aggregates, ~100 GB of
parquet for 2003–2026). The design reuses the same pieces with the **trading day** as the
unit of parallelism instead of the security:

* a minute field store with one block per day (`390 regular-session bars × members of the
  day`), float32, aligned to the daily grid's security ids;
* intraday expressions are evaluated per day (time axis = bar of the session), so days run
  in parallel with no cross-talk;
* the main use is **intraday features reduced to daily values** — `day_agg(expr, "last" |
  "mean" | "sum" | "std" | …)` — which land in the daily node store and combine freely with
  daily operators (`cs_rank(day_agg(ts_corr(close, volume, 30), "mean"))`);
* full minute-level outputs are stored per day only when a minute backtest asks for them.

## 6. Phases

1. **Daily FactorDB** — canonical ids, global grid, node store, column-sharded parallel
   compute, append and corporate-action invalidation, registry and jobs, REST and MCP v2,
   HTTP MCP behind accounts. Benchmarked against the current engine.
2. **Minute bars** — minute field store builder, per-day intraday evaluation, `day_agg`,
   minute backtests.
3. **Scale-out** — eviction by cost × hits, node store on a shared volume for several
   compute hosts, scheduled precompute of the library's frontier after each panel update.
