"""Time-series operator kernels (``ts_*``).

These reduce along axis 0 (time) within each symbol. A rolling window of length
``d`` ending at day ``t`` is *full*: if any value in it is NaN (including the
``d-1`` leading warm-up days) the result for that day is NaN. ``ts_ema`` /
``ts_dema`` are the exception — they are recurrences seeded at the first
observation, so they are finite from the first non-NaN day.
"""

from __future__ import annotations

import numpy as np

from ._base import as2d, windows
from .registry import register


def ts_delay(x, d):
    x = as2d(x)
    d = int(d)
    if d < 0:
        raise ValueError("ts_delay(x, d) needs d >= 0 (a negative shift would look ahead)")
    out = np.full_like(x, np.nan)
    if d == 0:
        return x.copy()
    out[d:] = x[:-d]
    return out


def ts_delta(x, d):
    return as2d(x) - ts_delay(x, d)


def ts_returns(x, d):
    prev = ts_delay(x, d)
    with np.errstate(divide="ignore", invalid="ignore"):
        out = as2d(x) / prev - 1.0
    return np.where(prev == 0, np.nan, out)


def ts_log_returns(x, d):
    prev = ts_delay(x, d)
    ratio = as2d(x) / prev
    return np.where(ratio > 0, np.log(np.where(ratio > 0, ratio, 1.0)), np.nan)


def ts_mean(x, d):
    return windows(x, d).mean(axis=-1)


def ts_sum(x, d):
    return windows(x, d).sum(axis=-1)


def ts_product(x, d):
    return windows(x, d).prod(axis=-1)


def ts_std(x, d):
    if int(d) < 2:
        raise ValueError("ts_std(x, d) needs d >= 2")
    return windows(x, d).std(axis=-1, ddof=1)


def ts_min(x, d):
    return windows(x, d).min(axis=-1)


def ts_max(x, d):
    return windows(x, d).max(axis=-1)


def _arg_extreme(x, d, want_max: bool):
    w = windows(x, d)
    valid = ~np.isnan(w).any(axis=-1)
    filled = np.where(np.isnan(w), -np.inf if want_max else np.inf, w)
    idx = filled.argmax(axis=-1) if want_max else filled.argmin(axis=-1)
    days_ago = (int(d) - 1) - idx  # 0 == today, d-1 == oldest in window
    out = days_ago.astype(np.float64)
    out[~valid] = np.nan
    return out


def ts_argmax(x, d):
    return _arg_extreme(x, d, want_max=True)


def ts_argmin(x, d):
    return _arg_extreme(x, d, want_max=False)


def ts_rank(x, d):
    d = int(d)
    if d < 2:
        raise ValueError("ts_rank(x, d) needs d >= 2")
    w = windows(x, d)
    cur = w[..., -1:]
    valid = ~np.isnan(w).any(axis=-1)
    le = (w <= cur).sum(axis=-1)  # count of window values <= today (incl. today)
    out = ((le - 1) / (d - 1)).astype(np.float64)
    out[~valid] = np.nan
    return out


def ts_decay_linear(x, d):
    d = int(d)
    w = windows(x, d)
    weights = np.arange(1, d + 1, dtype=np.float64)  # newest gets the largest weight
    weights /= weights.sum()
    return (w * weights).sum(axis=-1)


def ts_ema(x, d):
    x = as2d(x)
    alpha = 2.0 / (int(d) + 1.0)
    out = np.empty_like(x)
    prev = np.full(x.shape[1], np.nan)
    for t in range(x.shape[0]):
        xt = x[t]
        stepped = alpha * xt + (1.0 - alpha) * prev
        stepped = np.where(np.isnan(prev), xt, stepped)  # seed on first observation
        stepped = np.where(np.isnan(xt), prev, stepped)  # carry through gaps
        out[t] = stepped
        prev = stepped
    return out


def ts_dema(x, d):
    ema1 = ts_ema(x, d)
    return 2.0 * ema1 - ts_ema(ema1, d)


def _central_moment(x, d, power):
    w = windows(x, d)
    dev = w - w.mean(axis=-1, keepdims=True)
    return (dev**power).mean(axis=-1), (dev**2).mean(axis=-1)


def ts_skew(x, d):
    m3, m2 = _central_moment(x, d, 3)
    with np.errstate(invalid="ignore", divide="ignore"):
        g1 = m3 / m2**1.5
    return np.where(m2 == 0, np.nan, g1)


