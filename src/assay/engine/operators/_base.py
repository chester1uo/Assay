"""Shared numpy helpers used across the operator-kernel categories."""

from __future__ import annotations

import contextlib
import os
import warnings

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

from assay import _native


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


# cap on one block of window temporaries (MB); a reduction over ``(rows, N, d)`` views
# makes a few float64 copies of that size, so the cap bounds kernel memory at any ``d``
WINDOW_BLOCK_MB = float(os.environ.get("ASSAY_WINDOW_BLOCK_MB") or 64)


def rolling(fn, d: int, *xs: np.ndarray, native: str | None = None) -> np.ndarray:
    """``fn(*windows)`` over trailing windows of ``xs``, computed in blocks of rows.

    Same values as ``fn(*(windows(x, d) for x in xs))`` (each output element reduces
    the same window), but the ``(T, N, d)`` temporaries ``fn`` creates are limited to
    about :data:`WINDOW_BLOCK_MB` per block instead of growing with ``d``.

    ``native`` names the equivalent kernel of the Rust extension (``ts_rolling`` /
    ``ts_rolling2``), used instead when it is loaded: O(1) per step for the sum
    family, so the cost no longer grows with ``d`` (values agree to ~1e-12).
    """
    d = int(d)
    if d < 1:
        raise ValueError(f"window length must be >= 1, got {d}")
    arrs = [np.asarray(x, dtype=np.float64) for x in xs]
    for a in arrs:
        if a.ndim != 2:
            raise ValueError(f"time-series operator expects a (T, N) matrix, got shape {a.shape}")
    k = _native.kernels
    if native is not None and k is not None and all(a.shape == arrs[0].shape for a in arrs):
        arrs = [np.ascontiguousarray(a) for a in arrs]
        return k.ts_rolling(native, arrs[0], d) if len(arrs) == 1 else k.ts_rolling2(native, *arrs, d)
    T, N = arrs[0].shape
    pads = [np.concatenate((np.full((d - 1, N), np.nan), a), axis=0) for a in arrs]
    step = max(1, int(WINDOW_BLOCK_MB * 2**20 // (max(N, 1) * d * 8 * 4 * len(arrs))))
    if T == 0 or step >= T:
        return np.asarray(fn(*(sliding_window_view(p, d, axis=0) for p in pads)))
    out = None
    for a in range(0, T, step):
        b = min(T, a + step)
        r = np.asarray(fn(*(sliding_window_view(p[a:b + d - 1], d, axis=0) for p in pads)))
        if out is None:
            out = np.empty((T,) + r.shape[1:], dtype=r.dtype)
        out[a:b] = r
    return out


def as2d(x: np.ndarray) -> np.ndarray:
    arr = np.asarray(x, dtype=np.float64)
    if arr.ndim != 2:
        raise ValueError(f"expected a (T, N) matrix, got shape {arr.shape}")
    return arr


def as_float(v):
    """Coerce a scalar or array operand to a float64 ndarray (broadcast-ready)."""
    return np.asarray(v, dtype=np.float64)
