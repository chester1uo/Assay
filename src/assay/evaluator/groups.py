"""Quantile-group forward returns and long-short spread (engineering-docs §6).

Pure numpy. Each date is sorted cross-sectionally by factor value into
``n_groups`` equal-count quantile buckets (Q1 = lowest factor value, Qn =
highest), and the mean forward return of each bucket is averaged over its members
and then over time. The long-short spread is ``mean(Qn) - mean(Q1)`` — the
return of a portfolio long the top quantile and short the bottom. ``monotonic``
reports whether the per-quantile mean returns increase strictly from Q1 to Qn.

NaN-aware: on each date only symbols finite in **both** the factor and the
forward-return rows are bucketed; dates with too few valid symbols contribute
nothing to the quantile means.
"""

from __future__ import annotations

import numpy as np


def group_returns(
    factor: np.ndarray, fwd: np.ndarray, n_groups: int = 5
) -> dict[str, object]:
    """Mean forward return per factor quantile, plus long-short spread.

    Returns ``{"quantile_returns": {"Q1": .., ..., "Qn": ..}, "long_short": float,
    "monotonic": bool}``. ``Q1`` is the lowest-factor bucket.
    """
    factor = np.asarray(factor, dtype=np.float64)
    fwd = np.asarray(fwd, dtype=np.float64)
    if factor.shape != fwd.shape:
        raise ValueError("factor and fwd must share the same (T, N) shape")
    n_groups = int(n_groups)
    if n_groups < 2:
        raise ValueError("n_groups must be >= 2")
    t_n = factor.shape[0]

    # Per-date sum and count of forward returns landing in each quantile bucket.
    sums = np.zeros((t_n, n_groups), dtype=np.float64)
    counts = np.zeros((t_n, n_groups), dtype=np.float64)

    for t in range(t_n):
        mask = np.isfinite(factor[t]) & np.isfinite(fwd[t])
        m = int(mask.sum())
        if m < n_groups:  # not enough names to populate every bucket
            continue
        fvals = factor[t][mask]
        rvals = fwd[t][mask]
        order = np.argsort(fvals, kind="mergesort")  # ascending: low factor first
        # Equal-count buckets via positional edges (handles ragged remainders).
        edges = (np.arange(m) * n_groups) // m  # bucket id per sorted position
        for pos in range(m):
            g = edges[pos]
            sums[t, g] += rvals[order[pos]]
            counts[t, g] += 1.0

    # Per-date bucket means, then average over dates (ignoring empty dates).
    with np.errstate(invalid="ignore"):
        per_date = np.where(counts > 0, sums / counts, np.nan)
    q_means = np.full(n_groups, np.nan)
    for g in range(n_groups):
        col = per_date[:, g]
        finite = col[np.isfinite(col)]
        if finite.size:
            q_means[g] = float(finite.mean())

    quantile_returns = {f"Q{g + 1}": float(q_means[g]) for g in range(n_groups)}
    lo, hi = q_means[0], q_means[-1]
    long_short = float(hi - lo) if np.isfinite(lo) and np.isfinite(hi) else float("nan")

    if np.all(np.isfinite(q_means)):
        monotonic = bool(np.all(np.diff(q_means) > 0.0))
    else:
        monotonic = False

    return {
        "quantile_returns": quantile_returns,
        "long_short": long_short,
        "monotonic": monotonic,
    }
