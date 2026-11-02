"""The vectorized cross-sectional rank and quantile buckets reproduce the
row-by-row reference exactly (ties, NaNs, single names, empty rows)."""

from __future__ import annotations

import numpy as np

from assay.engine.operators.cross_sectional import _rank01_row, cs_rank
from assay.evaluator.groups import group_returns


def _panel(seed=3, t=60, n=40):
    rng = np.random.default_rng(seed)
    x = np.round(rng.normal(size=(t, n)), 1)          # rounding makes many ties
    x[rng.random(x.shape) < 0.15] = np.nan
    x[5] = np.nan                                      # empty row
    x[6, 1:] = np.nan                                  # one name
    x[7] = 2.0                                         # all tied
    x[8, 0], x[8, 1] = np.inf, -np.inf                 # infinities are ranked, not dropped
    return x


def test_cs_rank_matches_the_row_reference():
    x = _panel()
    ref = np.vstack([_rank01_row(r) for r in x])
    np.testing.assert_array_equal(cs_rank(x), ref)


def _group_reference(factor, fwd, n_groups=5):
    t_n = factor.shape[0]
    sums, counts = np.zeros((t_n, n_groups)), np.zeros((t_n, n_groups))
    for t in range(t_n):
        mask = np.isfinite(factor[t]) & np.isfinite(fwd[t])
        m = int(mask.sum())
        if m < n_groups:
            continue
        order = np.argsort(factor[t][mask], kind="mergesort")
        edges = (np.arange(m) * n_groups) // m
        for pos in range(m):
            sums[t, edges[pos]] += fwd[t][mask][order[pos]]
            counts[t, edges[pos]] += 1.0
    with np.errstate(invalid="ignore"):
        per = np.where(counts > 0, sums / counts, np.nan)
    return [float(per[:, g][np.isfinite(per[:, g])].mean()) for g in range(n_groups)]


def test_group_returns_match_the_loop_reference():
    x = _panel()
    fwd = np.random.default_rng(9).normal(size=x.shape)
    got = group_returns(x, fwd)["quantile_returns"]
    assert [got[f"Q{g + 1}"] for g in range(5)] == _group_reference(x, fwd)
