"""MCP server for Assay — the agent-facing surface (architecture §6).

This module exposes Assay's evaluate → library loop as Model Context Protocol
tools that an LLM agent can call directly. It is the primary interface for
agent-driven alpha mining: the same :class:`~assay.service.AssayService`
singleton that backs the SDK / REST API also backs every tool here, so a factor
evaluated by an agent produces the *identical* :class:`FactorReport` an analyst
would get from the Python SDK (architecture §1, "one in-process service").

Design (architecture §6):
- High-level :class:`mcp.server.fastmcp.FastMCP` (decorator tools; supports the
  ``stdio`` default plus SSE / streamable-HTTP for remote agents — §6.1).
- Eight tools (§6.2) cover the full loop: evaluate, batch, and the five library
  operations plus a system-status probe.
- Tool descriptions are **agent-actionable**: they spell out the dual qlib /
  Python syntax and steer the agent toward ``assay_batch`` over a serial
  ``assay_evaluate`` loop (§6.7 typical agent loop).
- ``assay_evaluate``'s description is enriched at import time with the live
  operator schema from :func:`assay.engine.operator_schema` (§6.6) so the agent
  has the full operator vocabulary inline without a second round-trip.

The :class:`AssayService` is fetched lazily through :func:`get_service`, which
catches the "not initialized" error and bootstraps from the environment. The
:class:`DataStore` underneath is itself built lazily, so **importing this module
needs no MASSIVE credentials** — only tools that actually touch price data do.

House style: ``from __future__ import annotations``, dataclasses/type hints,
numpy ``(T, N)`` float64 core upstream, polars frames, NaN-aware throughout.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from mcp.server.fastmcp import FastMCP

from assay.engine import operator_schema
from assay.service import AssayService

__all__ = ["server", "mcp", "get_service", "main"]


# ---------------------------------------------------------------------------
# Service access — lazy, credential-free import
# ---------------------------------------------------------------------------
def get_service() -> AssayService:
    """Return the live :class:`AssayService`, bootstrapping it on first use.

    Mirrors :func:`assay._service`: try the process-wide singleton and, if it was
    never initialised (``RuntimeError`` from :meth:`AssayService.get`), build one
    from the environment / project ``.env`` via :func:`assay.init`. The underlying
    :class:`DataStore` stays lazy, so library-only tools still work without
    MASSIVE credentials; data-touching tools surface a clear error if creds are
    missing.
    """
    try:
        return AssayService.get()
    except RuntimeError:
        # Imported here, not at module load, so this module imports offline-safe.
        import assay

        return assay.init()


# ---------------------------------------------------------------------------
# Operator-schema injection (architecture §6.6)
# ---------------------------------------------------------------------------
def _operator_docs() -> str:
    """Render :func:`operator_schema` as an inline operator reference for agents.

    Groups the registered kernels by category (arithmetic / time-series /
    cross-sectional / math) and lists each ``signature``, its ``output_range`` and
    any ``common_errors`` — the live ``{name: schema}`` view, so user-registered
    operators show up automatically (architecture §6.6).
    """
    schema = operator_schema()
    # Stable category order; anything unexpected falls into "other".
    order = ["arithmetic", "time-series", "cross-sectional", "math"]
    by_cat: dict[str, list[str]] = {}
    for name, spec in schema.items():
        cat = spec.get("category") or "other"
        sig = spec.get("signature", name)
        rng = spec.get("output_range")
        errs = spec.get("common_errors") or []
        line = f"  {sig}"
        if rng:
            line += f"  -> {rng}"
        if errs:
            line += f"  [note: {'; '.join(errs)}]"
        by_cat.setdefault(cat, []).append(line)

    out = [
        "",
        "",
        "Operator vocabulary (prefix ts_ = time-series, cs_ = cross-sectional; "
        "fields: close, open, high, low, volume, transactions):",
    ]
    for cat in order + [c for c in by_cat if c not in order]:
        lines = by_cat.get(cat)
        if not lines:
            continue
        out.append(f" {cat}:")
        out.extend(sorted(lines))
    return "\n".join(out)


_EVALUATE_DESC = (
    "Evaluate a quantitative alpha factor expression and return a structured "
    "FactorReport: IC, RankIC, ICIR/RankICIR, ic_by_horizon, decay half-life, "
    "turnover, redundancy_score, lookahead detection and a natural-language "
    "suggestion. Accepts BOTH dialects, which parse to the same factor: qlib "
    "syntax (e.g. Corr($close,$volume,20), Ref($close,5)) and Python syntax "
    "(e.g. ts_corr(close,volume,20), ts_delay(close,5)). When evaluating MANY "
    "expressions, call assay_batch instead of looping this tool — batch shares "
    "the panel load and is far faster."
) + _operator_docs()


# ---------------------------------------------------------------------------
# Server + tools (architecture §6.2 / §6.3)
# ---------------------------------------------------------------------------
mcp = FastMCP("assay")
# Backwards-compatible alias: architecture §6.4 names the object ``server``.
server = mcp


@mcp.tool(name="assay_evaluate", description=_EVALUATE_DESC)
def assay_evaluate(
    expr: str,
    universe: str = "NASDAQ100",
    period: list[str] | None = None,
    horizons: list[int] | None = None,
    neutralize: list[str] | None = None,
) -> dict[str, Any]:
    """Evaluate one factor expression into a FactorReport dict (architecture §6.3).

    ``universe`` is any data-backed pool — US (NASDAQ100, SP500…) or A-share
    (CSI300/CSI500/CSI1000); call ``assay_universes`` to see which are live.
    ``period`` is ``["YYYY-MM-DD", "YYYY-MM-DD"]`` (defaults to the config period).
    A factor that fails diagnostics returns a metrics-free report carrying
    ``failure_mode`` / ``suggestion`` / ``lookahead_detected`` rather than raising.
    """
    svc = get_service()
    report = svc.evaluate(
        expr,
        universe=universe,
        period=tuple(period) if period else None,
        horizons=horizons,
        neutralize=neutralize,
    )
    return report.to_dict()


@mcp.tool(
    name="assay_batch",
    description=(
        "Evaluate MULTIPLE factor expressions in parallel over one shared panel, "
        "returning a report list sorted descending by sort_by. Strongly prefer "
        "this over calling assay_evaluate in a loop: the panel load and forward "
        "returns are paid once and the per-factor numerics run concurrently, so a "
        "batch of N factors is far cheaper than N separate calls. Same dual "
        "qlib/Python syntax as assay_evaluate."
    ),
)
def assay_batch(
    exprs: list[str],
    universe: str = "NASDAQ100",
    period: list[str] | None = None,
    n_jobs: int = 8,
    sort_by: str = "rank_icir",
) -> dict[str, Any]:
    """Batch-evaluate ``exprs`` and return ``{total, reports}`` (architecture §6.3).

    All expressions share one session over ``universe``/``period``; results are
    sorted descending by ``sort_by`` (``rank_icir`` | ``rank_ic`` |
    ``decay_halflife``). Faster than looping ``assay_evaluate``.
    """
    svc = get_service()
    reports = svc.batch(
        list(exprs),
        universe=universe,
        period=tuple(period) if period else None,
        n_jobs=n_jobs,
        sort_by=sort_by,
    )
    return {"total": len(reports), "reports": [r.to_dict() for r in reports]}


@mcp.tool(
    name="assay_lint",
    description=(
        "Data-free, instant syntax + diagnostics check for a factor expression — "
        "no panel load, never touches data. Call this BEFORE assay_evaluate / "
        "assay_batch to catch syntax errors, unknown fields/operators and "
        "lookahead-risk warnings cheaply, so you don't waste an evaluation on a "
        "malformed candidate. Returns the detected dialect, the canonical "
        "round-tripped expression, the fields/operators used, and a diagnostics "
        "envelope (errors/warnings with codes and fix suggestions). "
        "NOTE: fields beyond OHLCV (vwap, market_cap/cap) parse but have NO data "
        "in the daily store, so factors using them fail at evaluate time."
    ),
)
def assay_lint(expr: str) -> dict[str, Any]:
    """Parse-only diagnostics: ``{dialect, canonical, fields, operators, diagnostics}``."""
    from assay.engine import detect_dialect, iter_fields, iter_ops, lint, parse

    diagnostics = lint(expr).to_dict()
    dialect = detect_dialect(expr)
    try:
        node = parse(expr)
        canonical = str(node)
        fields = sorted(iter_fields(node))
        operators = sorted(iter_ops(node))
    except Exception:
        canonical, fields, operators = None, [], []
    return {
        "dialect": dialect,
        "canonical": canonical,
        "fields": fields,
        "operators": operators,
        "diagnostics": diagnostics,
    }


@mcp.tool(
    name="assay_universes",
    description=(
        "List the stock universes (pools) available for evaluation and backtesting, "
        "each with its market and current symbol count. Call this FIRST to discover "
        "what data is live before choosing a universe — US (NASDAQ100, SP500…) and "
        "A-share (CSI300, CSI500, CSI1000) can be served side by side. A universe "
        "with n_symbols=0 has no ingested data; don't evaluate against it."
    ),
)
def assay_universes() -> dict[str, Any]:
    """Available universes as ``{universes:[{id, market, n_symbols}], default}``."""
    svc = get_service()
    _start, end = svc.config.default_period
    known = ["NASDAQ100", "SP500", "RUSSELL2000", "CSI300", "CSI500", "CSI1000"]
    out: list[dict[str, Any]] = []
    for uid in known:
        n = 0
        try:
            n = len(svc.store_for_universe(uid).get_universe(uid, end, end))
        except Exception:
            pass
        out.append({"id": uid, "market": svc._market_for(uid), "n_symbols": n})
    return {"universes": out, "default": svc.config.default_universe}


@mcp.tool(
    name="assay_portfolio_backtest",
    description=(
        "Run a FULL portfolio backtest for a factor — the deepest performance test: "
        "it turns the cross-sectional signal into an achievable NET return by "
        "building a portfolio, rebalancing on a schedule, applying real constraints "
        "and trading costs (commission / stamp duty / slippage; A-share ±price-limit "
        "+ T+1 are auto-enforced for CSI* universes), then marking to market daily. "
        "Returns Sharpe / Sortino / Calmar, annual & total return, excess return vs "
        "benchmark, max drawdown, annual turnover, cost_drag, avg holding days, beta/"
        "alpha and (for A-share) limit-hit / suspension stats. Use this on your BEST "
        "few factors from assay_batch to confirm they survive costs — IC alone does "
        "not. rebalance: daily|weekly|monthly|quarterly; weight_method: equal|"
        "signal_prop|quintile. A-share is long-only (融券 not modelled)."
    ),
)
def assay_portfolio_backtest(
    expr: str,
    universe: str = "NASDAQ100",
    period: list[str] | None = None,
    rebalance: str = "monthly",
    weight_method: str = "equal",
    long_short: bool = False,
    max_single_weight: float = 0.05,
) -> dict[str, Any]:
    """Portfolio backtest -> compact performance summary (no heavy NAV/trade series).

    Builds a market-appropriate :class:`PortfolioBacktestConfig` (A-share / US / HK
    cost & limit preset chosen from the universe's market) and runs the §1.1 pipeline.
    Returns the scalar performance metrics + monthly returns + a_share_metrics; the
    full NAV/benchmark/trade series are omitted to keep the response compact.
    """
    from assay.portfolio import PortfolioBacktestConfig

    svc = get_service()
    period_t = tuple(period) if period else tuple(svc.config.default_period)
    # service market codes are US/CN/HK; the portfolio preset uses US/A/HK.
    market = {"CN": "A"}.get(svc._market_for(universe), svc._market_for(universe))
    cfg = PortfolioBacktestConfig.preset(
        market,
        universe=universe,
        period_start=period_t[0],
        period_end=period_t[1],
        rebalance_type=rebalance,
        weight_method=weight_method,
        long_short=(False if market == "A" else bool(long_short)),
        max_single_weight=max_single_weight,
        save_trade_log=False,
        save_position_log=False,
    )
    d = svc.backtest_portfolio(expr, cfg).to_dict()
    keep = [
        "run_id", "factor_id", "period_start", "period_end", "n_trading_days",
        "n_rebalances", "total_return", "annual_return", "gross_return", "excess_return",
        "sharpe", "sortino", "calmar", "information_ratio", "max_drawdown",
        "drawdown_recovery_days", "beta", "alpha_capm", "tracking_error",
        "annual_turnover", "cost_drag", "avg_holding_days", "a_share_metrics",
    ]
    out = {k: d.get(k) for k in keep}
    out["monthly_returns"] = d.get("monthly_returns")
    out["config"] = {"universe": universe, "market": market, "rebalance": rebalance,
                     "weight_method": weight_method, "long_short": cfg.long_short}
    return out


@mcp.tool(
    name="assay_library_list",
    description=(
        "List factors already in the Assay library, with optional filters. Call "
        "this BEFORE generating new factors to see what already exists and how "
        "good it is — redundancy_score tells you how similar a candidate is to "
        "the existing set, so you can avoid duplicating known alphas. Returns "
        "compact summary rows sorted descending by sort_by."
    ),
)
def assay_library_list(
    min_rank_icir: float = 0.0,
    max_redundancy: float = 1.0,
    source: str | None = None,
    sort_by: str = "rank_icir",
    limit: int = 20,
) -> dict[str, Any]:
    """Filtered/sorted library view as ``{total, factors}`` (architecture §6.3).

    ``min_rank_icir`` / ``max_redundancy`` floor/ceiling the quality and
    uniqueness; ``source`` exact-matches the provenance tag (AGENT | ALPHA101 |
    ALPHA158 | CUSTOM | …); ``limit`` is capped at 100. Pass ``min_rank_icir=-1``
    to include inverse-signal (negative-ICIR) factors, which are hidden by default.
    """
    svc = get_service()
    summaries = svc.library_query(
        min_rank_icir=min_rank_icir,
        max_redundancy=max_redundancy,
        source=source,
        sort_by=sort_by,
        limit=min(int(limit), 100),
    )
    return {"total": len(summaries), "factors": [s.to_dict() for s in summaries]}


@mcp.tool(
    name="assay_library_get",
    description=(
        "Fetch the full FactorReport for one factor_id from the library — every "
        "metric, the ic/rank_ic series, quintile returns and diagnostics, not "
        "just the summary row. Use after assay_library_list to inspect a "
        "candidate in detail. Returns {found: false} if the id is unknown."
    ),
)
def assay_library_get(factor_id: str) -> dict[str, Any]:
    """Return the full stored :class:`FactorReport` for ``factor_id``.

    ``{found: false, factor_id}`` when the id is not in the library.
    """
    svc = get_service()
    report = svc.library.get(factor_id)
    if report is None:
        return {"found": False, "factor_id": factor_id}
    return {"found": True, "report": report.to_dict()}


@mcp.tool(
    name="assay_library_save",
    description=(
        "Persist a FactorReport to the library so future runs and "
        "assay_library_list / _correlation see it. Pass the `report` dict exactly "
        "as returned by assay_evaluate or assay_batch. Re-saving the same factor "
        "overwrites in place (the id is the canonical-expression hash). Returns "
        "the factor_id it was stored under."
    ),
)
def assay_library_save(report: dict[str, Any]) -> dict[str, Any]:
    """Save a report dict (from ``assay_evaluate``/``assay_batch``) to the library.

    Rebuilds a :class:`FactorReport` from ``report`` and persists it; the id is
    the canonical-expression hash, so re-saving overwrites in place.
    """
    from assay.library import FactorReport

    svc = get_service()
    factor_report = FactorReport.from_dict(report)
    factor_id = svc.library.save(factor_report)
    return {"saved": True, "factor_id": factor_id}


@mcp.tool(
    name="assay_library_correlation",
    description=(
        "Compute the pairwise signed rank-correlation matrix between a list of "
        "library factor_ids, re-evaluating each on one shared grid. Use this to "
        "check whether a newly generated factor is redundant with existing "
        "library factors before saving it: |correlation| near 1 means the same "
        "bet. Unknown or non-evaluable ids are dropped from the axes."
    ),
)
def assay_library_correlation(
    factor_ids: list[str],
    universe: str = "NASDAQ100",
    period: list[str] | None = None,
) -> dict[str, Any]:
    """Signed-Spearman similarity matrix over ``factor_ids`` (architecture §6.3).

    Returns ``{"factor_ids": [...], "matrix": [[...]]}`` on the surviving axes.
    """
    svc = get_service()
    return svc.correlation_matrix(
        list(factor_ids),
        universe=universe,
        period=tuple(period) if period else None,
    )


@mcp.tool(
    name="assay_library_prune",
    description=(
        "Find library factors that are redundant — pairwise |correlation| at or "
        "above redundancy_threshold — keeping the higher rank_icir of each "
        "correlated pair. With dry_run=true (default) it only REPORTS which "
        "factors would be removed; set dry_run=false to actually delete them. "
        "Run dry first and confirm before deleting."
    ),
)
def assay_library_prune(
    redundancy_threshold: float = 0.7,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Identify (and optionally remove) redundant library factors (architecture §6.3).

    Builds the correlation matrix over the whole library, greedily marks the
    weaker (lower ``rank_icir``) factor of each pair at/above
    ``redundancy_threshold``, and — when ``dry_run`` is false — deletes them.
    Returns the would-delete / kept partition plus the number actually deleted.
    """
    from assay.library import prune as _prune

    svc = get_service()
    summaries = svc.library_query(limit=-1)
    factor_ids = [s.factor_id for s in summaries]
    scores = {s.factor_id: (s.rank_icir if s.rank_icir is not None else float("-inf")) for s in summaries}

    corr = svc.correlation_matrix(factor_ids)
    # correlation_matrix may drop ids that failed to evaluate — realign scores.
    surviving = corr["factor_ids"]
    result = _prune(
        corr["matrix"],
        surviving,
        {fid: scores.get(fid, float("-inf")) for fid in surviving},
        threshold=redundancy_threshold,
    )

    deleted = 0
    if not dry_run and result["would_delete"]:
        deleted = svc.library.delete(result["would_delete"])

    return {
        "dry_run": dry_run,
        "redundancy_threshold": redundancy_threshold,
        "would_delete": result["would_delete"],
        "kept": result["kept"],
        "pairs_over_threshold": result["pairs_over_threshold"],
        "deleted": deleted,
    }


@mcp.tool(
    name="assay_system_status",
    description=(
        "Report Assay system health: library size, L2 factor-cache statistics "
        "(entries / hit-rate / footprint), active evaluation sessions and the "
        "resolved universe/period defaults. Use this to confirm the service is "
        "wired up and to read cache effectiveness before a large batch run."
    ),
)
def assay_system_status() -> dict[str, Any]:
    """Data-freshness / cache / session snapshot of the service (architecture §6.2)."""
    svc = get_service()
    cfg = svc.config
    return {
        "service": "assay",
        "market": cfg.market,
        "library": {
            "path": str(svc.library.path),
            "size": len(svc.library_query(limit=-1)),
        },
        "cache": svc.cache.stats(),
        "sessions": {"active": svc.sessions.active_sessions()},
        "defaults": {
            "universe": cfg.default_universe,
            "period": list(cfg.default_period),
            "horizons": list(cfg.default_horizons),
            "execution": cfg.default_execution,
            "adj": cfg.default_adj,
        },
    }


# ---------------------------------------------------------------------------
# Entry point (architecture §6.5)
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> None:
    """Run the MCP server. ``stdio`` by default; ``--transport sse|http --port N``.

    ``stdio``  — Claude Desktop / local agents (the default, no network).
    ``sse``    — Server-Sent Events over HTTP for remote agents.
    ``http``   — streamable-HTTP transport (mapped to FastMCP ``streamable-http``).
    """
    parser = argparse.ArgumentParser(
        prog="assay.mcp.server",
        description="Assay MCP server — agent tools over AssayService (architecture §6).",
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse", "http"],
        default="stdio",
        help="Transport: stdio (default, local), sse, or http (streamable-HTTP).",
    )
    parser.add_argument(
        "--host", default="127.0.0.1", help="Bind host for sse/http (default 127.0.0.1)."
    )
    parser.add_argument(
        "--port", type=int, default=8001, help="Bind port for sse/http (default 8001)."
    )
    args = parser.parse_args(argv)

    if args.transport == "stdio":
        mcp.run("stdio")
        return

    # Network transports: apply host/port to the FastMCP settings before running.
    mcp.settings.host = args.host
    mcp.settings.port = args.port
    transport = "streamable-http" if args.transport == "http" else "sse"
    print(
        f"assay MCP server: {transport} on http://{args.host}:{args.port}",
        file=sys.stderr,
    )
    mcp.run(transport)


if __name__ == "__main__":
    main()
