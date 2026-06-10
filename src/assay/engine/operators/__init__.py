"""Operator registry and numpy compute kernels, organised by category.

Every factor operator is registered under its **canonical Assay-native name**
(the parsers map qlib / Alpha-101 / WorldQuant spellings onto these names). Each
carries a numpy kernel over aligned ``(T, N)`` float matrices — ``T`` trading
days (axis 0, time) by ``N`` symbols (axis 1, cross-section) — and a
machine-readable schema for LLM-agent prompt injection.

Kernels live in one module per category:

* :mod:`~assay.engine.operators.time_series`     — ``ts_*`` (rolling, axis 0)
* :mod:`~assay.engine.operators.cross_sectional` — ``cs_*`` (per-date, axis 1)
* :mod:`~assay.engine.operators.math_ops`        — element-wise math
* :mod:`~assay.engine.operators.arithmetic`      — ``+ - * /``, comparisons, ``||``

The registry itself (``OpSpec``, :func:`register`, :func:`op`, :func:`get`, ...)
lives in :mod:`~assay.engine.operators.registry`.

Conventions
-----------
* **Time-series** windows are *full*: any NaN in the window (including ``d-1``
  warm-up days) yields NaN. ``ts_ema``/``ts_dema`` are recurrences seeded at the
  first observation (finite from day 0).
* **Cross-sectional** operators are NaN-aware (a missing symbol does not poison
  the date). All kernels return ``float64``.

Custom operators
----------------
Register your own kernel and use it in factor expressions immediately — the
parser resolves any registered name::

    import numpy as np
    from assay.engine import operators as ops
    from assay.engine import FactorEngine

    # 1. define a kernel over (T, N) matrices (reuse built-ins freely)
    @ops.op("ts_zscore", 2, 2, category="custom", output_range="(-inf, inf)")
    def ts_zscore(x, d):
        return (x - ops.ts_mean(x, d)) / ops.ts_std(x, d)

    # 2. use it in any expression, in either dialect
    FactorEngine(panel).evaluate("cs_rank(ts_zscore(close, 20))")

The kernel receives evaluated arguments: ``(T, N)`` matrices for array operands
and python scalars for literal parameters (e.g. ``d``). Set ``needs_ctx=True``
to additionally receive the evaluation context as ``ctx`` (for group operators
that resolve industry labels via ``ctx.require_groups(name)``). ``register(...)``
is the non-decorator form; ``unregister(name)`` removes one again.

Note: built-in ``ts_*`` operators floor fractional windows at parse time; custom
operators do not get that treatment, so pass integer windows (or ``int()`` them
in your kernel).
"""

from __future__ import annotations

# registry API (also the public custom-operator API)
from assay.engine.operators.registry import (
    OpSpec,
    all_specs,
    get,
    is_registered,
    op,
    operator_schema,
    register,
    unregister,
)

# importing the category modules registers their built-in kernels
from assay.engine.operators import (  # noqa: F401  (import side effect: registration)
    arithmetic,
    cross_sectional,
    math_ops,
    time_series,
)

# re-export every kernel so `operators.ts_mean`, `operators.op_pow`, ... keep working
from assay.engine.operators.arithmetic import *  # noqa: F401,F403
from assay.engine.operators.cross_sectional import *  # noqa: F401,F403
from assay.engine.operators.math_ops import *  # noqa: F401,F403
from assay.engine.operators.time_series import *  # noqa: F401,F403

# Machine-readable schema of the built-in operators (engineering-docs 4.2).
# For a live view that includes custom operators, call ``operator_schema()``.
OPERATOR_SCHEMA: dict[str, dict] = operator_schema()

__all__ = [
    "OpSpec",
    "register",
    "op",
    "unregister",
    "get",
    "is_registered",
    "all_specs",
    "operator_schema",
    "OPERATOR_SCHEMA",
    *time_series.__all__,
    *cross_sectional.__all__,
    *math_ops.__all__,
    *arithmetic.__all__,
]
