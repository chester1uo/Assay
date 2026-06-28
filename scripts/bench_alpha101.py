#!/usr/bin/env python3
"""Alpha-101 backtest performance benchmark — cache vs no-cache.

Measures the cost of evaluating the WorldQuant Alpha-101 factor catalog (the hot
path of any factor/portfolio backtest) under three regimes, on synthetic or real
ingested data:

* **no-cache**     — a fresh :class:`FactorEngine` per factor. Each call re-pivots
  every field matrix from scratch (and, on real data via ``from_store``, re-reads
  the point-in-time price panel from parquet). This is the cold path.
* **warm engine**  — one :class:`FactorEngine` reused across all factors. The price
  panel is loaded once and field pivots are memoised on the engine — the
  session-cache effect (engineering-docs §5.2 / §8.2: the ~220 ms panel read is
  amortised over the whole batch).
* **L2 cache**     — factor result matrices served from the on-disk
  :class:`~assay.cache.L2FactorCache` (engineering-docs §5.4): re-running a known
  expression set skips AST execution entirely.

It is correctness-checked: every regime must produce identical ``(T, N)`` factor
matrices (NaN-aware) — caching may never change a result.

Usage::

    # synthetic panel (offline, deterministic, runs all 101 alphas)
    PYTHONPATH=src python scripts/bench_alpha101.py --synthetic --n 200 --t 504

    # real ingested data (uses ASSAY_DATA_DIR; runs the alphas whose fields exist)
    PYTHONPATH=src python scripts/bench_alpha101.py --real \\
        --universe NASDAQ100 --start 2025-01-02 --end 2026-06-09
"""

from __future__ import annotations

import argparse
import datetime as dt
import tempfile
import time
from dataclasses import dataclass

import numpy as np
import polars as pl

from assay.cache import L2FactorCache
from assay.engine import FactorEngine, iter_fields, operators, parse
from assay.factors.alpha101 import ALPHA_101

# Fixed cache-key coordinates for the benchmark (the actual values are irrelevant
# — only that they are stable so the L2 key is reproducible across the two rounds).
_BENCH_UNIVERSE = "BENCH"
_BENCH_PERIOD = ("2020-01-01", "2024-12-31")
_BENCH_ADJ = "split"
_BENCH_MARKET = "US"

_ALL_FIELDS = ("open", "high", "low", "close", "volume", "vwap", "market_cap")
_GROUP_KEYS = ("sector", "industry", "subindustry")


# --------------------------------------------------------------------------- data
def build_synthetic_panel(
    n_symbols: int, n_days: int, *, seed: int = 7
) -> tuple[pl.DataFrame, dict[str, dict[str, str]]]:
    """A deterministic OHLCV+vwap+market_cap panel plus 3 industry groupings.

    Returns ``(panel, group_data)`` ready for ``FactorEngine(panel, group_data)``.
    Prices follow a gentle geometric random walk; volume/market_cap are positive.
    """
    rng = np.random.default_rng(seed)
    t, n = n_days, n_symbols
    ret = rng.normal(0.0002, 0.018, size=(t, n))
    close = 50.0 * np.exp(np.cumsum(ret, axis=0))
    spread = np.abs(rng.normal(0, 0.01, size=(t, n)))
    high = close * (1 + spread)
    low = close * (1 - spread)
    open_ = low + (high - low) * rng.random((t, n))
    vwap = (open_ + high + low + close) / 4.0
    volume = np.abs(rng.normal(2e6, 4e5, size=(t, n))) + 1.0
    shares = rng.uniform(1e8, 5e9, size=n)
    market_cap = close * shares  # (t, n)

    dates = [dt.date(2021, 1, 4) + dt.timedelta(days=i) for i in range(t)]
    symbols = [f"S{j:03d}" for j in range(n)]
    mats = {
        "open": open_, "high": high, "low": low, "close": close,
        "volume": volume, "vwap": vwap, "market_cap": market_cap,
    }
    data = {"date": np.repeat(np.array(dates), n), "symbol": symbols * t}
    for name, mat in mats.items():
        data[name] = mat.reshape(-1)
    panel = pl.DataFrame(data)

    # Assign each symbol to a sector/industry/subindustry bucket (synthetic labels).
    groups: dict[str, dict[str, str]] = {}
    for key, k in (("sector", 11), ("industry", 24), ("subindustry", 60)):
        groups[key] = {s: f"{key[:3]}{j % k}" for j, s in enumerate(symbols)}
    return panel, groups


# ----------------------------------------------------------------------- selection
def select_alphas(
    panel_fields: set[str], *, have_groups: bool, limit: int | None = None
) -> list[tuple[int, str]]:
    """The Alpha-101 expressions runnable given the available fields/groups.

    An alpha is runnable when it parses, all its operators are registered, all its
    fields are present, and (if it references a group neutralisation) group data is
    available. Returned sorted by alpha number, optionally capped at ``limit``.
    """
    runnable: list[tuple[int, str]] = []
    for num, expr in sorted(ALPHA_101.items()):
        try:
            node = parse(expr)
        except Exception:
            continue
        if any(not operators.is_registered(op) for op in _ops(node)):
            continue
        if not iter_fields(node) <= panel_fields:
            continue
        if not have_groups and any(g in expr for g in _GROUP_KEYS):
            continue
        runnable.append((num, expr))
    return runnable[:limit] if limit else runnable


