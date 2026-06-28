# Assay

Assay is a high-performance, point-in-time-correct factor backtesting engine built for LLM
agent-driven alpha mining. On top of a shared engine it exposes four surfaces — a Python SDK,
a REST API (with SSE streaming), an MCP server for agents, and a zero-install WebUI — plus a
full **portfolio backtest** module.

## 📚 Documentation

Full design specs and usage guides live in **[docs/](docs/README.md)**:

- **Start here:** [Getting Started](docs/guide/getting-started.md)
- **Guides:** [Data Pipeline](docs/guide/data-pipeline.md) · [Python SDK](docs/guide/python-sdk.md) ·
  [CLI](docs/guide/cli.md) · [REST API](docs/guide/rest-api.md) · [MCP](docs/guide/mcp-server.md) ·
  [WebUI](docs/guide/webui.md) · [Portfolio Backtest](docs/guide/portfolio-backtest.md) ·
  [Performance](docs/guide/performance.md)
- **Design:** [Engineering](docs/design/engineering.md) · [Architecture](docs/design/architecture.md) ·
  [Operator table](docs/design/operator-compatibility.md) · [Portfolio](docs/design/portfolio-backtest.md)

The rest of this README is a focused tour of the **data layer** and the **factor execution
engine** (the foundation); everything above the engine is covered in the guides.

---

## What the pipeline does

```
local MASSIVE mirror      ─┐
  day_aggs_v1/*.parquet    │   read            normalize + PIT          read (PIT)
  corporate_actions/*.jsonl├─►  readers  ──►   ingesters  ──►  parquet  ──►  DataStore.get_panel
NASDAQ-100 history (YAML) ─┘   (local, no network)            stores
```

Three parquet stores are produced under `$ASSAY_DATA_DIR` (default `./data`),
matching the schemas in the engineering docs §3.2:

| Store | Path | Contents |
|---|---|---|
| `price_raw` | `price_raw/market=US/year=Y/month=M/price_raw.parquet` | unadjusted daily OHLCV (+ transactions) |
| `adj_events` | `adj_events/market=US/adj_events.parquet` | splits, reverse-splits, mergers, dividends |
| `universe_snapshots` | `universe_snapshots/market=US/universe_snapshots.parquet` | NASDAQ-100 membership history |

Corporate actions (splits / merges / dividends) are applied **at read time** by
[`DataStore.get_panel`](src/assay/data/store/datastore.py), so the stored prices
stay raw and any point-in-time slice can be reproduced exactly.

---

## Setup

```bash
pip install -r requirements.txt          # or: pip install -e .
```

### Local data source

The pipeline reads a **local mirror** of the MASSIVE dataset (downloaded
out-of-band by the `downloader_*` scripts) and transforms it into the parquet
stores — it does **not** download anything, so no credentials are needed.

| Variable | Purpose |
|---|---|
| `MASSIVE_DATA_DIR` | root of the local MASSIVE mirror (default `/data/massive_data`) |
| `ASSAY_DATA_DIR` | output root for the prepared stores (default `./data`) |

Expected source layout under `MASSIVE_DATA_DIR`:

```
us_stocks_sip/day_aggs_v1/{YYYY}/{MM}/{YYYY-MM-DD}.parquet   # daily OHLCV bars
corporate_actions/splits/{TICKER}.jsonl                      # one split record per line
corporate_actions/dividends/{TICKER}.jsonl                   # one dividend record per line
```

Copy `.env.example` to `.env` to override the defaults — `assay.config` loads
`.env` automatically (the shell environment always wins). Both `.env` and
`data/` are git-ignored.

---

## Usage

### CLI

```bash
# Prepare the full NASDAQ-100 dataset (universe + corporate actions + prices)
python -m assay.cli prepare-nasdaq100 --start 2023-01-01 --end 2024-12-31

# Individual stages
python -m assay.cli universe     --index NASDAQ100 --start 2023-01-01 --end 2024-12-31
python -m assay.cli corp-actions --start 2023-01-01 --end 2024-12-31
python -m assay.cli prices       --start 2023-01-01 --end 2024-12-31

# Inspect / verify
python -m assay.cli status
python -m assay.cli verify  --start 2024-06-01 --end 2024-06-30 --adj split
python -m assay.cli discover           # show the local MASSIVE source layout
```

Run from source with `PYTHONPATH=src python -m assay.cli ...`, or after
`pip install -e .` use the `assay-data` entry point. There is also a thin
wrapper: `python scripts/prepare_nasdaq100.py 2023-01-01 2024-12-31`.

### Python API

