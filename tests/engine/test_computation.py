"""End-to-end computation checks through FactorEngine.

Validates the full parse -> pivot -> evaluate pipeline against *independent*
numpy reference implementations (plain loops, not the kernels' sliding-window
code), plus panel-pivot alignment (shuffled rows, missing cells), multi-stage
ts+cs composition, the long-frame round-trip, determinism, and per-symbol
independence. Run with::

    PYTHONPATH=src python -m pytest tests/engine/test_computation.py -q
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import polars as pl
import pytest

from assay.engine import FactorEngine, parse

T, N = 30, 5
SYMS = [f"S{j}" for j in range(N)]
DATES = [dt.date(2024, 1, 1) + dt.timedelta(days=i) for i in range(T)]


def _matrices(seed=0):
    rng = np.random.default_rng(seed)
    close = 100 + rng.normal(size=(T, N)).cumsum(axis=0)
    volume = 1e6 + rng.normal(size=(T, N)) * 1e4
    return close, volume


def _panel(close, volume, shuffle=False, drop=None):
    rows_d, rows_s, rows_c, rows_v = [], [], [], []
    for ti, d in enumerate(DATES):
        for sj, s in enumerate(SYMS):
            if drop and (ti, sj) in drop:
                continue
            rows_d.append(d); rows_s.append(s)
            rows_c.append(close[ti, sj]); rows_v.append(volume[ti, sj])
    df = pl.DataFrame({"date": rows_d, "symbol": rows_s, "close": rows_c, "volume": rows_v})
    if shuffle:
        df = df.sample(fraction=1.0, shuffle=True, seed=42)
    return df


# ---- independent reference implementations (plain loops) -------------------


def ref_rolling(x, d, fn):
    out = np.full_like(x, np.nan)
    for t in range(d - 1, x.shape[0]):
        out[t] = fn(x[t - d + 1 : t + 1], axis=0)
    return out


def ref_delay(x, d):
    out = np.full_like(x, np.nan)
    out[d:] = x[:-d]
    return out


# ---------------------------------------------------------------------------
# single-stage factors vs reference
# ---------------------------------------------------------------------------


def test_momentum_returns():
    close, volume = _matrices()
    eng = FactorEngine(_panel(close, volume))
    got = eng.evaluate("ts_returns(close, 10)").values
    expected = close / ref_delay(close, 10) - 1
    np.testing.assert_allclose(got, expected, equal_nan=True, rtol=1e-10)


def test_rolling_mean_and_std():
    close, volume = _matrices(1)
    eng = FactorEngine(_panel(close, volume))
    np.testing.assert_allclose(
        eng.evaluate("ts_mean(close, 5)").values,
        ref_rolling(close, 5, np.mean), equal_nan=True, rtol=1e-10,
    )
    np.testing.assert_allclose(
        eng.evaluate("ts_std(close, 5)").values,
        ref_rolling(close, 5, lambda w, axis: np.std(w, axis=axis, ddof=1)),
        equal_nan=True, rtol=1e-10,
    )


def test_rolling_correlation():
    close, volume = _matrices(2)
    eng = FactorEngine(_panel(close, volume))
    got = eng.evaluate("ts_corr(close, volume, 10)").values
    expected = np.full((T, N), np.nan)
    for t in range(9, T):
        for j in range(N):
            expected[t, j] = np.corrcoef(close[t - 9 : t + 1, j], volume[t - 9 : t + 1, j])[0, 1]
    np.testing.assert_allclose(got, expected, equal_nan=True, rtol=1e-9)


def test_cross_sectional_demean_and_zscore():
    close, volume = _matrices(3)
    eng = FactorEngine(_panel(close, volume))
    np.testing.assert_allclose(
        eng.evaluate("cs_demean(close)").values,
        close - close.mean(axis=1, keepdims=True), rtol=1e-10,
    )
    z = (close - close.mean(axis=1, keepdims=True)) / close.std(axis=1, ddof=1, keepdims=True)
    np.testing.assert_allclose(eng.evaluate("cs_zscore(close)").values, z, rtol=1e-10)


# ---------------------------------------------------------------------------
# multi-stage ts + cs composition
# ---------------------------------------------------------------------------


def test_composite_normalized_momentum():
    close, volume = _matrices(4)
    eng = FactorEngine(_panel(close, volume))
    got = eng.evaluate("(close - ts_mean(close, 5)) / ts_std(close, 5)").values
    mean5 = ref_rolling(close, 5, np.mean)
    std5 = ref_rolling(close, 5, lambda w, axis: np.std(w, axis=axis, ddof=1))
    np.testing.assert_allclose(got, (close - mean5) / std5, equal_nan=True, rtol=1e-9)


def test_cs_rank_properties_on_composite():
    close, volume = _matrices(5)
    eng = FactorEngine(_panel(close, volume))
    out = eng.evaluate("cs_rank(ts_mean(close, 5))").values
    base = ref_rolling(close, 5, np.mean)
    for t in range(T):
        finite = np.isfinite(out[t])
        if finite.sum() < 2:
            continue
        # rank is in [0,1] and order-preserving w.r.t. the underlying values
        assert out[t][finite].min() >= 0 and out[t][finite].max() <= 1
        order_in = np.argsort(base[t][finite])
        order_out = np.argsort(out[t][finite])
        np.testing.assert_array_equal(order_in, order_out)


# ---------------------------------------------------------------------------
# pivot / alignment correctness
# ---------------------------------------------------------------------------


def test_shuffled_panel_rows_align_identically():
    close, volume = _matrices(6)
    ordered = FactorEngine(_panel(close, volume)).evaluate("ts_returns(close, 3)").values
    shuffled = FactorEngine(_panel(close, volume, shuffle=True)).evaluate("ts_returns(close, 3)").values
    np.testing.assert_allclose(ordered, shuffled, equal_nan=True, rtol=1e-12)


def test_missing_cells_become_nan():
    close, volume = _matrices(7)
    drop = {(10, 2), (11, 2), (12, 2)}  # holes for symbol S2 mid-series
    eng = FactorEngine(_panel(close, volume, drop=drop))
    # the engine pivots to a full (T, N) grid; dropped (date, symbol) cells are NaN
    raw = eng.evaluate("close").values
    for ti, sj in drop:
        assert np.isnan(raw[ti, sj])
    # an untouched symbol/date is unaffected
    assert raw[10, 0] == pytest.approx(close[10, 0])


# ---------------------------------------------------------------------------
# round-trip, determinism, independence
# ---------------------------------------------------------------------------


def test_to_frame_roundtrip():
    close, volume = _matrices(8)
    eng = FactorEngine(_panel(close, volume))
    res = eng.evaluate("ts_returns(close, 5)")
    frame = res.to_frame()
    assert frame.shape == (T * N, 3)
    # pivot the long frame back to a (T, N) matrix and compare
    wide = frame.pivot(index="date", on="symbol", values="factor").sort("date")
    back = wide.select(SYMS).to_numpy()
    np.testing.assert_allclose(back, res.values, equal_nan=True, rtol=1e-12)


def test_determinism():
    close, volume = _matrices(9)
    eng = FactorEngine(_panel(close, volume))
    a = eng.evaluate("cs_rank(ts_corr(close, volume, 8))").values
    b = eng.evaluate("cs_rank(ts_corr(close, volume, 8))").values
    np.testing.assert_array_equal(np.nan_to_num(a), np.nan_to_num(b))


def test_time_series_factor_is_per_symbol_independent():
    close, volume = _matrices(10)
    base = FactorEngine(_panel(close, volume)).evaluate("ts_mean(close, 5)").values
    perturbed_close = close.copy()
    perturbed_close[:, 1] += 1000.0  # change only symbol S1
    pert = FactorEngine(_panel(perturbed_close, volume)).evaluate("ts_mean(close, 5)").values
    # S0's time-series factor is unaffected by S1's data
    np.testing.assert_allclose(base[:, 0], pert[:, 0], equal_nan=True, rtol=1e-12)


def test_constant_factor_broadcasts_to_grid():
    close, volume = _matrices(11)
    out = FactorEngine(_panel(close, volume)).evaluate("close * 0 + 7").values
    assert out.shape == (T, N)
    np.testing.assert_allclose(out, 7.0)


# ---------------------------------------------------------------------------
# review-driven: engine error/validation paths
# ---------------------------------------------------------------------------


def test_empty_panel_rejected():
    with pytest.raises(ValueError, match="empty panel"):
        FactorEngine(pl.DataFrame({"date": [], "symbol": [], "close": []}))


def test_missing_required_column_rejected():
    df = pl.DataFrame({"date": list(DATES[:2]) * 1, "close": [1.0, 2.0]})  # no 'symbol'
    with pytest.raises(ValueError, match="symbol"):
        FactorEngine(df)


def test_evaluate_unknown_field_rejected():
    close, volume = _matrices(12)
    eng = FactorEngine(_panel(close, volume))  # only close, volume present
    with pytest.raises(ValueError, match="not in the panel"):
        eng.evaluate("ts_mean(vwap, 5)")


def test_evaluate_accepts_prebuilt_ast():
    close, volume = _matrices(13)
    eng = FactorEngine(_panel(close, volume))
    node = parse("ts_returns(close, 5)")
    np.testing.assert_allclose(
        eng.evaluate(node).values, eng.evaluate("ts_returns(close, 5)").values, equal_nan=True
    )
