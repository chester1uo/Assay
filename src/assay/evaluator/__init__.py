"""Evaluator: pure-numpy metrics over ``(T, N)`` matrices (engineering-docs §6).

This package is the backtest/evaluation metrics layer. Every function here is a
**pure function** over aligned ``(T, N)`` float matrices (``T`` dates on axis 0,
``N`` symbols on axis 1) — it imports neither the factor engine nor the data
layer, so it can be reused, tested, and benchmarked in isolation. The caller is
responsible for materialising the factor and price panels (e.g. via
:class:`assay.engine.FactorResult` and :class:`assay.data.store.DataStore`) and
aligning them onto a shared ``(date, symbol)`` grid before handing them here.

Modules:

* :mod:`.forward_returns` — ``next_open`` / ``next_close`` forward returns.
* :mod:`.metrics`         — IC / RankIC series, summaries, and the ``evaluate_ic`` bundle.
* :mod:`.decay`           — exponential decay half-life.
* :mod:`.groups`          — quantile-group returns and long-short spread.
* :mod:`.turnover`        — rank-autocorrelation turnover.
* :mod:`.combination`     — multi-factor combination with train/val/test scoring.
"""

from __future__ import annotations

from .combination import (
    CombinationResult,
    SplitMetrics,
    available_methods,
    combine_factors,
    make_splits,
    standardize_xs,
)
from .decay import decay_halflife
from .forward_returns import forward_returns
from .groups import group_returns
from .metrics import (
    evaluate_ic,
    ic_series,
    ic_summary,
    rank_ic_series,
)
from .turnover import rank_autocorr, turnover

__all__ = [
    "forward_returns",
    "ic_series",
    "rank_ic_series",
    "ic_summary",
    "evaluate_ic",
    "decay_halflife",
    "group_returns",
    "turnover",
    "rank_autocorr",
    "combine_factors",
    "available_methods",
    "make_splits",
    "standardize_xs",
    "CombinationResult",
    "SplitMetrics",
]
