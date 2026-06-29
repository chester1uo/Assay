---
name: factor-mining
description: Methodology and toolset for mining, testing and reporting quantitative alpha factors with the Assay MCP server. Use when an agent is asked to discover, evaluate, validate, or report on quant factors (alpha expressions) for equities (US NASDAQ100/SP500 or A-share CSI300/500/1000) — generating candidate expressions, measuring IC/RankIC/ICIR/decay/turnover, checking redundancy, running portfolio backtests for achievable net return, and writing a defensible factor report.
---

# Factor Mining with Assay

Assay is an in-process quant factor research engine exposed to agents over an **MCP
server** (`assay`). Every tool runs the *same* `AssayService` the analyst UI uses, so
an agent's numbers are identical to a human's. This skill is the methodology: how to
generate, **test**, validate and **report** factors rigorously.

## Connecting the MCP server

The host must have the `assay` MCP server registered. To run it:

```bash
# stdio (local agents / Claude Desktop) — default
PYTHONPATH=src python -m assay.cli serve-mcp
# or remote:
PYTHONPATH=src python -m assay.cli serve-mcp --transport sse --port 8001
```

Data dirs come from the environment: `ASSAY_DATA_DIR` (primary, e.g. US) and
`ASSAY_DATA_DIR_CN` / `ASSAY_DATA_DIR_HK` for extra markets. Library/cache are shared.

## The toolset (11 tools)

| Tool | Use it to |
|---|---|
| `assay_universes` | **Discover** which pools are live (US + A-share) and their symbol counts. Call FIRST. |
| `assay_lint` | **Validate syntax cheaply** (no data) before evaluating — catches typos, unknown fields, lookahead risk. |
| `assay_evaluate` | Evaluate ONE expression → full `FactorReport` (IC/RankIC/ICIR/decay/turnover/redundancy/lookahead). |
| `assay_batch` | Evaluate MANY expressions in parallel over one shared panel — **always prefer over looping evaluate**. |
| `assay_portfolio_backtest` | The **deep** test: achievable NET return after costs/constraints (Sharpe, drawdown, turnover, cost drag; A-share ±limit + T+1). |
| `assay_library_list` | See factors already known (avoid re-discovering); filter by quality / source / redundancy. |
| `assay_library_get` | Full report for one stored factor_id. |
| `assay_library_save` | Persist a good factor (pass the report dict verbatim). |
| `assay_library_correlation` | Pairwise signed-rank correlation between factor_ids — is a new factor redundant? |
| `assay_library_prune` | Find/remove redundant factors (keeps the higher-RankICIR of each correlated pair). |
| `assay_system_status` | Library size, cache hit-rate, defaults. |

## The mining loop

1. **Discover** — `assay_universes`. Pick a data-backed pool (n_symbols > 0). Note its market.
2. **Survey priors** — `assay_library_list` (and the `ALPHA101` / `ALPHA158` demo sources) so you don't re-mine known alphas.
3. **Generate** candidate expressions (see Syntax). Think in economic hypotheses (reversal, momentum, volume-price divergence, volatility), not random operator soup.
4. **Lint** — `assay_lint` each candidate; drop the ones with errors before spending compute.
5. **Evaluate in bulk** — `assay_batch` the survivors over the chosen universe/period. Sort by `rank_icir`.
6. **Interpret** the reports (see Reading a report). Discard degenerate / lookahead / failed factors.
7. **De-dup** — for promising candidates, `assay_library_correlation` against the existing library; keep only genuinely new bets (|corr| < ~0.7).
8. **Stress test the winners** — `assay_portfolio_backtest` your top few. IC is necessary but NOT sufficient; a factor must survive turnover + trading costs. This is where most "good IC" factors die.
9. **Save** survivors with `assay_library_save` (tag the source).
10. **Report** to the user (see Reporting).

## Expression syntax

Two dialects parse to the same factor — use either:
- **qlib**: `$close`, `Ref($close,5)`, `Mean($close,20)`, `Corr($close,$volume,20)`, `Rank($close,20)` (2-arg = time-series rank), `Greater(a,b)`, `If(cond,a,b)`, `Slope`/`Resi`/`Rsquare`/`Quantile($close,20,0.8)`.
- **Python**: `close`, `ts_delay(close,5)`, `ts_mean(close,20)`, `ts_corr(close,volume,20)`, `cs_rank(x)` (cross-sectional), `cs_zscore`, `cs_neutralize(x,g)`.

