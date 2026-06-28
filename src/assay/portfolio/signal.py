"""Cross-sectional signal processing — design-doc section 1.1 (SignalProcessor), 2.4.

The ``SignalProcessor`` stage turns a raw ``(T, N)`` factor matrix into a clean,
allocation-ready signal of the same shape: ``winsorize`` -> (optional)
``neutralize`` -> ``normalize`` per the configured ``signal_transform``. Every
function operates **per date row** (the cross-section along axis 1) and is
NaN-aware — a symbol missing on a given date is ignored in that date's quantiles,
mean and std rather than poisoning the whole row, and stays NaN on output.

**Grounding to reality.** ``neutralize`` subtracts a per-date group mean (the
residual of a regression on group dummies) and is the hook for sector-neutral
signals. The DataStore has no sector/industry classification for US equities, so
``groups`` is ``None`` in practice and ``neutralize`` is a documented no-op — it
never fabricates sector labels. It activates verbatim if group labels are ever
supplied.

Matrices are ``float64`` with axis 0 = dates, axis 1 = symbols, matching the
``FactorEngine`` panel convention.
"""

from __future__ import annotations

import contextlib
import warnings

import numpy as np

from assay.portfolio.config import PortfolioBacktestConfig

__all__ = ["winsorize", "normalize", "neutralize", "process_signal"]


@contextlib.contextmanager
def _quiet():
    """Suppress numpy's expected ``RuntimeWarning``s (all-NaN slice / empty mean /
    ddof) — an all-NaN or empty cross-section deliberately yields NaN here."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        yield


def _as2d(x: np.ndarray) -> np.ndarray:
    """Coerce to a 2-D ``float64`` ``(T, N)`` matrix (1-D row -> ``(1, N)``)."""
    arr = np.asarray(x, dtype=np.float64)
    if arr.ndim == 1:
        arr = arr.reshape(1, -1)
    if arr.ndim != 2:
        raise ValueError(f"expected a (T, N) matrix, got shape {arr.shape}")
    return arr


def _rank01_row(row: np.ndarray) -> np.ndarray:
    """Cross-sectional percentile rank of one date row, in ``[0, 1]``, NaN-aware.

    Average-rank tie handling; a lone valid value maps to ``0.5``. NaNs pass
    through. Mirrors the engine's ``cs_rank`` kernel so processed signals match
    the rank semantics used elsewhere in Assay.
    """
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


def winsorize(x: np.ndarray, p: float = 0.01) -> np.ndarray:
    """Clip each date's cross-section to its ``[p, 1 - p]`` quantiles (NaN-aware).

    Tames extreme factor values before normalization so a single outlier symbol
    cannot dominate the allocation. ``p`` is a tail fraction in ``(0, 0.5)``;
    quantiles are computed per row over the finite values only, and NaNs are
    preserved (``np.clip`` leaves NaN untouched). A row that is all-NaN, or where
    a quantile is undefined, is returned unchanged.
    """
    x = _as2d(x)
    p = float(p)
    if not 0.0 < p < 0.5:
        raise ValueError("winsorize(x, p) needs 0 < p < 0.5 (a tail fraction)")
    with _quiet(), np.errstate(invalid="ignore"):
        lo = np.nanquantile(x, p, axis=1, keepdims=True)
        hi = np.nanquantile(x, 1.0 - p, axis=1, keepdims=True)
    # All-NaN rows yield NaN bounds; clip with NaN bounds is a no-op, so guard.
    lo = np.where(np.isnan(lo), -np.inf, lo)
    hi = np.where(np.isnan(hi), np.inf, hi)
    return np.clip(x, lo, hi)


def normalize(x: np.ndarray, method: str = "rank") -> np.ndarray:
    """Transform each date's cross-section by ``method`` (NaN-aware).

    ``'rank'`` -> cross-sectional percentile rank in ``[0, 1]`` (robust, the
    section-2.4 default). ``'zscore'`` -> demean / std per date (mean 0, std 1;
    a degenerate row with zero/undefined std maps to NaN). ``'raw'`` ->
    passthrough (the factor is already the desired signal). NaNs are preserved.
    """
    x = _as2d(x)
    if method == "raw":
        return x.copy()
    if method == "rank":
        return np.vstack([_rank01_row(x[t]) for t in range(x.shape[0])])
    if method == "zscore":
        with _quiet(), np.errstate(invalid="ignore", divide="ignore"):
            mean = np.nanmean(x, axis=1, keepdims=True)
            std = np.nanstd(x, axis=1, ddof=1, keepdims=True)
            z = (x - mean) / std
        return np.where(std == 0, np.nan, z)
    raise ValueError(f"normalize method={method!r} invalid; expected rank|zscore|raw")


def neutralize(
    x: np.ndarray, groups: np.ndarray | None, key: str | None = None
) -> np.ndarray:
    """Subtract the per-date group mean (group-neutral residual), NaN-aware.

    ``groups`` is a length-``N`` vector of per-symbol group labels (e.g. sector
    ids); for each date and each group the group's NaN-aware mean is removed,
    leaving the within-group residual. ``key`` is an advisory label naming the
    grouping (e.g. ``'sector'``), carried only for diagnostics.

    **No-op when ``groups`` is ``None``** (returns ``x`` unchanged). This is the
    common case for Assay's US-equity data, which has no sector/industry
    classification in the DataStore — see the module docstring. The function never
    invents labels; it neutralizes only against labels explicitly supplied.
    """
    x = _as2d(x)
    if groups is None:
        return x  # no sector data available — documented no-op
    labels = np.asarray(groups)
    if labels.shape[0] != x.shape[1]:
        raise ValueError(
            f"groups has {labels.shape[0]} labels but the signal has {x.shape[1]} symbols"
        )
    out = np.full_like(x, np.nan)
    with _quiet(), np.errstate(invalid="ignore"):
        for label in np.unique(labels):
            cols = np.flatnonzero(labels == label)
            sub = x[:, cols]
            out[:, cols] = sub - np.nanmean(sub, axis=1, keepdims=True)
    return out


def process_signal(
    factor: np.ndarray,
    config: PortfolioBacktestConfig,
    groups: np.ndarray | None = None,
) -> np.ndarray:
    """Full SignalProcessor pipeline — winsorize -> neutralize -> normalize.

    Produces a clean ``(T, N)`` signal ready for weight construction (design-doc
    1.1). Steps: (1) ``winsorize`` each cross-section to tame outliers; (2)
    ``neutralize`` against ``groups`` if supplied (no-op otherwise — no US sector
    data); (3) ``normalize`` by ``config.signal_transform`` (``rank`` /
    ``zscore`` / ``raw``). NaN-aware throughout: missing symbols stay NaN.

    The winsorize tail fraction is a fixed conservative ``0.01`` (the spec
    default for ``winsorize``); ``config`` selects only the final transform.
    """
    sig = winsorize(factor, p=0.01)
    sig = neutralize(sig, groups, key="sector")
    return normalize(sig, method=config.signal_transform)