def _ops(node) -> set[str]:
    from assay.engine import iter_ops

    return iter_ops(node)


# ------------------------------------------------------------------------ timing
@dataclass
class BenchResult:
    label: str
    n_factors: int
    total_s: float
    per_factor_ms: float
    factors_per_s: float

    @classmethod
    def of(cls, label: str, n: int, total_s: float) -> "BenchResult":
        per = (total_s / n * 1e3) if n else float("nan")
        fps = (n / total_s) if total_s > 0 else float("inf")
        return cls(label, n, total_s, per, fps)


def _values(eng: FactorEngine, expr: str) -> np.ndarray:
    return eng.evaluate(expr).values


def bench_no_cache(
    exprs: list[tuple[int, str]], panel: pl.DataFrame, groups: dict
) -> tuple[BenchResult, dict[int, np.ndarray]]:
    """Cold path: a brand-new engine per factor (re-pivots fields every time)."""
    out: dict[int, np.ndarray] = {}
    t0 = time.perf_counter()
    for num, expr in exprs:
        out[num] = _values(FactorEngine(panel, group_data=groups), expr)
    return BenchResult.of("no-cache (fresh engine/factor)", len(exprs), time.perf_counter() - t0), out


def bench_warm_engine(
    exprs: list[tuple[int, str]], panel: pl.DataFrame, groups: dict
) -> tuple[BenchResult, dict[int, np.ndarray]]:
    """Warm path: one reused engine (panel loaded once, field pivots memoised)."""
    eng = FactorEngine(panel, group_data=groups)
    out: dict[int, np.ndarray] = {}
    t0 = time.perf_counter()
    for num, expr in exprs:
        out[num] = _values(eng, expr)
    return BenchResult.of("warm engine (panel/pivot cache)", len(exprs), time.perf_counter() - t0), out


def bench_l2(
    exprs: list[tuple[int, str]], panel: pl.DataFrame, groups: dict, cache_dir: str
) -> tuple[BenchResult, BenchResult, dict[int, np.ndarray]]:
    """L2 result cache: round 1 computes + stores; round 2 loads from disk.

    Returns ``(cold_compute, warm_load, results)``. The warm-load timing is what a
    re-run of an already-evaluated factor set costs.
    """
    eng = FactorEngine(panel, group_data=groups)
    cache = L2FactorCache(cache_dir)
    keys = {num: cache.key(str(parse(expr)), _BENCH_UNIVERSE, _BENCH_PERIOD, _BENCH_ADJ, _BENCH_MARKET)
            for num, expr in exprs}

    out: dict[int, np.ndarray] = {}
    t0 = time.perf_counter()  # round 1 — compute + put (cold)
    for num, expr in exprs:
        v = _values(eng, expr)
        cache.put(keys[num], v)
        out[num] = v
    cold = BenchResult.of("L2 cold (compute + store)", len(exprs), time.perf_counter() - t0)

    t1 = time.perf_counter()  # round 2 — get (warm; no AST execution)
    for num, _ in exprs:
        got = cache.get(keys[num])
        if got is None:  # pragma: no cover - cache should always hit here
            raise RuntimeError(f"L2 miss on warm round for alpha {num}")
    warm = BenchResult.of("L2 warm (load from disk)", len(exprs), time.perf_counter() - t1)
    return cold, warm, out


# ------------------------------------------------------------------- correctness
def max_abs_diff(a: dict[int, np.ndarray], b: dict[int, np.ndarray]) -> float:
    """Largest absolute difference between two result sets.

    Returns ``inf`` on a shape or NaN-pattern mismatch (caching must reproduce the
    exact NaN mask, not just the finite values); otherwise the max abs diff over
    the finite cells. NaN-vs-NaN counts as equal.
    """
    worst = 0.0
    for num in a.keys() & b.keys():
        x, y = a[num], b[num]
        if x.shape != y.shape:
            return float("inf")
        # Fast exact path: bit-identical incl. NaN/±inf (the L2/warm cases). Avoids
        # any subtraction (no inf-inf RuntimeWarning) when results match exactly.
        if np.array_equal(x, y, equal_nan=True):
            continue
        # They differ: the finite mask must still match, then quantify on finite cells.
        fx, fy = np.isfinite(x), np.isfinite(y)
        if not np.array_equal(fx, fy):
            return float("inf")
        if fx.any():
            worst = max(worst, float(np.max(np.abs(x[fx] - y[fx]))))
        else:
            return float("inf")  # differ only on non-finite cells
    return worst


