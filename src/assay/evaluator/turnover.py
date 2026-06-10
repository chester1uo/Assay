"""Factor turnover via cross-sectional rank autocorrelation (engineering-docs §7.2).

Pure numpy. A factor that re-shuffles its cross-sectional ordering every day
incurs high trading turnover; a slow-moving factor keeps a similar ranking from
``t-lag`` to ``t``. We measure persistence as the Spearman rank autocorrelation
between consecutive (``lag``-apart) cross-sections and report turnover as
``1 - mean(rank autocorr)`` — roughly the fraction of the ranking that churns.

Both functions are NaN-aware: each pair of dates is correlated only over the
symbols finite in **both** rows, and dates with fewer than two such symbols (or
no defined prior date) yield ``NaN`` and are dropped from the turnover mean.
"""

from __future__ import annotations

import numpy as np

from .metrics import rank_ic_series


def rank_autocorr(factor: np.ndarray, lag: int = 1) -> np.ndarray:
    """Per-date Spearman rank autocorrelation between ``factor[t-lag]`` and ``factor[t]``.

    Returns ``(T,)`` float64; the first ``lag`` entries are ``NaN`` (no prior
    cross-section). Reuses the NaN-aware Spearman kernel from :mod:`.metrics`.
    """
    factor = np.ascontiguousarray(factor, dtype=np.float64)
    if factor.ndim != 2:
        raise ValueError("factor must be a 2-D (T, N) matrix")
    lag = int(lag)
    if lag < 1:
        raise ValueError("lag must be >= 1")
    t_n, n_sym = factor.shape
    out = np.full(t_n, np.nan)
    if lag >= t_n:
        return out
    # Correlate prior cross-section (shifted down by lag) against current.
    prior = np.full((t_n, n_sym), np.nan)
    prior[lag:] = factor[: t_n - lag]
    out[:] = rank_ic_series(prior, factor)
    out[:lag] = np.nan  # no defined prior cross-section for the warm-up rows
    return out


def turnover(factor: np.ndarray, lag: int = 1) -> float:
    """Average factor turnover: ``1 - mean cross-sectional rank autocorr``.

    A value near 0 means a near-static ranking (low trading); near 1 means the
    ranking is reshuffled each period. ``NaN`` if no date has a defined
    autocorrelation.
    """
    ac = rank_autocorr(factor, lag=lag)
    finite = ac[np.isfinite(ac)]
    if finite.size == 0:
        return float("nan")
    return 1.0 - float(finite.mean())
