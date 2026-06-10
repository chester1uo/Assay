"""Shared numpy helpers used across the operator-kernel categories."""

from __future__ import annotations

import contextlib
import warnings

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view


@contextlib.contextmanager
def quiet_numeric():
    """Suppress numpy's expected RuntimeWarnings (all-NaN slice / empty mean /
    ddof) — for these kernels an empty cross-section deliberately yields NaN."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        yield


def windows(x: np.ndarray, d: int) -> np.ndarray:
    """Return ``(T, N, d)`` trailing windows; window at ``t`` ends at ``t``.

    The first ``d-1`` rows are NaN-padded so any reduction over a not-yet-warm
    window yields NaN.
    """
    d = int(d)
    if d < 1:
        raise ValueError(f"window length must be >= 1, got {d}")
    x = np.asarray(x, dtype=np.float64)
    if x.ndim != 2:
        raise ValueError(f"time-series operator expects a (T, N) matrix, got shape {x.shape}")
    pad = np.full((d - 1, x.shape[1]), np.nan, dtype=np.float64)
    padded = np.concatenate((pad, x), axis=0)
    return sliding_window_view(padded, d, axis=0)  # (T, N, d)


def as2d(x: np.ndarray) -> np.ndarray:
    arr = np.asarray(x, dtype=np.float64)
    if arr.ndim != 2:
        raise ValueError(f"expected a (T, N) matrix, got shape {arr.shape}")
    return arr


def as_float(v):
    """Coerce a scalar or array operand to a float64 ndarray (broadcast-ready)."""
    return np.asarray(v, dtype=np.float64)
