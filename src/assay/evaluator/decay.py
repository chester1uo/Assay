"""Signal decay / half-life estimation (engineering-docs §6.3).

Pure numpy. Given mean IC at each holding horizon, fit the exponential decay
model ``IC(h) = IC(1) * exp(-lambda * h)`` by ordinary least squares on
``log(IC(h))`` versus ``h`` (log-linear regression). The half-life is then
``ln(2) / lambda`` trading days.

Only **positive-IC** horizons enter the fit (the log is undefined otherwise, and
a flipped sign carries no decay information). With fewer than two positive points
the slope is unidentifiable and we return ``None``. A non-decaying or
strengthening signal (``lambda <= 0``) likewise has no finite half-life and
returns ``None``.
"""

from __future__ import annotations

import math

import numpy as np


def decay_halflife(ic_by_horizon: dict[int, float]) -> float | None:
    """Estimate decay half-life in trading days, or ``None`` if not identifiable.

    Parameters
    ----------
    ic_by_horizon
        Mapping ``horizon -> mean IC`` (typically RankIC), e.g. the
        ``ic_by_horizon`` field of :func:`~assay.evaluator.metrics.evaluate_ic`.
    """
    # Keep horizons with a finite, strictly-positive IC; sort by horizon.
    pts = [
        (float(h), float(v))
        for h, v in ic_by_horizon.items()
        if np.isfinite(v) and v > 0.0
    ]
    if len(pts) < 2:
        return None
    pts.sort(key=lambda p: p[0])
    h = np.array([p[0] for p in pts], dtype=np.float64)
    y = np.log(np.array([p[1] for p in pts], dtype=np.float64))

    # OLS slope of log(IC) on h:  log IC(h) = a - lambda * h  ->  slope = -lambda.
    h_mean = h.mean()
    y_mean = y.mean()
    denom = float(((h - h_mean) ** 2).sum())
    if denom == 0.0:  # all horizons identical — degenerate
        return None
    slope = float(((h - h_mean) * (y - y_mean)).sum() / denom)
    lam = -slope
    if lam <= 0.0:  # non-decaying / strengthening signal
        return None
    return math.log(2.0) / lam