```python
from assay.config import AssayConfig
from assay.data.pipeline import prepare_nasdaq100
from assay.data.store import DataStore

cfg = AssayConfig.from_env()
prepare_nasdaq100(cfg, date(2024, 1, 1), date(2024, 12, 31))

store = DataStore(cfg)
universe = store.get_universe("NASDAQ100", date="2024-06-28", as_of_date="2024-06-28")
panel = store.get_panel(
    fields=["close", "volume"],
    symbols=universe,
    start_date="2024-01-01", end_date="2024-06-28",
    as_of_date="2024-06-28",   # required — point-in-time correctness
    adj="split",               # none | split | total (alias: forward)
)
```

---

## Point-in-time correctness

Every read takes a required `as_of_date`. The store only uses rows knowable then:

* **Prices** — `as_of_date` = the trading date (EOD bar for day *d* is known at
  the close of *d*).
* **Splits** — `as_of_date` = execution date.
* **Dividends** — `as_of_date` = declaration date (falls back to ex-date).
* **Universe** — `as_of_date` = effective date.

### Adjustment (`adj`)

Forward adjustment rescales history onto the **most recent date's basis** (latest
bar unchanged). A split with forward ratio `r = split_to/split_from` divides
every price *strictly before* its ex-date by `r` (the ex-date bar already
reflects the split); volume is multiplied by `r`. Reverse splits (`r<1`) and
merger share-changes use the same ratio mechanism. In `total` mode, each cash
dividend `D` additionally multiplies prices before its ex-date by
`1 − D/close_prev` (raw prior close). Modes: `none`, `split` (default), `total`.

The provider's `historical_adjustment_factor` is stored for cross-checking but
**not** used for the math, so the adjustment is computed only from events known
as-of the query date (no leakage from future actions).

---

## NASDAQ-100 history

