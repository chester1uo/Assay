"""Cross-sectional operator kernels (``cs_*``).

These reduce along axis 1 (symbols) within each date and are NaN-aware: a
missing symbol does not poison the whole cross-section. Group operators
(``cs_neutralize`` / ``cs_group_rank`` / ``cs_group_mean``) demean or rank within
industry groups; they receive the evaluation context (``needs_ctx=True``) to
resolve the per-symbol group labels.
"""

from __future__ import annotations

import numpy as np

from ._base import as2d, quiet_numeric
from .registry import register


def _rank01_row(row: np.ndarray) -> np.ndarray:
    out = np.full(row.shape, np.nan)
    mask = ~np.isnan(row)
    vals = row[mask]
    n = vals.size
    if n == 0:
        return out
    if n == 1:
        out[mask] = 0.5
        return out
    order = np.argsort(vals, kind="mergesort")
    ordered = vals[order]
    ranks = np.empty(n)
    i = 0
    while i < n:  # average-rank tie handling
        j = i + 1
        while j < n and ordered[j] == ordered[i]:
            j += 1
        ranks[order[i:j]] = (i + j - 1) / 2.0
        i = j
    out[mask] = ranks / (n - 1)
    return out


def cs_rank(x):
    x = as2d(x)
    return np.vstack([_rank01_row(x[t]) for t in range(x.shape[0])])


def cs_demean(x):
    x = as2d(x)
    with quiet_numeric():
        return x - np.nanmean(x, axis=1, keepdims=True)


def cs_zscore(x):
    x = as2d(x)
    with quiet_numeric(), np.errstate(invalid="ignore", divide="ignore"):
        mean = np.nanmean(x, axis=1, keepdims=True)
        std = np.nanstd(x, axis=1, ddof=1, keepdims=True)
        z = (x - mean) / std
    return np.where(std == 0, np.nan, z)


def cs_scale(x, a=1.0):
    x = as2d(x)
    with quiet_numeric(), np.errstate(invalid="ignore", divide="ignore"):
        denom = np.nansum(np.abs(x), axis=1, keepdims=True)
        scaled = float(a) * x / denom
    return np.where(denom == 0, np.nan, scaled)


def cs_winsorize(x, p):
    x = as2d(x)
    p = float(p)
    if not 0.0 < p < 0.5:
        raise ValueError("cs_winsorize(x, p) needs 0 < p < 0.5 (a tail fraction)")
    with quiet_numeric():
        lo = np.nanquantile(x, p, axis=1, keepdims=True)
        hi = np.nanquantile(x, 1.0 - p, axis=1, keepdims=True)
    return np.clip(x, lo, hi)


def _group_apply(x, labels, reduce_to_residual: bool, rank: bool):
    x = as2d(x)
    labels = np.asarray(labels)
    if labels.shape[0] != x.shape[1]:
        raise ValueError(
            f"group vector has {labels.shape[0]} labels but the panel has {x.shape[1]} symbols"
        )
    out = np.full_like(x, np.nan)
    with quiet_numeric():
        for label in np.unique(labels):
            cols = np.flatnonzero(labels == label)
            sub = x[:, cols]
            if rank:
                out[:, cols] = np.vstack([_rank01_row(sub[t]) for t in range(sub.shape[0])])
            elif reduce_to_residual:
                out[:, cols] = sub - np.nanmean(sub, axis=1, keepdims=True)
            else:
                out[:, cols] = np.nanmean(sub, axis=1, keepdims=True)
    return out


def cs_neutralize(x, group, *, ctx):
    """Group-neutralize: subtract the per-date group mean (residual of a
    cross-sectional regression on group dummies). ``group`` is a label key
    (``'sector'``, ``'industry'``, ...) resolved against the engine context."""
    return _group_apply(x, ctx.require_groups(group), reduce_to_residual=True, rank=False)


def cs_group_rank(x, group, *, ctx):
    return _group_apply(x, ctx.require_groups(group), reduce_to_residual=False, rank=True)


def cs_group_mean(x, group, *, ctx):
    return _group_apply(x, ctx.require_groups(group), reduce_to_residual=False, rank=False)


register("cs_rank", cs_rank, 1, 1, signature="cs_rank(x)", category="cross-sectional",
         output_range="[0, 1]", incremental=None)
register("cs_zscore", cs_zscore, 1, 1, signature="cs_zscore(x)", category="cross-sectional",
         output_range="(-inf, inf)", incremental=None)
register("cs_demean", cs_demean, 1, 1, signature="cs_demean(x)", category="cross-sectional",
         output_range="(-inf, inf)", incremental=None)
register("cs_scale", cs_scale, 1, 2, signature="cs_scale(x, a=1)", category="cross-sectional",
         output_range="sum(|x|) == a per date", incremental=None)
register("cs_winsorize", cs_winsorize, 2, 2, signature="cs_winsorize(x, p)",
         category="cross-sectional", output_range="same as x", incremental=None)
register("cs_neutralize", cs_neutralize, 2, 2, signature="cs_neutralize(x, group)",
         category="cross-sectional", output_range="(-inf, inf)", incremental=None,
         needs_ctx=True, common_errors=["requires group data configured on the engine"])
register("cs_group_rank", cs_group_rank, 2, 2, signature="cs_group_rank(x, group)",
         category="cross-sectional", output_range="[0, 1]", incremental=None, needs_ctx=True)
register("cs_group_mean", cs_group_mean, 2, 2, signature="cs_group_mean(x, group)",
         category="cross-sectional", output_range="same as x", incremental=None, needs_ctx=True)


__all__ = [
    "cs_rank", "cs_demean", "cs_zscore", "cs_scale", "cs_winsorize",
    "cs_neutralize", "cs_group_rank", "cs_group_mean",
]
