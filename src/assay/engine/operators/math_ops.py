"""Element-wise math operator kernels.

Standard math functions plus Assay's safety variants. All operate element-wise
and broadcast scalars; ``signed_power`` keeps a negative base real (sign
preserved) while ``pow`` follows numpy (negative base, fractional exponent ->
NaN). NaN propagates through every kernel.
"""

from __future__ import annotations

import numpy as np

from ._base import as_float, quiet_numeric
from .registry import register


def op_abs(x):
    return np.abs(as_float(x))


def op_log(x):
    x = as_float(x)
    return np.where(x > 0, np.log(np.where(x > 0, x, 1.0)), np.nan)


def op_sign(x):
    return np.sign(as_float(x))


def op_sqrt(x):
    x = as_float(x)
    return np.where(x >= 0, np.sqrt(np.where(x >= 0, x, 0.0)), np.nan)


def signed_power(x, e):
    # exponent may be a scalar OR an aligned (T, N) matrix (e.g. Alpha#84).
    x = as_float(x)
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.sign(x) * np.abs(x) ** as_float(e)


def op_pow(x, e):
    # base and exponent may both be matrices (e.g. rank(...)^rank(...)).
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.power(as_float(x), as_float(e))


def op_clip(x, lo, hi):
    return np.clip(as_float(x), float(lo), float(hi))


def elem_min(x, y):
    return np.minimum(as_float(x), as_float(y))


def elem_max(x, y):
    return np.maximum(as_float(x), as_float(y))


def op_where(cond, a, b):
    cond = as_float(cond)
    out = np.where(cond != 0, as_float(a), as_float(b))
    return np.where(np.isnan(cond), np.nan, out)


def safe_div(a, b, fill=0.0):
    a, b = as_float(a), as_float(b)
    with np.errstate(divide="ignore", invalid="ignore"):
        out = a / b
    return np.where(b == 0, float(fill), out)


def op_sigmoid(x):
    with np.errstate(over="ignore"):  # large negative input saturates to 0, not an error
        return 1.0 / (1.0 + np.exp(-as_float(x)))


def op_fillna(x, method="zero"):
    x = as_float(x)
    if method == "zero":
        return np.nan_to_num(x, nan=0.0)
    if method == "median":  # per-date cross-sectional median
        with quiet_numeric():
            med = np.nanmedian(x, axis=1, keepdims=True)
        return np.where(np.isnan(x), med, x)
    if method == "ffill":  # forward-fill along time within each symbol
        out = x.copy()
        for t in range(1, out.shape[0]):
            out[t] = np.where(np.isnan(out[t]), out[t - 1], out[t])
        return out
    raise ValueError(f"fillna method must be 'zero' | 'median' | 'ffill', got {method!r}")


register("abs", op_abs, 1, 1, signature="abs(x)", category="math", output_range="[0, inf)",
         incremental=True)
register("log", op_log, 1, 1, signature="log(x)", category="math", output_range="(-inf, inf)",
         incremental=True, common_errors=["x <= 0 -> NaN"])
register("sign", op_sign, 1, 1, signature="sign(x)", category="math",
         output_range="{-1, 0, 1}", incremental=True)
register("sqrt", op_sqrt, 1, 1, signature="sqrt(x)", category="math", output_range="[0, inf)",
         incremental=True, common_errors=["x < 0 -> NaN"])
register("signed_power", signed_power, 2, 2, signature="signed_power(x, e)", category="math",
         output_range="(-inf, inf)", incremental=True,
         common_errors=["distinct from pow: keeps sign -> sign(x)*abs(x)**e"])
register("pow", op_pow, 2, 2, signature="pow(x, e)", category="math",
         output_range="(-inf, inf)", incremental=True)
register("clip", op_clip, 3, 3, signature="clip(x, lo, hi)", category="math",
         output_range="[lo, hi]", incremental=True)
register("elem_min", elem_min, 2, 2, signature="elem_min(x, y)", category="math",
         output_range="min(x, y)", incremental=True)
register("elem_max", elem_max, 2, 2, signature="elem_max(x, y)", category="math",
         output_range="max(x, y)", incremental=True)
register("where", op_where, 3, 3, signature="where(cond, a, b)", category="math",
         output_range="a or b", incremental=True)
register("safe_div", safe_div, 2, 3, signature="safe_div(a, b, fill=0)", category="math",
         output_range="(-inf, inf)", incremental=True)
register("sigmoid", op_sigmoid, 1, 1, signature="sigmoid(x)", category="math",
         output_range="(0, 1)", incremental=True)
register("fillna", op_fillna, 1, 2, signature="fillna(x, method='zero')", category="math",
         output_range="same as x", incremental=True,
         common_errors=["method in {'zero','median','ffill'}"])


__all__ = [
    "op_abs", "op_log", "op_sign", "op_sqrt", "signed_power", "op_pow", "op_clip",
    "elem_min", "elem_max", "op_where", "safe_div", "op_sigmoid", "op_fillna",
]