# ---------------------------------------------------------------------- real data
def bench_real_data(
    universe: str, start: str, end: str, *, as_of: str | None = None, adj: str = "split",
    limit: int | None = None,
) -> dict:
    """Benchmark over real ingested data via the DataStore (no network).

    ``no-cache`` rebuilds the engine through ``FactorEngine.from_store`` for every
    factor (re-reading the PIT parquet panel each time); ``warm`` builds it once.
    This is the realistic session-cache story (engineering-docs §8.2).
    """
    from assay.config import AssayConfig
    from assay.data.store import DataStore

    store = DataStore(AssayConfig.from_env())
    period = (start, end)
    aod = as_of or end
    # discover the real field set, then pick runnable alphas
    probe = FactorEngine.from_store(store, universe, period, aod, adj=adj)
    fields = set(probe._field_cols)  # noqa: SLF001 - benchmark introspection
    exprs = select_alphas(fields, have_groups=False, limit=limit)

    out_cold: dict[int, np.ndarray] = {}
    t0 = time.perf_counter()
    for num, expr in exprs:
        eng = FactorEngine.from_store(store, universe, period, aod, adj=adj)
        out_cold[num] = eng.evaluate(expr).values
    no_cache = BenchResult.of("no-cache (from_store/factor)", len(exprs), time.perf_counter() - t0)

    eng = FactorEngine.from_store(store, universe, period, aod, adj=adj)
    out_warm: dict[int, np.ndarray] = {}
    t1 = time.perf_counter()
    for num, expr in exprs:
        out_warm[num] = eng.evaluate(expr).values
    warm = BenchResult.of("warm engine (session cache)", len(exprs), time.perf_counter() - t1)

    return {
        "shape": (len(probe.dates), len(probe.symbols)),
        "n_factors": len(exprs),
        "results": [no_cache, warm],
        "max_abs_diff": max_abs_diff(out_cold, out_warm),
    }


# ------------------------------------------------------------------------- report
def format_report(title: str, shape: tuple[int, int], results: list[BenchResult],
                  max_diff: float, baseline_label: str | None = None) -> str:
    """A fixed-width performance table with speedups vs the first (baseline) row."""
    base = results[0].total_s if results else float("nan")
    lines = [
        "",
        f"  {title}",
        f"  panel: {shape[0]} dates x {shape[1]} symbols  |  {len(results) and results[0].n_factors} alphas",
        "  " + "-" * 78,
        f"  {'regime':<34}{'total':>11}{'ms/factor':>12}{'factors/s':>12}{'speedup':>9}",
        "  " + "-" * 78,
    ]
    for r in results:
        speed = f"{base / r.total_s:5.1f}x" if r.total_s > 0 else "   inf"
        speed = "  base" if r is results[0] else speed
        lines.append(f"  {r.label:<34}{r.total_s * 1e3:>9.1f}ms{r.per_factor_ms:>11.3f}{r.factors_per_s:>12.0f}{speed:>9}")
    lines += [
        "  " + "-" * 78,
        f"  correctness: max |Δ| across regimes = {max_diff:.2e}  "
        + ("(identical ✓)" if max_diff < 1e-9 else "(MISMATCH!)"),
        "",
    ]
    return "\n".join(lines)


def run_synthetic(n_symbols: int, n_days: int, *, limit: int | None = None, seed: int = 7) -> dict:
    """Run all three regimes on a synthetic panel and return a structured result."""
    panel, groups = build_synthetic_panel(n_symbols, n_days, seed=seed)
    fields = set(panel.columns) - {"date", "symbol"}
    exprs = select_alphas(fields, have_groups=True, limit=limit)
    nc, r_nc = bench_no_cache(exprs, panel, groups)
    we, r_we = bench_warm_engine(exprs, panel, groups)
    with tempfile.TemporaryDirectory() as d:
        l2c, l2w, r_l2 = bench_l2(exprs, panel, groups, d)
    diff = max(max_abs_diff(r_nc, r_we), max_abs_diff(r_nc, r_l2))
    return {
        "shape": (n_days, n_symbols),
        "n_factors": len(exprs),
        "results": [nc, we, l2c, l2w],
        "max_abs_diff": diff,
    }


# ---------------------------------------------------------------------------- CLI
def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Alpha-101 backtest performance benchmark (cache vs no-cache)")
    p.add_argument("--real", action="store_true", help="benchmark over real ingested data (ASSAY_DATA_DIR)")
    p.add_argument("--synthetic", action="store_true", help="benchmark over a synthetic panel (default)")
    p.add_argument("--n", type=int, default=150, help="synthetic: number of symbols")
    p.add_argument("--t", type=int, default=378, help="synthetic: number of trading days")
    p.add_argument("--limit", type=int, default=None, help="cap the number of alphas (default: all runnable)")
    p.add_argument("--universe", default="NASDAQ100")
    p.add_argument("--start", default="2025-01-02")
    p.add_argument("--end", default="2026-06-09")
    p.add_argument("--adj", default="split")
    args = p.parse_args(argv)

    if args.real:
        res = bench_real_data(args.universe, args.start, args.end, adj=args.adj, limit=args.limit)
        print(format_report(
            f"Alpha-101 real-data backtest performance — {args.universe} {args.start}..{args.end}",
            res["shape"], res["results"], res["max_abs_diff"]))
    else:
        res = run_synthetic(args.n, args.t, limit=args.limit)
        print(format_report("Alpha-101 backtest performance — synthetic panel",
                            res["shape"], res["results"], res["max_abs_diff"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
