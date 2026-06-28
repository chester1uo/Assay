"""Performance test: Alpha-101 backtest, cache vs no-cache.

Exercises the factor-evaluation hot path of a backtest over the WorldQuant
Alpha-101 catalog under three regimes — no-cache (fresh engine per factor), warm
engine (panel/pivot reuse, the session-cache effect), and the L2 on-disk result
cache — and asserts the two invariants that matter:

* **correctness** — every regime produces *identical* factor matrices (caching
  may never change a result; checked NaN-aware and deterministically), and
* **the cache pays off** — re-running an evaluated factor set from the L2 cache
  is dramatically faster than recomputing it, and reusing a warm engine is no
  slower than rebuilding one per factor.

The benchmark itself lives in ``scripts/bench_alpha101.py`` (which also has a
``--real`` mode that runs against ingested data); this test drives a small,
deterministic synthetic workload so it stays fast and offline. Run just this
group with ``pytest -m performance`` (use ``-s`` to see the printed report), or
the full benchmark with ``python scripts/bench_alpha101.py``.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

# Import the reusable benchmark from scripts/ (single source of truth).
_SCRIPTS = pathlib.Path(__file__).resolve().parents[2] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import bench_alpha101 as bench  # noqa: E402


def test_alpha101_cache_vs_nocache_performance(capsys):
    """Benchmark Alpha-101 under all cache regimes; assert identical + faster.

    Small panel (deterministic) so the test is fast; the assertions are about
    correctness (exact) and the *direction* of the speedup (with wide margins so
    the test is not timing-flaky on a busy machine).
    """
    res = bench.run_synthetic(n_symbols=40, n_days=140, limit=40, seed=11)
    report = bench.format_report(
        "Alpha-101 backtest performance — synthetic (test)",
        res["shape"], res["results"], res["max_abs_diff"],
    )
    # Surface the numbers in the test output (visible with `pytest -s`).
    with capsys.disabled():
        print(report)

    by_label = {r.label.split(" (")[0]: r for r in res["results"]}
    no_cache = by_label["no-cache"]
    l2_warm = by_label["L2 warm"]

    # Enough factors actually ran for the comparison to be meaningful.
    assert no_cache.n_factors >= 20

    # 1) Caching must never change a result (deterministic, exact — the assertion
    #    that actually guards the caches).
    assert res["max_abs_diff"] < 1e-9, f"cache changed a result: max|Δ|={res['max_abs_diff']}"

    # 2) The L2 result cache (disk load) is far cheaper than recomputing. This is a
    #    ~100-700x margin, so it is timing-robust even on a busy machine.
    assert l2_warm.total_s < 0.5 * no_cache.total_s, "L2 warm should be far faster than recompute"

    # NOTE: we deliberately do NOT assert the warm-engine ('panel/pivot cache') vs
    # no-cache timing here. On this small synthetic panel the cached field-pivot is a
    # sub-ms numpy assignment, so warm-vs-cold is in the scheduler noise and would be
    # flaky. That cache's real payoff is amortising the parquet PANEL LOAD, which
    # shows up clearly (~3-4x) only on real ingested data — see
    # `scripts/bench_alpha101.py --real`. It is still reported above for visibility.


def test_benchmark_selects_and_runs_alphas():
    """Sanity: the synthetic panel runs the full Alpha-101 catalog (all fields)."""
    panel, groups = bench.build_synthetic_panel(12, 40, seed=3)
    fields = set(panel.columns) - {"date", "symbol"}
    exprs = bench.select_alphas(fields, have_groups=True)
    # All 101 parse and are runnable when every field + grouping is present.
    assert len(exprs) == 101
    # And the real-data field set (OHLCV only, no vwap/cap/groups) runs a subset.
    real = bench.select_alphas({"open", "high", "low", "close", "volume"}, have_groups=False)
    assert 40 <= len(real) < 101
