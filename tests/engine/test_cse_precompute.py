"""Tests for common-sub-expression analysis, CSE batch eval, and the precompute store.

Offline / synthetic — a small OHLCV panel and a handful of factors with deliberate
overlap. Verifies: (1) the CSE analysis ranks shared subtrees by recompute saved;
(2) ``evaluate_many`` is bit-for-bit identical to per-factor ``evaluate`` while
computing shared subtrees once; (3) the precompute store round-trips and accelerates
a warm pass; (4) the panel fingerprint changes with history.

    PYTHONPATH=src python -m pytest tests/engine/test_cse_precompute.py -q
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import polars as pl
import pytest

from assay.engine import FactorEngine, PrecomputeStore, common_subexpressions
from assay.engine.cse import iter_subtrees, node_size
from assay.engine.parsing import parse


def _panel(t=120, n=20, seed=1):
    rng = np.random.default_rng(seed)
    close = 100 + rng.normal(0, 1, (t, n)).cumsum(0).clip(1)
    open_ = close + rng.normal(0, 0.3, (t, n))
    high = np.maximum(open_, close) + np.abs(rng.normal(0, 0.2, (t, n)))
    low = np.minimum(open_, close) - np.abs(rng.normal(0, 0.2, (t, n)))
    vol = 1e6 + np.abs(rng.normal(0, 1, (t, n))) * 1e4
    dates = [dt.date(2022, 1, 3) + dt.timedelta(days=i) for i in range(t)]
    syms = [f"S{j:02d}" for j in range(n)]
    return pl.DataFrame({
        "date": np.repeat(np.array(dates), n), "symbol": syms * t,
        "open": open_.reshape(-1), "high": high.reshape(-1), "low": low.reshape(-1),
        "close": close.reshape(-1), "volume": vol.reshape(-1),
    })


# factors sharing 'sub(high, low)' and 'ts_mean(volume, 20)'
FACTORS = [
    "Div(Sub($high, $low), Add($close, 1e-12))",
    "Mul(Sub($high, $low), Mean($volume, 20))",
    "Div(Sub($high, $low), Add(Mean($volume, 20), 1e-12))",
    "Rank($close, 10)",
    "Sub($high, $low)",
]


# ---------------------------------------------------------------------------
# CSE analysis
# ---------------------------------------------------------------------------
def test_common_subexpressions_ranks_shared_subtrees():
    cs = common_subexpressions(FACTORS, min_count=2, min_nodes=2)
    by_expr = {c.expr: c for c in cs}
    # 'sub(high, low)' appears in 4 of the 5 factors -> a top common subexpr
    assert "sub(high, low)" in by_expr
    shl = by_expr["sub(high, low)"]
    assert shl.count >= 4 and shl.n_factors >= 4 and shl.n_nodes == 3
    assert shl.score == shl.count * (shl.n_nodes - 1)
    # 'ts_mean(volume, 20)' shared by 2 factors
    assert "ts_mean(volume, 20)" in by_expr
    # ranked by score descending
    scores = [c.score for c in cs]
    assert scores == sorted(scores, reverse=True)
    # top_k truncates
    assert len(common_subexpressions(FACTORS, top_k=2)) == 2


def test_iter_subtrees_and_node_size():
    root = parse("Div(Sub($high, $low), $close)")
    subs = list(iter_subtrees(root))
    # OpNodes: Div(...), Sub(...) — leaves ($high/$low/$close) are not yielded
    assert any(str(s) == "sub(high, low)" for s in subs)
    assert all(getattr(s, "op", None) is not None for s in subs)
    assert node_size(root) == 5  # div, sub, high, low, close


# ---------------------------------------------------------------------------
# evaluate_many == per-factor evaluate (correctness)
# ---------------------------------------------------------------------------
def test_evaluate_many_matches_per_factor_evaluate():
    eng = FactorEngine(_panel())
    many = eng.evaluate_many(FACTORS)
    assert [r.expr for r in many] == list(FACTORS)  # keeps the raw expr, like evaluate()
    for e, r in zip(FACTORS, many):
        a = eng.evaluate(e).values
        b = r.values
        # identical values where both finite, and identical NaN pattern
        assert np.array_equal(np.isfinite(a), np.isfinite(b))
        m = np.isfinite(a)
        assert np.allclose(a[m], b[m], rtol=0, atol=0)


def test_evaluate_many_shares_subtrees():
    # the shared 'sub(high, low)' subtree is computed once: the memo returns the
    # *same array object* for the second factor that needs it (object identity).
    from assay.engine.engine import EvalContext
    from assay.engine.ast import hash_tree, iter_fields

    eng = FactorEngine(_panel())
    n1 = parse("Div(Sub($high, $low), $close)")
    n2 = parse("Mul(Sub($high, $low), $volume)")
    fields = iter_fields(n1) | iter_fields(n2)
    ctx = EvalContext(eng.dates.tolist(), eng.symbols.tolist(),
                      {f: eng.field_matrix(f) for f in fields}, eng._groups)
    hashes: dict = {}
    hash_tree(n1, hashes); hash_tree(n2, hashes)
    memo: dict = {}
    eng._eval_cse(n1, ctx, memo, hashes)
    h_sub = parse("Sub($high, $low)").struct_hash()
    assert h_sub in memo
    shared_obj = memo[h_sub]
    eng._eval_cse(n2, ctx, memo, hashes)
    assert memo[h_sub] is shared_obj  # second factor reused the cached array, not recomputed


# ---------------------------------------------------------------------------
# precompute store
# ---------------------------------------------------------------------------
def test_precompute_store_roundtrip_and_warm_eval(tmp_path):
    eng = FactorEngine(_panel())
    store = PrecomputeStore(tmp_path)
    info = store.build(eng, FACTORS, top_k=50, min_count=2)
    assert info["built"] >= 1 and store.stats()["entries"] >= 1
    fp = eng.panel_fingerprint()

    bound = store.bind(fp)
    warm = eng.evaluate_many(FACTORS, precompute=bound)
    # warm results still match the naive evaluation exactly
    for e, r in zip(FACTORS, warm):
        a = eng.evaluate(e).values
        assert np.array_equal(np.isfinite(a), np.isfinite(r.values))
        m = np.isfinite(a)
        assert np.allclose(a[m], r.values[m], rtol=0, atol=0)
    # the precompute store was actually consulted and hit
    assert bound.hits >= 1 and bound.hit_rate > 0.0


def test_precompute_miss_on_wrong_fingerprint(tmp_path):
    eng = FactorEngine(_panel())
    store = PrecomputeStore(tmp_path)
    store.build(eng, FACTORS, top_k=50, min_count=2)
    bound = store.bind("not-this-panels-fingerprint")
    eng.evaluate_many(FACTORS, precompute=bound)
    assert bound.hits == 0  # nothing matches a foreign fingerprint -> all misses


def test_panel_fingerprint_changes_with_history():
    fp_short = FactorEngine(_panel(t=100)).panel_fingerprint()
    fp_long = FactorEngine(_panel(t=140)).panel_fingerprint()  # more history
    fp_same = FactorEngine(_panel(t=100)).panel_fingerprint()
    assert fp_short == fp_same          # same panel -> same fingerprint
    assert fp_short != fp_long          # growing history -> new fingerprint