Prefixes: `ts_` = time-series (within a symbol over time), `cs_` = cross-sectional
(across symbols on one day). Fields available in the daily store: **open, high, low,
close, volume, transactions**. Use `assay_lint` to confirm; the live operator
vocabulary is embedded in the `assay_evaluate` tool description.

## Reading a FactorReport

- **rank_ic** — Spearman corr of factor vs forward return. For a single price/volume
  daily factor, |rank_ic| ≈ 0.02–0.03 is decent, > 0.05 is strong. Sign = direction
  (negative ⇒ inverse signal; short it / flip the sign).
- **rank_icir** — RankIC / its volatility = consistency. The primary ranking metric.
  Higher is better; sign follows rank_ic.
- **ic / icir** — Pearson analogues; rank metrics are more robust.
- **decay_halflife_days** — how fast the edge decays. Short (<10d) ⇒ fast alpha, needs
  frequent rebalancing (more cost). Long (>30d) ⇒ slow, stable, cheaper to harvest.
- **turnover_1d** — daily name churn; high turnover ⇒ high trading cost.
- **redundancy_score** — similarity to the existing library: <0.4 unique, 0.4–0.7
  similar, >0.7 redundant.
- **lookahead_detected** — MUST be false. A true here means the factor peeks at the
  future; discard it, the IC is fake.
- **failure_mode** — non-null ⇒ the factor did not evaluate (e.g. `RUNTIME_ERROR` for
  `vwap`/`cap` which have no data, `CONSTANT`, `SYNTAX_ERROR`). Not a real result.

### Traps to avoid
- **Degenerate near-constant factors**: a factor that is almost always the same value
  (e.g. `Gt($high,$low)` ≈ always 1) can show a huge |ICIR| with a tiny RankIC and no
  decay half-life — it's a variance artifact, NOT signal. Trust RankIC + a real decay,
  not ICIR alone.
- **`vwap` / `cap` (market_cap)**: these parse but have NO data in the OHLCV store, so
  any factor using them fails at evaluate time. Lint won't block them; evaluate will
  fail with RUNTIME_ERROR. Many WorldQuant Alpha101 use them.
- **Cross-sectional ops need a cross-section**: `cs_rank` etc. are meaningless on a
  single symbol; they're for universe-wide evaluation.
- **IC ≠ profit**: always confirm with `assay_portfolio_backtest`. A 0.05 RankIC factor
  with 1.5 daily turnover can be net-negative after A-share stamp duty + commission.

## Deep performance testing (portfolio backtest)

`assay_portfolio_backtest(expr, universe, period, rebalance, weight_method, long_short)`
turns the signal into an achievable strategy and returns:
- **sharpe / sortino / calmar**, **annual_return / total_return**, **excess_return** vs benchmark.
- **max_drawdown**, **annual_turnover**, **cost_drag** (return lost to costs), **avg_holding_days**.
- **beta / alpha_capm / tracking_error**.
- **a_share_metrics** (CSI* only): limit-hit rate, suspension blocks, forced-hold ratio.

Rules baked in: A-share universes (CSI*) auto-enforce ±10%/20% price limits, T+1
settlement and are long-only (融券 not modelled); costs use the market preset
(commission / stamp duty / transfer fee / slippage). `rebalance`:
daily|weekly|monthly|quarterly; `weight_method`: equal|signal_prop|quintile.

A factor "passes" when, after costs: Sharpe is meaningfully positive (≈ >1 is good for
a single factor), excess_return > 0, and turnover/cost_drag aren't eating the edge.

## Reporting (what to hand the user)

For each recommended factor report, in plain language:
1. **Expression** (both the canonical form and an economic interpretation — what bet is it?).
2. **Signal quality**: RankIC, RankICIR, decay half-life, with the direction (long/short).
3. **Uniqueness**: redundancy_score / nearest existing factor.
4. **Net performance**: portfolio Sharpe, annual & excess return, max drawdown, turnover, cost drag (and A-share limit/suspension stats if relevant) — i.e. does it survive costs.
5. **Caveats**: failure modes seen, lookahead status, period/universe tested on, and any degeneracy you ruled out.

Rank recommendations by **risk-adjusted, cost-aware** performance (portfolio Sharpe /
RankICIR), not raw IC. Be explicit when a factor is a strong *inverse* signal (negative
RankIC) — it's still tradable by flipping the sign. Never present an unvalidated factor
(failure_mode set, or lookahead detected) as a result.
