"""Blocked window kernels give exactly the values of one big block, and the
use-count release in ``evaluate_iter`` changes memory, never values."""

from __future__ import annotations

import numpy as np
import pytest

from assay.engine.engine import FactorEngine
from assay.engine.operators import _base
from assay.engine.operators import time_series as ts

ONE = ["ts_mean", "ts_sum", "ts_product", "ts_std", "ts_min", "ts_max", "ts_argmax", "ts_argmin",
       "ts_rank", "ts_decay_linear", "ts_skew", "ts_kurt", "ts_var", "ts_med", "ts_mad", "ts_count",
       "ts_slope", "ts_resi", "ts_rsquare"]


@pytest.fixture
def xy():
    rng = np.random.default_rng(7)
    x, y = rng.normal(size=(300, 17)), rng.normal(size=(300, 17))
    x[rng.random(x.shape) < 0.03] = np.nan
    x[:, 3] = 0.0                                  # constant column: zero-variance branches
    return x, y


@pytest.mark.parametrize("d", [2, 5, 40])
def test_blocks_match_one_block(monkeypatch, xy, d):
    x, y = xy
    whole = {op: getattr(ts, op)(x, d) for op in ONE}
    whole["ts_corr"], whole["ts_cov"] = ts.ts_corr(x, y, d), ts.ts_cov(x, y, d)
    whole["ts_quantile"] = ts.ts_quantile(x, d, 0.3)
    monkeypatch.setattr(_base, "WINDOW_BLOCK_MB", 0.001)      # a few rows per block
    for op in ONE:
        np.testing.assert_array_equal(getattr(ts, op)(x, d), whole[op], err_msg=op)
    np.testing.assert_array_equal(ts.ts_corr(x, y, d), whole["ts_corr"])
    np.testing.assert_array_equal(ts.ts_cov(x, y, d), whole["ts_cov"])
    np.testing.assert_array_equal(ts.ts_quantile(x, d, 0.3), whole["ts_quantile"])


def test_release_keeps_values(xy):
    x, y = xy
    dates = np.arange("2020-01-01", 300, dtype="datetime64[D]")[:300]
    eng = FactorEngine.from_matrices(dates, np.array([f"S{i:02d}" for i in range(17)]), {"close": x, "volume": y})
    exprs = ["ts_mean(close, 5)", "cs_rank(ts_mean(close, 5)) - ts_std(volume, 10)",
             "ts_corr(ts_mean(close, 5), volume, 20)", "ts_mean(close, 5)", "mul(ts_std(volume, 10), ts_std(volume, 10))"]
    kept = list(eng.evaluate_iter(exprs, release=False))
    freed = list(eng.evaluate_iter(exprs))
    for a, b in zip(kept, freed):
        np.testing.assert_array_equal(a.values, b.values, err_msg=a.expr)