def ts_kurt(x, d):
    m4, m2 = _central_moment(x, d, 4)
    with np.errstate(invalid="ignore", divide="ignore"):
        g2 = m4 / m2**2 - 3.0  # excess (Fisher) kurtosis
    return np.where(m2 == 0, np.nan, g2)


def _rolling_pair(x, y, d):
    wx, wy = windows(x, d), windows(y, d)
    mx = wx.mean(axis=-1, keepdims=True)
    my = wy.mean(axis=-1, keepdims=True)
    return wx - mx, wy - my


def ts_cov(x, y, d):
    if int(d) < 2:
        raise ValueError("ts_cov(x, y, d) needs d >= 2")
    ax, ay = _rolling_pair(x, y, d)
    return (ax * ay).sum(axis=-1) / (int(d) - 1)


# -- rolling order-statistics & dispersion (qlib Var/Med/Mad/Count/Quantile) --
def ts_var(x, d):
    if int(d) < 2:
        raise ValueError("ts_var(x, d) needs d >= 2")
    return windows(x, int(d)).var(axis=-1, ddof=1)


def ts_med(x, d):
    return np.median(windows(x, int(d)), axis=-1)  # NaN in a window -> NaN (full-window)


def ts_mad(x, d):
    w = windows(x, int(d))
    return np.abs(w - w.mean(axis=-1, keepdims=True)).mean(axis=-1)


def ts_count(x, d):
    """Rolling count of nonzero (truthy) values over the window (qlib ``Count``)."""
    w = windows(x, int(d))
    valid = ~np.isnan(w).any(axis=-1)
    cnt = (w != 0).sum(axis=-1).astype(np.float64)
    cnt[~valid] = np.nan
    return cnt


def ts_quantile(x, d, q):
    """Rolling ``q``-quantile (qlib ``Quantile(x, d, qscore)``); ``q`` in [0, 1]."""
    return np.quantile(windows(x, int(d)), float(q), axis=-1)


# -- rolling least-squares vs time (qlib Slope/Rsquare/Resi) ------------------
def _rolling_ls(x, d):
    """Per-window OLS of x against time t=0..d-1. Returns the shared pieces."""
    d = int(d)
    if d < 2:
        raise ValueError("slope/rsquare/resi need d >= 2")
    w = windows(x, d)                       # (T, N, d), NaN-filled until full
    t = np.arange(d, dtype=np.float64)
    tc = t - t.mean()
    stt = float((tc * tc).sum())            # scalar: sum (t - tbar)^2
    xbar = w.mean(axis=-1)
    sxt = (w * tc).sum(axis=-1)             # == sum((w-xbar) * tc) since sum(tc)=0
    slope = sxt / stt
    intercept = xbar - slope * t.mean()
    return w, t, stt, xbar, sxt, slope, intercept


def ts_slope(x, d):
    return _rolling_ls(x, d)[5]


def ts_resi(x, d):
    """Regression residual at the most recent bar: x_t - (intercept + slope*(d-1))."""
    w, t, _, _, _, slope, intercept = _rolling_ls(x, d)
    return w[..., -1] - (intercept + slope * t[-1])


def ts_rsquare(x, d):
    w, t, stt, xbar, sxt, slope, intercept = _rolling_ls(x, d)
    sxx = ((w - xbar[..., None]) ** 2).sum(axis=-1)
    with np.errstate(invalid="ignore", divide="ignore"):
        r2 = (sxt ** 2) / (stt * sxx)
    return np.where(sxx == 0, np.nan, r2)


def ts_corr(x, y, d):
    ax, ay = _rolling_pair(x, y, d)
    cov = (ax * ay).sum(axis=-1)
    denom = np.sqrt((ax * ax).sum(axis=-1)) * np.sqrt((ay * ay).sum(axis=-1))
    with np.errstate(invalid="ignore", divide="ignore"):
        r = cov / denom
    return np.where(denom == 0, np.nan, r)  # zero variance (e.g. d too small) -> NaN


# `incremental` mirrors the appendix table (whether an O(1) daily update exists);
# the current kernels recompute, but the flag is part of the agent-facing schema.
register("ts_delay", ts_delay, 2, 2, signature="ts_delay(x, d)", category="time-series",
         output_range="same as x", incremental=True)
register("ts_delta", ts_delta, 2, 2, signature="ts_delta(x, d)", category="time-series",
         output_range="same as x", incremental=True)
register("ts_returns", ts_returns, 2, 2, signature="ts_returns(x, d)", category="time-series",
         output_range="(-1, inf)", incremental=True)
register("ts_log_returns", ts_log_returns, 2, 2, signature="ts_log_returns(x, d)",
         category="time-series", output_range="(-inf, inf)", incremental=True)