Point-in-time membership is modelled on
[jmccarrell/n100tickers](https://github.com/jmccarrell/n100tickers): per-year
YAML files (vendored under
[src/assay/data/universe/data/](src/assay/data/universe/data/)) holding
`tickers_on_Jan_1` plus dated `union`/`difference` changes.

```python
from assay.data.universe import nasdaq100
nasdaq100.tickers_as_of(2020, 9, 1)          # frozenset of members on a date
nasdaq100.union_over_range(start, end)        # every ticker ever a member (no survivorship bias)
nasdaq100.membership_snapshots(start, end)    # PIT snapshots for the universe store
```

The download universe for a backtest is the **union** of all members over the
range, so de-listed / removed names are still fetched.

---

## Factor engine

The engine ([src/assay/engine/](src/assay/engine/)) parses a factor expression
into a unified AST and evaluates it over a PIT panel. Two front-end syntaxes
lower to the *same* AST and operator backend — see
[docs/design/operator-compatibility.md](docs/design/operator-compatibility.md) for the full operator table:

* **qlib** — `$`-fields, CamelCase ops: `Corr($close, $volume, 20)`
* **function-call** — Assay-native `ts_*`/`cs_*` *and* Alpha-101 / WorldQuant
  spellings (`delay`, `correlation`, `rank`, `decay_linear`, `SignedPower`, the
  `adv{d}` macro, the `? :` ternary, `^` power, `||` logical-or):
  `ts_corr(close, volume, 20)`

```python
from assay.engine import FactorEngine, parse

# Equivalent expressions in either dialect produce the same tree:
assert parse("Corr($close, $volume, 20)").struct_hash() \
    == parse("ts_corr(close, volume, 20)").struct_hash()

eng = FactorEngine(panel)        # panel: long (date, symbol, *fields) frame
res = eng.evaluate("cs_rank(ts_corr(close, volume, 20))")
res.values                       # (T, N) numpy matrix on aligned date/symbol axes
res.to_frame()                   # long (date, symbol, factor) DataFrame
```

Build the panel straight from the store with `FactorEngine.from_store(store,
universe, period, as_of)`. Group operators (`cs_neutralize`, `cs_group_rank`,
`cs_group_mean`) take per-symbol labels via `group_data=` (sector data is not
part of the Phase-1 store, so these raise a clear error without it).

```bash
# Parse only — show dialect, struct hash, fields, operators (no data needed)
python -m assay.cli parse 'cs_rank(ts_corr($close, $volume, 20)) - 0.5'

# Evaluate over the prepared PIT panel and summarise the factor
python -m assay.cli eval 'ts_returns(close, 20)' --start 2024-01-01 --end 2024-06-14
```

### Custom operators

Operator kernels live in the [src/assay/engine/operators/](src/assay/engine/operators/)
package, one module per category (`time_series`, `cross_sectional`, `math_ops`,
`arithmetic`) over a shared [registry](src/assay/engine/operators/registry.py).
Register your own with the `@op` decorator (or `register(...)`) — the parser
resolves any registered name, so it's usable in expressions immediately:

```python
import numpy as np
from assay.engine import operators as ops, FactorEngine

@ops.op("ts_zscore", 2, 2, category="custom", output_range="(-inf, inf)")
def ts_zscore(x, d):                      # x is a (T, N) matrix; d is the literal arg
    return (x - ops.ts_mean(x, d)) / ops.ts_std(x, d)

FactorEngine(panel).evaluate("cs_rank(ts_zscore(close, 20))")   # just works
```

The kernel gets `(T, N)` matrices for array operands and python scalars for
literal params. Pass `needs_ctx=True` to also receive the evaluation context as
`ctx` (group operators resolve labels via `ctx.require_groups(name)`).
`unregister(name)` removes one; `operator_schema()` returns a live schema view
including custom ops.

### Diagnostics (for the agent mining loop)

`FactorEngine.diagnose(expr)` never throws — it runs the whole pipeline and
returns structured [`FactorDiagnostics`](src/assay/engine/diagnostics.py) with
stable error codes, the location in the expression, and an actionable suggestion
per problem, across three stages:

* **parse** (`ASSAY-P###`) — syntax, unknown operator/variable, wrong arity, bad window/argument
* **execute** (`ASSAY-E###`) — unknown field, missing group data, a kernel raised, bad parameter
* **output** (`ASSAY-O###`) — the series is suspect: all-NaN, high-NaN, no cross-sectional variance, infinities, extreme magnitudes, excessive warm-up, low coverage

```python
fd = FactorEngine(panel).diagnose("ts_corr(close, volume)")
fd.ok            # False
fd.failure_mode  # "SYNTAX_ERROR"  (matches FactorReport §7.2)
print(fd)        # human-readable, with a caret under the offending token
fd.to_dict()     # JSON for the LLM agent — see below
```

```json
{"code": "ASSAY-P007", "name": "OPERATOR_ARITY", "severity": "error", "stage": "parse",
 "message": "operator 'ts_corr' takes 3 argument(s), got 2",
 "location": {"start": 0, "end": 7, "snippet": "ts_corr(close, volume)\n^^^^^^^"},
 "suggestion": "Pass exactly the arguments the operator's signature expects."}
```

On success `fd.result` holds the `FactorResult` and `fd.stats` reports
coverage / NaN-fraction / warm-up / dispersion. Use `lint(expr)` for panel-free
syntax checks. The full code catalog is `assay.engine.diagnostics.CATALOG`.

### The 101 Formulaic Alphas

All 101 alphas from [Kakushadze (2016), arXiv:1601.00991](https://arxiv.org/abs/1601.00991)
are catalogued verbatim as Assay expressions in
[src/assay/factors/alpha101.py](src/assay/factors/alpha101.py):

```python
from assay.factors.alpha101 import ALPHA_101, INDNEUTRALIZE_ALPHAS
from assay.engine import FactorEngine

eng = FactorEngine(panel, group_data={"sector": ..., "industry": ..., "subindustry": ...})
report = eng.evaluate(ALPHA_101[58])   # indneutralize alphas need group_data
```

Fidelity notes: non-integer day-counts are floored (`floor(d)`, per the paper);
`adv{d}` maps to `ts_mean(volume, d)` (share volume — the paper means dollar
volume); `signedpower` is sign-preserving while `^` is plain `pow`. The 18
`indneutralize` alphas (`INDNEUTRALIZE_ALPHAS`) require sector/industry/
subindustry `group_data`. See [tests/factors/test_alpha101.py](tests/factors/test_alpha101.py).

> **Scope.** This is the cold single-factor path: parse → evaluate. The IC /
> RankIC / decay evaluation, `FactorReport`, factor library, `AssayService` + SDK,
> REST/MCP/WebUI surfaces, and the portfolio backtest are all built on top of these
> kernels — see the [docs guides](docs/README.md). (Batch DAG/CSE execution and the
> L1 incremental cache remain the open performance items.)

---

## Tests

Tests are grouped by module under `tests/` (mirroring the source packages):

```
tests/data/      data layer — loaders, corporate-action adjustment, universe, DataStore PIT
tests/engine/    factor engine — parser, operators, dialects, computation, custom ops, diagnostics
tests/factors/   factor catalogs — the 101 Formulaic Alphas
```

A single entry point runs the whole suite or any module/group (it sets
`PYTHONPATH=src` for you and passes extra pytest args through):

```bash
python scripts/run_tests.py            # whole suite
python scripts/run_tests.py engine     # one group  (== pytest -m engine == pytest tests/engine)
python scripts/run_tests.py diagnostics # one module (tests/**/test_diagnostics.py)
python scripts/run_tests.py engine -k corr -x   # extra args pass through
```

Equivalent raw pytest (each test is auto-marked by its folder):

```bash
PYTHONPATH=src python -m pytest tests                # all groups
PYTHONPATH=src python -m pytest tests/engine        # by path
PYTHONPATH=src python -m pytest -m data             # by marker
```
