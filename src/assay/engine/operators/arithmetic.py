"""Arithmetic, comparison and logical kernels produced by infix operators.

These back the ``+ - * /`` arithmetic, the ``< <= > >= == !=`` comparisons (which
return 1.0/0.0, with NaN where either operand is NaN) and the ``||`` logical-or.
"""

from __future__ import annotations

import numpy as np

from ._base import as_float
from .registry import register


def op_add(a, b):
    return as_float(a) + as_float(b)


def op_sub(a, b):
    return as_float(a) - as_float(b)


def op_mul(a, b):
    return as_float(a) * as_float(b)


def op_div(a, b):
    with np.errstate(divide="ignore", invalid="ignore"):
        return as_float(a) / as_float(b)


def op_neg(a):
    return -as_float(a)


def _compare(a, b, fn):
    a, b = as_float(a), as_float(b)
    out = fn(a, b).astype(np.float64)
    return np.where(np.isnan(a) | np.isnan(b), np.nan, out)


def op_lt(a, b):
    return _compare(a, b, np.less)


def op_le(a, b):
    return _compare(a, b, np.less_equal)


def op_gt(a, b):
    return _compare(a, b, np.greater)


def op_ge(a, b):
    return _compare(a, b, np.greater_equal)


def op_eq(a, b):
    return _compare(a, b, np.equal)


def op_ne(a, b):
    return _compare(a, b, np.not_equal)


def op_or(a, b):
    """Logical OR (the paper's ``||``): 1.0 if either operand is truthy.

    A NaN operand is treated as false; NaN only when *both* are NaN.
    """
    a, b = as_float(a), as_float(b)
    av = np.where(np.isnan(a), 0.0, a)
    bv = np.where(np.isnan(b), 0.0, b)
    out = ((av != 0) | (bv != 0)).astype(np.float64)
    return np.where(np.isnan(a) & np.isnan(b), np.nan, out)


register("add", op_add, 2, 2, signature="a + b", category="arithmetic", incremental=True)
register("sub", op_sub, 2, 2, signature="a - b", category="arithmetic", incremental=True)
register("mul", op_mul, 2, 2, signature="a * b", category="arithmetic", incremental=True)
register("div", op_div, 2, 2, signature="a / b", category="arithmetic", incremental=True)
register("neg", op_neg, 1, 1, signature="-a", category="arithmetic", incremental=True)
register("lt", op_lt, 2, 2, signature="a < b", category="comparison", incremental=True)
register("le", op_le, 2, 2, signature="a <= b", category="comparison", incremental=True)
register("gt", op_gt, 2, 2, signature="a > b", category="comparison", incremental=True)
register("ge", op_ge, 2, 2, signature="a >= b", category="comparison", incremental=True)
register("eq", op_eq, 2, 2, signature="a == b", category="comparison", incremental=True)
register("ne", op_ne, 2, 2, signature="a != b", category="comparison", incremental=True)
register("or", op_or, 2, 2, signature="a || b", category="logical", incremental=True)


__all__ = [
    "op_add", "op_sub", "op_mul", "op_div", "op_neg",
    "op_lt", "op_le", "op_gt", "op_ge", "op_eq", "op_ne", "op_or",
]
