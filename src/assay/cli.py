"""Command-line interface for the Assay data pipeline.

Examples
--------
    # one-shot: prepare NASDAQ-100 for a range
    python -m assay.cli prepare-nasdaq100 --start 2023-01-01 --end 2023-12-31

    # individual stages
    python -m assay.cli universe   --index NASDAQ100 --start 2023-01-01 --end 2023-12-31
    python -m assay.cli corp-actions --start 2023-01-01 --end 2023-12-31
    python -m assay.cli prices     --start 2023-01-01 --end 2023-12-31

    # inspect / verify
    python -m assay.cli status
    python -m assay.cli verify --start 2023-06-01 --end 2023-06-30
    python -m assay.cli discover                 # show the local MASSIVE source layout
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging

from assay.config import AssayConfig
from assay.data.massive import LocalFlatFiles
from assay.data.pipeline import prepare_nasdaq100
from assay.data.schemas import (
    adj_events_path,
    price_root,
    universe_snapshots_path,
)
from assay.data.universe import nasdaq100


def _date(s: str) -> dt.date:
    return dt.date.fromisoformat(s)


def _symbols(s: str | None) -> set[str] | None:
    if not s:
        return None
    return {tok.strip().upper() for tok in s.split(",") if tok.strip()}


def _horizons(s: str | None) -> list[int] | None:
    """Parse a comma-separated horizon list, e.g. '1,5,10,20' -> [1,5,10,20]."""
    if not s:
        return None
    return [int(tok.strip()) for tok in s.split(",") if tok.strip()]


def _period(start: dt.date | None, end: dt.date | None) -> tuple[str, str] | None:
    """SDK ``period`` from optional --start/--end dates.

    Returns a complete ``(start, end)`` pair only when both are supplied; otherwise
    ``None`` so the service fills the whole period from its config defaults (a partial
    pair would otherwise be ambiguous).
    """
    if start is None or end is None:
        return None
    return (start.isoformat(), end.isoformat())


def _fmt(x, spec: str = "+.4f") -> str:
    """Format a float metric for display; None / NaN -> 'n/a'."""
    import math

    if x is None:
        return "n/a"
    try:
        f = float(x)
    except (TypeError, ValueError):
        return str(x)
    return "n/a" if not math.isfinite(f) else format(f, spec)


def _resolve_universe(start: dt.date, end: dt.date, explicit: set[str] | None) -> set[str]:
    if explicit:
        return explicit
    return set(nasdaq100.union_over_range(start, end))


# -- command handlers ---------------------------------------------------------
def cmd_discover(args, config: AssayConfig) -> None:
    client = LocalFlatFiles(config.massive)
    print(f"local source: {config.massive.source_dir}")
    print("datasets present:")
    for p in client.list_top_level_prefixes():
        print(" ", p)
    today = dt.date.today()
    recent = client.list_day_aggs(today - dt.timedelta(days=30), today)
    print(f"\nMost recent day-aggregate files (last 30 days, {len(recent)} found):")
    for f in recent[-5:]:
        print(" ", f.date, f.key)


def cmd_universe(args, config: AssayConfig) -> None:
    from assay.data.ingest import UniverseIngester

    stats = UniverseIngester(config).run(args.index, args.start, args.end)
    print(f"universe[{args.index}]: {stats}")


def cmd_corp_actions(args, config: AssayConfig) -> None:
    from assay.data.ingest import CorpActionIngester

    tickers = sorted(_resolve_universe(args.start, args.end, _symbols(args.symbols)))
    stats = CorpActionIngester(config).run(tickers, args.start, args.end)
    print(f"corp-actions ({len(tickers)} tickers): {stats}")


def cmd_prices(args, config: AssayConfig) -> None:
    from assay.data.ingest import PriceIngester

    symbols = _resolve_universe(args.start, args.end, _symbols(args.symbols))
    stats = PriceIngester(config).run(args.start, args.end, symbols=symbols)
    print(f"prices ({len(symbols)} symbols): {stats}")


def cmd_prepare(args, config: AssayConfig) -> None:
    report = prepare_nasdaq100(
        config,
        args.start,
        args.end,
        do_universe=not args.skip_universe,
        do_corp_actions=not args.skip_corp_actions,
        do_prices=not args.skip_prices,
    )
    print("prepare-nasdaq100 report:")
    for k, v in report.items():
        print(f"  {k}: {v}")


def cmd_status(args, config: AssayConfig) -> None:
    import polars as pl

    print(f"data_dir: {config.data_dir}  market: {config.market}\n")

    proot = price_root(config.data_dir, config.market)
    files = sorted(proot.glob("**/price_raw.parquet")) if proot.exists() else []
    if files:
        df = pl.read_parquet([str(f) for f in files], columns=["date", "symbol"])
        print(
            f"price_raw:          {df.height:>10,} rows | "
            f"{df['symbol'].n_unique()} symbols | "
            f"{df['date'].min()} .. {df['date'].max()} | {len(files)} partitions"
        )
    else:
        print("price_raw:          (none)")

    apath = adj_events_path(config.data_dir, config.market)
    if apath.is_file():
        df = pl.read_parquet(apath)
        by_type = df.group_by("event_type").len().sort("len", descending=True)
        counts = ", ".join(f"{r['event_type']}={r['len']}" for r in by_type.to_dicts())
        print(f"adj_events:         {df.height:>10,} rows | {counts}")
    else:
        print("adj_events:         (none)")

    upath = universe_snapshots_path(config.data_dir, config.market)
    if upath.is_file():
        df = pl.read_parquet(upath)
        print(
            f"universe_snapshots: {df.height:>10,} rows | "
            f"indices={sorted(df['index_id'].unique().to_list())} | "
            f"{df['effective_date'].min()} .. {df['effective_date'].max()}"
        )
    else:
        print("universe_snapshots: (none)")


def cmd_parse(args, config: AssayConfig | None) -> None:
    from assay.engine import detect_dialect, iter_fields, iter_ops, parse

    node = parse(args.expr)
    print(f"dialect:     {detect_dialect(args.expr)}")
    print(f"struct_hash: {node.struct_hash()}")
    print(f"fields:      {sorted(iter_fields(node))}")
    print(f"operators:   {sorted(iter_ops(node))}")
    print(f"canonical:   {node}")


def cmd_eval(args, config: AssayConfig) -> None:
    import numpy as np

    from assay.data.store import DataStore
    from assay.engine import FactorEngine

    store = DataStore(config)
    as_of = args.as_of or args.end
    eng = FactorEngine.from_store(
        store, args.index, (args.start, args.end), as_of, adj=args.adj
    )
    result = eng.evaluate(args.expr)
    v = result.values
    finite = np.isfinite(v)
    print(f"factor:   {result.expr}")
    print(
        f"panel:    {v.shape[0]} dates x {v.shape[1]} symbols | "
        f"coverage={finite.mean():.1%} | as-of {as_of} ({args.adj}-adjusted)"
    )
    if finite.any():
        vals = v[finite]
        print(f"values:   mean={vals.mean():+.4g}  std={vals.std():.4g}  "
              f"min={vals.min():+.4g}  max={vals.max():+.4g}")
        # last fully-evaluated cross-section: top/bottom names by factor value
        last = next((t for t in range(v.shape[0] - 1, -1, -1) if finite[t].any()), None)
        if last is not None:
            row = v[last]
            order = np.argsort(np.where(np.isnan(row), -np.inf, row))
            syms = result.symbols
            top = [(syms[j], row[j]) for j in order[::-1][:3] if np.isfinite(row[j])]
            bot = [(syms[j], row[j]) for j in order[:3] if np.isfinite(row[j])]
            print(f"top  ({result.dates[last]}): " + ", ".join(f"{s}={x:+.3g}" for s, x in top))
            print(f"bottom({result.dates[last]}): " + ", ".join(f"{s}={x:+.3g}" for s, x in bot))


# -- SDK-backed factor / library / server commands (engineering-docs §7.4) ----
def _report_summary(report) -> str:
    """Compact, agent-readable one-block summary of a :class:`FactorReport`.

    Mirrors the headline fields the agent loop consumes (engineering-docs §7.2):
    predictive quality, decay, turnover, and — when something is wrong — the
    ``failure_mode`` + ``suggestion`` so the next generation step has actionable signal.
    """
    panel = f"panel:       {report.n_dates} dates x {report.n_symbols} symbols"
    if report.duration_ms is not None:
        panel += f"  ({report.duration_ms:.0f} ms)"
    lines = [
        f"factor:      {report.expr}",
        f"factor_id:   {report.factor_id}",
        f"universe:    {report.universe_id}  period={report.eval_period[0]}..{report.eval_period[1]}"
        f"  exec={report.execution}",
        panel,
    ]
    if report.failure_mode is not None:
        lines.append(f"FAILURE:     {report.failure_mode}")
        if report.suggestion:
            lines.append(f"suggestion:  {report.suggestion}")
    if report.rank_ic == report.rank_ic:  # not NaN -> metrics present
        ibh = ", ".join(f"h{h}={_fmt(v, '+.3f')}" for h, v in sorted(report.ic_by_horizon.items()))
        lines += [
            f"rank_ic:     {_fmt(report.rank_ic)}   rank_icir: {_fmt(report.rank_icir)}",
            f"ic:          {_fmt(report.ic)}   icir:      {_fmt(report.icir)}",
            f"ic_by_horizon: {ibh}" if ibh else "ic_by_horizon: (none)",
            f"decay_halflife: {report.decay_halflife_days if report.decay_halflife_days is not None else 'n/a'} d"
            f"   turnover_1d: {_fmt(report.turnover_1d, '.3f')}",
        ]
        if report.failure_mode is not None and report.suggestion:
            lines.append(f"note:        {report.suggestion}")
    return "\n".join(lines)


def cmd_run(args, config: AssayConfig | None) -> None:
    """Evaluate a single factor via the SDK and print a compact FactorReport summary."""
    import assay

    assay.init()  # auto-init from env / .env; DataStore built lazily on data access
    report = assay.backtest(
        args.expr,
        universe=args.index,
        period=_period(args.start, args.end),
        as_of=args.as_of.isoformat() if args.as_of else None,
        adj=args.adj,
        execution=args.execution,
        neutralize=args.neutralize,
        horizons=_horizons(args.horizons),
        save=args.save,
    )
    print(_report_summary(report))
    if args.save and report.failure_mode is None:
        print(f"saved:       library[{report.factor_id}]")


def cmd_batch(args, config: AssayConfig | None) -> None:
    """Evaluate many factors (from a file or positionals); print top by rank_icir."""
    import assay

    exprs = _read_exprs(args.factors)
    if not exprs:
        print("no expressions to evaluate (empty file / no positionals)")
        return

    assay.init()
    reports = assay.batch_backtest(
        exprs,
        universe=args.index,
        period=_period(args.start, args.end),
        as_of=args.as_of.isoformat() if args.as_of else None,
        adj=args.adj,
        execution=args.execution,
        neutralize=args.neutralize,
        horizons=_horizons(args.horizons),
        n_jobs=args.jobs,
        sort_by=args.sort,
        save=args.save,
    )

    ok = sum(1 for r in reports if r.failure_mode is None)
    print(f"evaluated {len(reports)} factors ({ok} ok, {len(reports) - ok} failed), "
          f"sorted by {args.sort} desc:\n")
    top = reports[: args.limit] if args.limit and args.limit > 0 else reports
    print(f"  {'rank_icir':>10}  {'rank_ic':>8}  {'ic':>8}  {'mode':<13}  expr")
    for r in top:
        mode = r.failure_mode or "-"
        print(f"  {_fmt(r.rank_icir):>10}  {_fmt(r.rank_ic, '+.3f'):>8}  "
              f"{_fmt(r.ic, '+.3f'):>8}  {mode:<13}  {r.expr}")

    if args.output:
        _write_batch_output(reports, args.output)
        print(f"\nwrote {len(reports)} reports -> {args.output}")


def _read_exprs(tokens: list[str]) -> list[str]:
    """Resolve batch expressions: a single existing file (one expr/line, '#' skipped)
    is expanded; otherwise the positionals are treated as literal expressions."""
    import os

    if len(tokens) == 1 and os.path.isfile(tokens[0]):
        from pathlib import Path

        out: list[str] = []
        for raw in Path(tokens[0]).read_text().splitlines():
            line = raw.strip()
            if line and not line.startswith("#"):
                out.append(line)
        return out
    return list(tokens)


def _write_batch_output(reports, path: str) -> None:
    """Serialise batch reports to JSON (.json) or Parquet (.parquet/.pq) by extension."""
    import json
    from pathlib import Path

    p = Path(path)
    rows = [r.to_dict() for r in reports]
    if p.suffix.lower() in (".parquet", ".pq"):
        import polars as pl

        # Flatten to the summary columns Parquet can hold rectangularly; the nested
        # series stay in the JSON path. Keep the scalar leaderboard fields.
        flat = [
            {
                "factor_id": r.factor_id,
                "expr": r.expr,
                "rank_ic": r.rank_ic,
                "rank_icir": r.rank_icir,
                "ic": r.ic,
                "icir": r.icir,
                "decay_halflife_days": r.decay_halflife_days,
                "turnover_1d": r.turnover_1d,
                "failure_mode": r.failure_mode,
                "n_dates": r.n_dates,
                "n_symbols": r.n_symbols,
                "universe_id": r.universe_id,
            }
            for r in reports
        ]
        pl.DataFrame(flat).write_parquet(p)
    else:
        p.write_text(json.dumps(rows, indent=2))


def _portfolio_summary(report) -> str:
    """Compact, agent-readable one-block summary of a :class:`PortfolioReport`.

    Mirrors the headline portfolio metrics the agent loop / WebUI consume
    (portfolio design-doc §4-§5): realised return, risk-adjusted ratios, drawdown,
    and the turnover / cost-drag the IC analysis cannot see.
    """
    cfg = report.config or {}
    universe = cfg.get("universe", "?")
    market = cfg.get("market", "?")
    rebal = cfg.get("rebalance_type", "?")
    weight = cfg.get("weight_method", "?")
    ls = "long-short" if cfg.get("long_short") else "long-only"
    lines = [
        f"run_id:        {report.run_id}",
        f"factor:        {report.expr if hasattr(report, 'expr') else report.factor_id}",
        f"factor_id:     {report.factor_id}",
        f"universe:      {universe} [{market}]  {report.period_start}..{report.period_end}",
        f"setup:         rebalance={rebal}  weights={weight}  {ls}",
        f"panel:         {report.n_trading_days} trading days | {report.n_rebalances} rebalances",
        f"total_return:  {_fmt(report.total_return, '+.2%')}   annual_return: {_fmt(report.annual_return, '+.2%')}",
        f"sharpe:        {_fmt(report.sharpe, '.2f')}   sortino: {_fmt(report.sortino, '.2f')}"
        f"   calmar: {_fmt(report.calmar, '.2f')}",
        f"max_drawdown:  {_fmt(report.max_drawdown, '+.2%')}   information_ratio: {_fmt(report.information_ratio, '.2f')}",
        f"annual_turnover: {_fmt(report.annual_turnover, '.2f')}   cost_drag: {_fmt(report.cost_drag, '+.2%')}"
        f"   avg_holding_days: {_fmt(report.avg_holding_days, '.1f')}",
    ]
    return "\n".join(lines)


def cmd_portfolio(args, config: AssayConfig | None) -> None:
    """Run a portfolio backtest via the SDK and print a compact PortfolioReport summary.

    Builds a :class:`PortfolioBacktestConfig` from the market preset (cost/limit
    defaults, portfolio design-doc §6) overridden by the supplied CLI knobs, runs the
    section-1.1 pipeline, prints the headline metrics, and — with ``--output`` —
    writes the full JSON report.
    """
    import json

    import assay
    from assay.portfolio import PortfolioBacktestConfig

    assay.init()  # auto-init from env / .env; DataStore built lazily on data access

    overrides: dict = {
        "period_start": args.start.isoformat(),
        "period_end": args.end.isoformat(),
        "universe": args.universe,
        "rebalance_type": args.rebalance,
        "weight_method": args.weight_method,
        "long_short": args.long_short,
    }
    if args.as_of is not None:
        overrides["as_of_date"] = args.as_of.isoformat()
    if args.gross_exposure is not None:
        overrides["gross_exposure"] = args.gross_exposure
    if args.net_exposure is not None:
        overrides["net_exposure"] = args.net_exposure
    if args.max_weight is not None:
        overrides["max_single_weight"] = args.max_weight
    if args.execution is not None:
        overrides["execution_price"] = args.execution
    config_obj = PortfolioBacktestConfig.preset(args.market, **overrides)

    report = assay.backtest_portfolio(
        args.expr,
        config_obj,
        as_of=args.as_of.isoformat() if args.as_of else None,
    )
    print(_portfolio_summary(report))

    if args.output:
        from pathlib import Path

        Path(args.output).write_text(json.dumps(report.to_dict(), indent=2))
        print(f"\nwrote full report -> {args.output}")


def cmd_report(args, config: AssayConfig | None) -> None:
    """Fetch a saved factor by id from the library and pretty-print its report."""
    import assay

    assay.init()
    report = assay.library.get(args.factor_id)
    if report is None:
        print(f"no factor with id {args.factor_id!r} in the library")
        return
    print(_report_summary(report))
    qr = report.quintile_returns
    if qr:
        print("quintiles:   " + ", ".join(f"q{i + 1}={_fmt(v, '+.3f')}" for i, v in enumerate(qr)))
    if report.most_similar_factor:
        print(f"most_similar: {report.most_similar_factor}  "
              f"(redundancy={_fmt(report.redundancy_score, '.3f')})")


def cmd_library(args, config: AssayConfig | None) -> None:
    """List / get / delete / prune the factor library via the SDK 'library' proxy."""
    import assay

    assay.init()
    action = args.action

    if action == "list":
        rows = assay.library.list(
            universe=args.universe,
            min_rank_icir=args.min_icir,
            source=args.source,
            sort_by=args.sort,
            limit=args.limit,
        )
        if not rows:
            print("(library empty / no rows match the filter)")
            return
        print(f"{'factor_id':<18}  {'rank_icir':>10}  {'rank_ic':>8}  {'ic':>8}  "
              f"{'halflife':>8}  {'mode':<12}  expr")
        for s in rows:
            hl = s.decay_halflife_days if s.decay_halflife_days is not None else "-"
            mode = s.failure_mode or "-"
            print(f"{s.factor_id:<18}  {_fmt(s.rank_icir):>10}  "
                  f"{_fmt(s.rank_ic, '+.3f'):>8}  {_fmt(s.ic, '+.3f'):>8}  "
                  f"{str(hl):>8}  {mode:<12}  {s.expr}")
        print(f"\n{len(rows)} factor(s).")

    elif action == "get":
        cmd_report(args, config)

    elif action == "delete":
        n = assay.library.delete(list(args.factor_ids))
        print(f"deleted {n} factor(s).")

    elif action == "prune":
        plan = assay.library.prune(
            redundancy_threshold=args.redundancy_threshold,
            dry_run=not args.apply,
        )
        wd = plan.get("would_delete", [])
        kept = plan.get("kept", [])
        print(f"pairs over threshold ({args.redundancy_threshold}): "
              f"{plan.get('pairs_over_threshold', 0)}")
        print(f"would_delete: {len(wd)}   kept: {len(kept)}")
        for fid in wd:
            print(f"  - {fid}")
        if args.apply:
            print(f"\ndeleted {plan.get('deleted', plan.get('count', 0))} factor(s).")
        else:
            print("\n(dry run — pass --apply to delete)")


def cmd_seed_demo(args, config: AssayConfig | None) -> None:
    """Seed the factor library with the Alpha101 / Alpha158 demo catalogs."""
    import assay

    svc = assay.init()
    period = (args.start, args.end) if (args.start and args.end) else None
    sources = tuple(s.strip().upper() for s in (args.sources or "ALPHA101,ALPHA158").split(",") if s.strip())
    print(f"seeding demo library: sources={list(sources)} "
          f"universe={args.universe or '(config default)'} ... (this evaluates a few hundred factors)")
    summary = svc.seed_demo_library(universe=args.universe, period=period, as_of=args.as_of, sources=sources)
    for tag, st in summary.items():
        print(f"  {tag}: evaluated {st['evaluated']}, saved {st['saved']}, skipped {st['skipped']}")
    total = sum(st["saved"] for st in summary.values())
    print(f"done — {total} demo factor(s) now in the library.")


def cmd_serve_api(args, config: AssayConfig | None) -> None:
    """Launch the FastAPI REST service (uvicorn assay.api.app:app). Imports lazily."""
    try:
        import uvicorn  # noqa: F401

        import assay.api.app  # noqa: F401
    except ModuleNotFoundError as e:
        raise SystemExit(
            f"serve-api unavailable: {e.name!r} is not installed/built yet. "
            "The REST surface (assay.api.app) is a planned module (architecture §8.3); "
            "install the API extra and ensure 'uvicorn' is available."
        )
    import uvicorn

    uvicorn.run("assay.api.app:app", host=args.host, port=args.port, reload=args.reload)


def cmd_serve_mcp(args, config: AssayConfig | None) -> None:
    """Launch the MCP server (assay.mcp.server.main). Imports lazily."""
    try:
        from assay.mcp.server import main as mcp_main
    except ModuleNotFoundError as e:
        raise SystemExit(
            f"serve-mcp unavailable: {e.name!r} is not installed/built yet. "
            "The MCP surface (assay.mcp.server) is a planned module (architecture §8.3); "
            "ensure the 'mcp' package and assay.mcp are available."
        )
    mcp_main(transport=args.transport, port=args.port)


def cmd_verify(args, config: AssayConfig) -> None:
    from assay.data.store import DataStore

    store = DataStore(config)
    as_of = args.as_of or args.end
    universe = store.get_universe(args.index, args.end, as_of)
    if not universe:
        universe = sorted(nasdaq100.members_on(args.end))
        print(f"(universe_snapshots empty; using live nasdaq100.members_on({args.end}))")
    print(f"universe[{args.index}] as-of {as_of}: {len(universe)} symbols")

    panel = store.get_panel(
        fields=["close", "volume"],
        symbols=universe,
        start_date=args.start,
        end_date=args.end,
        as_of_date=as_of,
        adj=args.adj,
    )
    print(
        f"panel ({args.adj}-adjusted): {panel.height:,} rows | "
        f"{panel['symbol'].n_unique()} symbols | "
        f"{panel['date'].min()} .. {panel['date'].max()}"
    )
    print(panel.head(8))


# -- argument parser ----------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="assay-data", description="Assay data loader & preparer")
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = p.add_subparsers(dest="command", required=True)

    def add_range(sp):
        sp.add_argument("--start", type=_date, required=True, help="YYYY-MM-DD")
        sp.add_argument("--end", type=_date, required=True, help="YYYY-MM-DD")

    sp = sub.add_parser("discover", help="show the local MASSIVE source layout (sanity check)")
    sp.set_defaults(func=cmd_discover)

    sp = sub.add_parser("prepare-nasdaq100", help="prepare full dataset for NASDAQ-100")
    add_range(sp)
    sp.add_argument("--skip-universe", action="store_true")
    sp.add_argument("--skip-corp-actions", action="store_true")
    sp.add_argument("--skip-prices", action="store_true")
    sp.set_defaults(func=cmd_prepare)

    sp = sub.add_parser("universe", help="build universe_snapshots")
    add_range(sp)
    sp.add_argument("--index", default="NASDAQ100")
    sp.set_defaults(func=cmd_universe)

    sp = sub.add_parser("corp-actions", help="read local splits & dividends")
    add_range(sp)
    sp.add_argument("--symbols", help="comma-separated; default = NASDAQ-100 union over range")
    sp.set_defaults(func=cmd_corp_actions)

    sp = sub.add_parser("prices", help="transfer & normalize local day aggregates")
    add_range(sp)
    sp.add_argument("--symbols", help="comma-separated; default = NASDAQ-100 union over range")
    sp.set_defaults(func=cmd_prices)

    sp = sub.add_parser("status", help="show what has been ingested")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("verify", help="read a PIT panel and print a summary")
    add_range(sp)
    sp.add_argument("--index", default="NASDAQ100")
    sp.add_argument("--as-of", type=_date, default=None, help="default = --end")
    sp.add_argument("--adj", default="split", choices=["none", "split", "total", "forward"])
    sp.set_defaults(func=cmd_verify)

    sp = sub.add_parser("parse", help="parse a factor expression and show its AST (no data needed)")
    sp.add_argument("expr", help="qlib or function-call/Alpha-101 factor expression")
    sp.set_defaults(func=cmd_parse, needs_config=False)

    sp = sub.add_parser("eval", help="evaluate a factor expression over the PIT panel")
    add_range(sp)
    sp.add_argument("expr", help="qlib or function-call/Alpha-101 factor expression")
    sp.add_argument("--index", default="NASDAQ100")
    sp.add_argument("--as-of", type=_date, default=None, help="default = --end")
    sp.add_argument("--adj", default="split", choices=["none", "split", "total", "forward"])
    sp.set_defaults(func=cmd_eval)

    # -- SDK-backed factor / library / server surface (engineering-docs §7.4) --
    # These auto-init the AssayService themselves (needs_config=False), so the CLI
    # never builds an AssayConfig for them; the SDK reads env / .env and touches the
    # network only when a backtest actually loads data.
    def add_sdk_eval_opts(sp, *, with_expr: bool = True):
        """Common evaluation knobs shared by run/batch (universe, period, execution...)."""
        if with_expr:
            sp.add_argument("expr", help="qlib or function-call/Alpha-101 factor expression")
        grp = sp.add_mutually_exclusive_group()
        grp.add_argument("--index", "--universe", dest="index", default=None,
                         help="universe id (default = config default_universe)")
        sp.add_argument("--start", type=_date, default=None, help="period start YYYY-MM-DD")
        sp.add_argument("--end", type=_date, default=None, help="period end YYYY-MM-DD")
        sp.add_argument("--as-of", type=_date, default=None, help="point-in-time as-of date")
        sp.add_argument("--adj", default=None,
                        choices=["none", "split", "total", "forward"], help="price adjustment")
        sp.add_argument("--execution", default=None, choices=["next_open", "next_close"],
                        help="forward-return convention (default = config default)")
        sp.add_argument("--neutralize", default=None,
                        help="comma-separated group keys to neutralize against")
        sp.add_argument("--horizons", default=None, help="comma-separated, e.g. 1,5,10,20")
        sp.add_argument("--save", action="store_true", help="persist report(s) to the library")

    sp = sub.add_parser("run", help="evaluate a single factor via the SDK (compact report)")
    add_sdk_eval_opts(sp)
    sp.set_defaults(func=cmd_run, needs_config=False)

    sp = sub.add_parser(
        "batch", help="evaluate many factors (file: one/line, '#' skipped — or positionals)"
    )
    sp.add_argument("factors", nargs="+", help="a factors file OR literal expressions")
    add_sdk_eval_opts(sp, with_expr=False)
    sp.add_argument("--jobs", type=int, default=None, help="parallel workers (default = config)")
    sp.add_argument("--sort", default="rank_icir", help="sort key (default rank_icir)")
    sp.add_argument("--limit", type=int, default=20, help="print top N (default 20)")
    sp.add_argument("--output", default=None, help="write all reports to .json or .parquet")
    sp.set_defaults(func=cmd_batch, needs_config=False)

    sp = sub.add_parser(
        "portfolio", help="run a portfolio backtest for a factor (compact PortfolioReport)"
    )
    sp.add_argument("expr", help="qlib or function-call/Alpha-101 factor expression")
    sp.add_argument("--universe", default="NASDAQ100",
                    help="universe id (default NASDAQ100)")
    sp.add_argument("--start", type=_date, required=True, help="period start YYYY-MM-DD")
    sp.add_argument("--end", type=_date, required=True, help="period end YYYY-MM-DD")
    sp.add_argument("--as-of", type=_date, default=None, help="point-in-time as-of date")
    sp.add_argument("--market", default="US", choices=["US", "A", "HK"],
                    help="market preset (cost/limit model; default US)")
    sp.add_argument("--rebalance", default="monthly",
                    choices=["daily", "weekly", "monthly", "quarterly", "threshold", "signal"],
                    help="rebalance schedule (default monthly)")
    sp.add_argument("--weight-method", dest="weight_method", default="signal_prop",
                    choices=["equal", "signal_prop", "mv", "risk_parity", "quintile",
                             "decile", "bl"],
                    help="weight construction (default signal_prop)")
    sp.add_argument("--long-short", dest="long_short", action="store_true",
                    help="long top / short bottom (default long-only)")
    sp.add_argument("--gross-exposure", dest="gross_exposure", type=float, default=None,
                    help="total absolute weight sum (0.5-2.0)")
    sp.add_argument("--net-exposure", dest="net_exposure", type=float, default=None,
                    help="net weight sum (-1.0-1.0; 0.0 = dollar-neutral)")
    sp.add_argument("--max-weight", dest="max_weight", type=float, default=None,
                    help="max single-stock weight (0.01-0.30)")
    sp.add_argument("--execution", default=None,
                    choices=["next_open", "next_close", "vwap", "arrival"],
                    help="execution price benchmark (default = preset)")
    sp.add_argument("--output", default=None, help="write the full report to a .json file")
    sp.set_defaults(func=cmd_portfolio, needs_config=False)

    sp = sub.add_parser("report", help="fetch a saved factor by id and pretty-print it")
    sp.add_argument("factor_id", help="library factor id (sha256[:16] of canonical expr)")
    sp.set_defaults(func=cmd_report, needs_config=False)

    sp = sub.add_parser("library", help="manage the factor library (list/get/delete/prune)")
    lib_sub = sp.add_subparsers(dest="action", required=True)

    lsp = lib_sub.add_parser("list", help="list/sort/filter saved factors")
    lsp.add_argument("--universe", default=None, help="filter by universe id")
    lsp.add_argument("--source", default=None, help="filter by lineage source")
    lsp.add_argument("--min-icir", dest="min_icir", type=float, default=0.0,
                     help="minimum rank_icir floor")
    lsp.add_argument("--sort", default="rank_icir", help="sort key (default rank_icir)")
    lsp.add_argument("--limit", type=int, default=20, help="max rows (default 20; <0 = all)")
    lsp.set_defaults(func=cmd_library, needs_config=False)

    lsp = lib_sub.add_parser("get", help="pretty-print one saved factor by id")
    lsp.add_argument("factor_id", help="library factor id")
    lsp.set_defaults(func=cmd_library, needs_config=False)

    lsp = lib_sub.add_parser("delete", help="delete one or more factors by id")
    lsp.add_argument("factor_ids", nargs="+", help="library factor id(s)")
    lsp.set_defaults(func=cmd_library, needs_config=False)

    lsp = lib_sub.add_parser("prune", help="greedy redundancy pruning of the library")
    lsp.add_argument("--redundancy-threshold", type=float, default=0.7,
                     help="signed-Spearman similarity cutoff (default 0.7)")
    lsp.add_argument("--apply", action="store_true",
                     help="actually delete (default is a dry run)")
    lsp.set_defaults(func=cmd_library, needs_config=False)

    sp = sub.add_parser("serve-api", help="run the FastAPI REST service (uvicorn)")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=8000)
    sp.add_argument("--reload", action="store_true", help="uvicorn auto-reload (dev)")
    sp.set_defaults(func=cmd_serve_api, needs_config=False)

    sp = sub.add_parser("serve-mcp", help="run the MCP server (stdio or sse)")
    sp.add_argument("--transport", default="stdio", choices=["stdio", "sse"])
    sp.add_argument("--port", type=int, default=8001, help="port for the sse transport")
    sp.set_defaults(func=cmd_serve_mcp, needs_config=False)

    sp = sub.add_parser("seed-demo", help="populate the library with Alpha101 / Alpha158 demo factors")
    sp.add_argument("--universe", help="evaluation universe (default: config default, e.g. NASDAQ100/CSI300)")
    sp.add_argument("--start", help="YYYY-MM-DD (default: config period)")
    sp.add_argument("--end", help="YYYY-MM-DD (default: config period)")
    sp.add_argument("--as-of", dest="as_of", help="point-in-time cutoff (default: end)")
    sp.add_argument("--sources", help="comma-separated subset of ALPHA101,ALPHA158 (default: both)")
    sp.set_defaults(func=cmd_seed_demo, needs_config=False)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # Normalise comma-separated --neutralize ('sector,size') into a list for the SDK.
    if getattr(args, "neutralize", None) is not None and isinstance(args.neutralize, str):
        args.neutralize = [t.strip() for t in args.neutralize.split(",") if t.strip()] or None
    config = AssayConfig.from_env() if getattr(args, "needs_config", True) else None
    args.func(args, config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
