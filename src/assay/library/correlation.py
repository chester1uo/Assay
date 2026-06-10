"""Factor correlation & redundancy — engineering-docs section 6 (CorrelationAnalyzer)
and section 7.2 (``redundancy_score`` / ``most_similar_factor``).

Two factors are *redundant* when they rank the cross-section the same way, date by
date — the same signed bet under any monotone rescaling. We measure that with the
cross-sectional **Spearman** rank correlation averaged over dates (engineering-docs
6.2 defines RankIC as Spearman between factor and forward returns; here both sides
are factors). The score is **signed**: a factor and its negation correlate at -1,
which the library treats as fully redundant via ``abs`` at the decision boundary.

Everything operates on numpy ``(T, N)`` float64 matrices (axis 0 = dates, axis 1 =
cross-section) and is NaN-aware: a date is only scored on symbols present in *both*
factors, and a missing symbol never poisons the rest of that date's cross-section.
Dates with fewer than two jointly-present, non-degenerate observations contribute
nothing (they are skipped, not counted as zero).
"""

from __future__ import annotations

import numpy as np

__all__ = [
    "factor_similarity",
    "correlation_matrix",
    "redundancy_score",
    "prune",
]


# ---------------------------------------------------------------------------
# Cross-sectional Spearman, one date
# ---------------------------------------------------------------------------
def _rankdata_masked(vals: np.ndarray) -> np.ndarray:
    """Average ranks (1..n) of a 1-D finite array, ties shared (Spearman convention)."""
    n = vals.size
    order = np.argsort(vals, kind="mergesort")
    ordered = vals[order]
    ranks = np.empty(n, dtype=np.float64)
    i = 0
    while i < n:  # average-rank tie handling
        j = i + 1
        while j < n and ordered[j] == ordered[i]:
            j += 1
        ranks[order[i:j]] = (i + j + 1) / 2.0  # mean of 1-based ranks i+1..j
        i = j
    return ranks


def _spearman_row(a: np.ndarray, b: np.ndarray) -> float:
    """Signed Spearman correlation over symbols jointly present (finite) in both.

    Returns ``nan`` when fewer than two joint observations exist or either side is
    constant on the joint support (correlation undefined).
    """
    mask = np.isfinite(a) & np.isfinite(b)
    if mask.sum() < 2:
        return np.nan
    ra = _rankdata_masked(a[mask])
    rb = _rankdata_masked(b[mask])
    ra = ra - ra.mean()
    rb = rb - rb.mean()
    denom = np.sqrt(np.dot(ra, ra) * np.dot(rb, rb))
    if denom == 0.0:  # a constant column has zero rank variance
        return np.nan
    return float(np.dot(ra, rb) / denom)


# ---------------------------------------------------------------------------
# Pairwise similarity
# ---------------------------------------------------------------------------
def factor_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Mean over dates of the cross-sectional Spearman correlation of two factors.

    ``a`` and ``b`` are ``(T, N)`` matrices on the same (date, symbol) grid. Per
    date we rank-correlate the two cross-sections (NaN-aware, signed); the result is
    the mean over dates that yielded a defined correlation. Returns ``0.0`` when no
    date is scorable (e.g. all-NaN or single-symbol panels) — a safe "unrelated".

    Identical matrices score ``1.0``; a factor versus its negation scores ``-1.0``.
    """
    a = np.asarray(a, dtype=np.float64)
    b = np.asarray(b, dtype=np.float64)
    if a.shape != b.shape:
        raise ValueError(f"factor_similarity: shape mismatch {a.shape} vs {b.shape}")
    a = np.atleast_2d(a)
    b = np.atleast_2d(b)
    per_date = [_spearman_row(a[t], b[t]) for t in range(a.shape[0])]
    finite = [c for c in per_date if np.isfinite(c)]
    if not finite:
        return 0.0
    return float(np.mean(finite))


# ---------------------------------------------------------------------------
# Full matrix
# ---------------------------------------------------------------------------
def correlation_matrix(values_by_id: dict[str, np.ndarray]) -> dict:
    """Symmetric signed-Spearman similarity matrix over a set of factors.

    ``values_by_id`` maps ``factor_id -> (T, N)`` matrix (all on a shared grid).
    Returns ``{"factor_ids": [...], "matrix": [[...]]}`` with the ids in insertion
    order, ``matrix[i][j] == factor_similarity(values[i], values[j])`` and the
    diagonal pinned to ``1.0``. Empty input yields empty axes.
    """
    ids = list(values_by_id.keys())
    n = len(ids)
    mat = [[0.0] * n for _ in range(n)]
    for i in range(n):
        mat[i][i] = 1.0
    for i in range(n):
        for j in range(i + 1, n):
            s = factor_similarity(values_by_id[ids[i]], values_by_id[ids[j]])
            mat[i][j] = s
            mat[j][i] = s
    return {"factor_ids": ids, "matrix": mat}


# ---------------------------------------------------------------------------
# Redundancy of one factor against a library
# ---------------------------------------------------------------------------
def redundancy_score(
    target: np.ndarray, others: dict[str, np.ndarray]
) -> tuple[float, str | None]:
    """Closest match of ``target`` against ``others`` — the section-7.2 fields.

    Returns ``(max |similarity|, argmax_id)``: the redundancy score is the *absolute*
    Spearman similarity (sign-agnostic — a negated duplicate is just as redundant),
    and the id is the most-similar library factor. Empty ``others`` -> ``(0.0, None)``.
    """
    best_score = 0.0
    best_id: str | None = None
    for fid, mat in others.items():
        s = abs(factor_similarity(target, mat))
        if best_id is None or s > best_score:
            best_score = s
            best_id = fid
    if best_id is None:
        return 0.0, None
    return best_score, best_id


# ---------------------------------------------------------------------------
# Pruning a correlated set
# ---------------------------------------------------------------------------
def prune(
    matrix: list[list[float]],
    factor_ids: list[str],
    scores: dict[str, float],
    threshold: float = 0.7,
) -> dict:
    """Greedy redundancy pruning over a similarity matrix (engineering-docs 6/CLI).

    For every pair ``(i, j)`` with ``abs(matrix[i][j]) >= threshold`` we keep the
    factor with the higher quality ``scores`` value (``rank_icir``; missing -> -inf)
    and mark the weaker one for deletion. Ties break on ``factor_id`` order so the
    result is deterministic.

    Returns ``{"would_delete": [ids], "kept": [ids], "pairs_over_threshold": int}``
    where ``would_delete`` and ``kept`` partition ``factor_ids`` (order preserved).
    """
    thr = abs(float(threshold))
    n = len(factor_ids)
    doomed: set[str] = set()
    pairs_over = 0
    for i in range(n):
        for j in range(i + 1, n):
            if abs(matrix[i][j]) < thr:
                continue
            pairs_over += 1
            fi, fj = factor_ids[i], factor_ids[j]
            si = scores.get(fi, float("-inf"))
            sj = scores.get(fj, float("-inf"))
            # drop the lower-quality one; tie -> drop the later id for determinism
            if si > sj:
                loser = fj
            elif sj > si:
                loser = fi
            else:
                loser = max(fi, fj)
            doomed.add(loser)
    would_delete = [f for f in factor_ids if f in doomed]
    kept = [f for f in factor_ids if f not in doomed]
    return {
        "would_delete": would_delete,
        "kept": kept,
        "pairs_over_threshold": pairs_over,
    }