register("ts_mean", ts_mean, 2, 2, signature="ts_mean(x, d)", category="time-series",
         output_range="same as x", incremental=True)
register("ts_sum", ts_sum, 2, 2, signature="ts_sum(x, d)", category="time-series",
         output_range="same as x", incremental=True)
register("ts_product", ts_product, 2, 2, signature="ts_product(x, d)", category="time-series",
         output_range="same as x", incremental=False)
register("ts_std", ts_std, 2, 2, signature="ts_std(x, d)", category="time-series",
         output_range="[0, inf)", incremental=True,
         common_errors=["d=1 is undefined (needs d >= 2)"])
register("ts_min", ts_min, 2, 2, signature="ts_min(x, d)", category="time-series",
         output_range="same as x", incremental=False)
register("ts_max", ts_max, 2, 2, signature="ts_max(x, d)", category="time-series",
         output_range="same as x", incremental=False)
register("ts_argmin", ts_argmin, 2, 2, signature="ts_argmin(x, d)", category="time-series",
         output_range="[0, d-1] (days since min)", incremental=False)
register("ts_argmax", ts_argmax, 2, 2, signature="ts_argmax(x, d)", category="time-series",
         output_range="[0, d-1] (days since max)", incremental=False)
register("ts_rank", ts_rank, 2, 2, signature="ts_rank(x, d)", category="time-series",
         output_range="[0, 1]", incremental=False,
         common_errors=["d=1 is undefined (needs d >= 2)"])
register("ts_decay_linear", ts_decay_linear, 2, 2, signature="ts_decay_linear(x, d)",
         category="time-series", output_range="same as x", incremental=True)
register("ts_ema", ts_ema, 2, 2, signature="ts_ema(x, d)", category="time-series",
         output_range="same as x", incremental=True)
register("ts_dema", ts_dema, 2, 2, signature="ts_dema(x, d)", category="time-series",
         output_range="same as x", incremental=True)
register("ts_skew", ts_skew, 2, 2, signature="ts_skew(x, d)", category="time-series",
         output_range="(-inf, inf)", incremental=False)
register("ts_kurt", ts_kurt, 2, 2, signature="ts_kurt(x, d)", category="time-series",
         output_range="(-inf, inf) (excess)", incremental=False)
register("ts_corr", ts_corr, 3, 3, signature="ts_corr(x, y, d)", category="time-series",
         output_range="[-1, 1]", incremental=True,
         common_errors=["d=1 produces all-NaN output (zero variance)"])
register("ts_cov", ts_cov, 3, 3, signature="ts_cov(x, y, d)", category="time-series",
         output_range="(-inf, inf)", incremental=True)
register("ts_var", ts_var, 2, 2, signature="ts_var(x, d)", category="time-series",
         output_range="[0, inf)", incremental=False,
         common_errors=["d=1 is undefined (needs d >= 2)"])
register("ts_med", ts_med, 2, 2, signature="ts_med(x, d)", category="time-series",
         output_range="same as x", incremental=False)
register("ts_mad", ts_mad, 2, 2, signature="ts_mad(x, d)", category="time-series",
         output_range="[0, inf)", incremental=False)
register("ts_count", ts_count, 2, 2, signature="ts_count(cond, d)", category="time-series",
         output_range="[0, d]", incremental=False)
register("ts_quantile", ts_quantile, 3, 3, signature="ts_quantile(x, d, q)",
         category="time-series", output_range="same as x", incremental=False)
register("ts_slope", ts_slope, 2, 2, signature="ts_slope(x, d)", category="time-series",
         output_range="(-inf, inf)", incremental=False)
register("ts_resi", ts_resi, 2, 2, signature="ts_resi(x, d)", category="time-series",
         output_range="(-inf, inf)", incremental=False)
register("ts_rsquare", ts_rsquare, 2, 2, signature="ts_rsquare(x, d)", category="time-series",
         output_range="[0, 1]", incremental=False)


__all__ = [
    "ts_delay", "ts_delta", "ts_returns", "ts_log_returns", "ts_mean", "ts_sum", "ts_product",
    "ts_std", "ts_min", "ts_max", "ts_argmin", "ts_argmax", "ts_rank", "ts_decay_linear",
    "ts_ema", "ts_dema", "ts_skew", "ts_kurt", "ts_cov", "ts_corr",
    "ts_var", "ts_med", "ts_mad", "ts_count", "ts_quantile",
    "ts_slope", "ts_resi", "ts_rsquare",
]
