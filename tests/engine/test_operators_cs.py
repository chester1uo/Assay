"""Cross-sectional and group operator tests.

``cs_*`` operators reduce along axis 1 (symbols) within each date and are
NaN-aware. Group operators (``cs_neutralize`` / ``cs_group_rank`` /
``cs_group_mean``) demean/rank within industry groups supplied via the engine
context. Run with::

    PYTHONPATH=src python -m pytest tests/engine/test_operators_cs.py -q
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import polars as pl
import pytest

from assay.engine import FactorEngine, operators as op


class _Ctx:
    """Minimal stand-in for EvalContext for direct group-kernel tests."""

    def __init__(self, **groups):
        self._g = {k: np.asarray(v, dtype=object) for k, v in groups.items()}

    def require_groups(self, key):
        return self._g[key]


def row(*vals) -> np.ndarray:
    """A single-date (1, N) cross-section."""
    return np.asarray([vals], dtype=float)


# ---------------------------------------------------------------------------
# cs_rank
# ---------------------------------------------------------------------------


def test_cs_rank_basic_and_bounds():
    np.testing.assert_allclose(op.cs_rank(row(10, 20, 30))[0], [0.0, 0.5, 1.0])
    np.testing.assert_allclose(op.cs_rank(row(30, 20, 10))[0], [1.0, 0.5, 0.0])


def test_cs_rank_average_ties():
    # [10, 20, 20, 30] -> ranks 0, 1.5, 1.5, 3 over (n-1)=3
    np.testing.assert_allclose(op.cs_rank(row(10, 20, 20, 30))[0], [0.0, 0.5, 0.5, 1.0])


def test_cs_rank_nan_excluded():
    out = op.cs_rank(row(10, np.nan, 30, 20))[0]
    assert np.isnan(out[1])
    np.testing.assert_allclose([out[0], out[2], out[3]], [0.0, 1.0, 0.5])


def test_cs_rank_singleton_is_half():
    assert op.cs_rank(row(42))[0, 0] == pytest.approx(0.5)


def test_cs_rank_is_per_date():
    x = np.array([[10.0, 20.0, 30.0], [30.0, 20.0, 10.0]])
    out = op.cs_rank(x)
    np.testing.assert_allclose(out[0], [0.0, 0.5, 1.0])
    np.testing.assert_allclose(out[1], [1.0, 0.5, 0.0])


# ---------------------------------------------------------------------------
# cs_demean / cs_zscore
# ---------------------------------------------------------------------------


def test_cs_demean_sums_to_zero():
    out = op.cs_demean(row(1, 2, 3, 4))[0]
    np.testing.assert_allclose(out, [-1.5, -0.5, 0.5, 1.5])
    assert out.sum() == pytest.approx(0.0)


def test_cs_zscore_mean0_std1():
    out = op.cs_zscore(row(1, 2, 3))[0]
    np.testing.assert_allclose(out, [-1.0, 0.0, 1.0])  # ddof=1
    assert out.mean() == pytest.approx(0.0)
    assert out.std(ddof=1) == pytest.approx(1.0)


def test_cs_zscore_constant_row_is_nan():
    assert np.isnan(op.cs_zscore(row(5, 5, 5))[0]).all()


def test_cs_demean_nan_aware():
    out = op.cs_demean(row(2, 4, np.nan))[0]  # mean of {2,4} = 3
    np.testing.assert_allclose([out[0], out[1]], [-1.0, 1.0])
    assert np.isnan(out[2])


# ---------------------------------------------------------------------------
# cs_scale
# ---------------------------------------------------------------------------


def test_cs_scale_sum_abs_equals_a():
    out = op.cs_scale(row(1, 3))[0]  # default a=1
    np.testing.assert_allclose(out, [0.25, 0.75])
    assert np.abs(out).sum() == pytest.approx(1.0)


def test_cs_scale_custom_a_and_signs():
    out = op.cs_scale(row(-1, 3), a=2.0)[0]
    assert np.abs(out).sum() == pytest.approx(2.0)
    assert out[0] < 0 and out[1] > 0  # signs preserved


def test_cs_scale_all_zero_is_nan():
    assert np.isnan(op.cs_scale(row(0, 0, 0))[0]).all()


# ---------------------------------------------------------------------------
# cs_winsorize
# ---------------------------------------------------------------------------


def test_cs_winsorize_clips_tails():
    r = row(*range(1, 11))
    out = op.cs_winsorize(r, 0.1)[0]
    lo = np.quantile(np.arange(1, 11), 0.1)
    hi = np.quantile(np.arange(1, 11), 0.9)
    np.testing.assert_allclose(out, np.clip(np.arange(1, 11), lo, hi))
    assert out.min() >= lo - 1e-9 and out.max() <= hi + 1e-9


@pytest.mark.parametrize("p", [0.0, 0.5, 0.7, -0.1])
def test_cs_winsorize_bad_p(p):
    with pytest.raises(ValueError):
        op.cs_winsorize(row(1, 2, 3), p)


# ---------------------------------------------------------------------------
# group operators (direct kernels with a stub context)
# ---------------------------------------------------------------------------

GROUPS = ["A", "A", "B", "B"]


def test_cs_neutralize_demeans_within_group():
    x = row(1, 3, 10, 30)  # A mean 2, B mean 20
    out = op.cs_neutralize(x, "sector", ctx=_Ctx(sector=GROUPS))[0]
    np.testing.assert_allclose(out, [-1.0, 1.0, -10.0, 10.0])


def test_cs_group_mean_broadcasts():
    x = row(1, 3, 10, 30)
    out = op.cs_group_mean(x, "sector", ctx=_Ctx(sector=GROUPS))[0]
    np.testing.assert_allclose(out, [2.0, 2.0, 20.0, 20.0])


def test_cs_group_rank_within_group():
    x = row(1, 3, 30, 10)  # A: [1,3]->[0,1]; B: [30,10]->[1,0]
    out = op.cs_group_rank(x, "sector", ctx=_Ctx(sector=GROUPS))[0]
    np.testing.assert_allclose(out, [0.0, 1.0, 1.0, 0.0])


def test_group_vector_length_mismatch():
    x = row(1, 2, 3, 4)
    with pytest.raises(ValueError, match="symbols"):
        op.cs_neutralize(x, "sector", ctx=_Ctx(sector=["A", "B", "C"]))


# ---------------------------------------------------------------------------
# end-to-end through the engine (group_data wiring + error path)
# ---------------------------------------------------------------------------


def _panel(close_mat, syms):
    t, n = close_mat.shape
    dates = [dt.date(2024, 1, 1) + dt.timedelta(days=i) for i in range(t)]
    return pl.DataFrame(
        {
            "date": np.repeat(np.array(dates), n),
            "symbol": syms * t,
            "close": close_mat.reshape(-1).astype(float),
        }
    )


def test_cs_neutralize_through_engine():
    syms = ["S0", "S1", "S2", "S3"]
    close = np.array([[1.0, 3.0, 10.0, 30.0], [2.0, 6.0, 20.0, 60.0]])
    eng = FactorEngine(
        _panel(close, syms),
        group_data={"sector": {"S0": "A", "S1": "A", "S2": "B", "S3": "B"}},
    )
    out = eng.evaluate("cs_neutralize(close, 'sector')").values
    np.testing.assert_allclose(out[0], [-1.0, 1.0, -10.0, 10.0])
    np.testing.assert_allclose(out[1], [-2.0, 2.0, -20.0, 20.0])


def test_engine_indneutralize_without_groups_errors():
    syms = ["S0", "S1"]
    eng = FactorEngine(_panel(np.array([[1.0, 2.0]]), syms))  # no group_data
    with pytest.raises(ValueError, match="group data"):
        eng.evaluate("cs_neutralize(close, 'sector')")


# ---------------------------------------------------------------------------
# review-driven: incomplete group map, group ops via engine, all-NaN, NaN-in-group
# ---------------------------------------------------------------------------


def test_engine_incomplete_group_map_raises_clearly():
    # a symbol absent from the sector map must raise a clear error, not crash
    # later inside the kernel with an opaque TypeError.
    syms = ["S0", "S1", "S2", "S3"]
    with pytest.raises(ValueError, match="missing labels"):
        FactorEngine(
            _panel(np.array([[1.0, 2.0, 3.0, 4.0]]), syms),
            group_data={"sector": {"S0": "A", "S1": "A", "S2": "B"}},  # S3 omitted
        )


def test_cs_group_rank_and_mean_through_engine():
    syms = ["S0", "S1", "S2", "S3"]
    close = np.array([[1.0, 3.0, 30.0, 10.0]])
    eng = FactorEngine(
        _panel(close, syms),
        group_data={"sector": {"S0": "A", "S1": "A", "S2": "B", "S3": "B"}},
    )
    np.testing.assert_allclose(
        eng.evaluate("cs_group_rank(close, 'sector')").values[0], [0.0, 1.0, 1.0, 0.0]
    )
    np.testing.assert_allclose(
        eng.evaluate("cs_group_mean(close, 'sector')").values[0], [2.0, 2.0, 20.0, 20.0]
    )


@pytest.mark.parametrize("fn", [op.cs_rank, op.cs_demean, op.cs_zscore, op.cs_scale])
def test_cs_all_nan_cross_section(fn):
    out = fn(np.array([[np.nan, np.nan, np.nan]]))
    assert np.isnan(out).all()


def test_cs_neutralize_is_nan_aware_within_group():
    # group A = {0,1}; symbol 1 missing -> A mean is just value 0; group B demeaned normally
    x = row(4.0, np.nan, 10.0, 30.0)
    out = op.cs_neutralize(x, "sector", ctx=_Ctx(sector=GROUPS))[0]
    assert out[0] == pytest.approx(0.0)   # only finite member of group A -> demeans to 0
    assert np.isnan(out[1])
    np.testing.assert_allclose([out[2], out[3]], [-10.0, 10.0])  # B mean 20


@pytest.mark.parametrize("fn", [op.cs_demean, op.cs_zscore, op.cs_scale])
def test_cs_ops_are_per_date(fn):
    x = np.array([[1.0, 2.0, 3.0], [10.0, 20.0, 30.0]])
    out = fn(x)
    np.testing.assert_allclose(out[0], fn(x[:1])[0], equal_nan=True)
    np.testing.assert_allclose(out[1], fn(x[1:])[0], equal_nan=True)
