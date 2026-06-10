"""Tests for the evaluator metrics layer (engineering-docs §6).

Offline only — pure-numpy/polars synthetic data, no network or ingested data.
Every evaluator function is a pure function over aligned ``(T, N)`` float
matrices (``T`` dates on axis 0, ``N`` symbols on axis 1), so these are small
deterministic arrays with hand-checked known answers. Run with::

    PYTHONPATH=src python -m pytest tests/evaluator -q

Covers: forward_returns (next_close / next_open math, NaN tail, vwap raises),
ic_series / rank_ic_series (perfect +/- correlation, constant-date NaN,
NaN-awareness), ic_summary, evaluate_ic (keys, per-horizon, min-horizon
headline), decay_halflife, group_returns (monotonicity, long-short), and
turnover (static vs reshuffled).
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from assay.evaluator import (
    decay_halflife,
    evaluate_ic,
    forward_returns,
    group_returns,
    ic_series,
    ic_summary,
    rank_autocorr,
    rank_ic_series,
    turnover,
)


# ---------------------------------------------------------------------------
# forward_returns — execution conventions, NaN tail, vwap guard
# ---------------------------------------------------------------------------


def test_forward_returns_next_close_math_and_nan_tail():
    # Single symbol, geometric closes: 1, 2, 4, 8, 16 (doubles each day).
    close = np.array([[1.0], [2.0], [4.0], [8.0], [16.0]])
    out = forward_returns(close, None, [1, 2], execution="next_close")
    # h=1: fwd[t] = close[t+1]/close[t] - 1 == 1.0 for all defined rows.
    h1 = out[1][:, 0]
    np.testing.assert_allclose(h1[:4], [1.0, 1.0, 1.0, 1.0])
    assert np.isnan(h1[4])  # last row falls off the panel
    # h=2: close[t+2]/close[t] - 1 == 3.0; last *two* rows are the NaN tail.
    h2 = out[2][:, 0]
    np.testing.assert_allclose(h2[:3], [3.0, 3.0, 3.0])
    assert np.isnan(h2[3]) and np.isnan(h2[4])


def test_forward_returns_next_open_math_and_nan_tail():
    # next_open: fwd[t] = open[t+1+h] / open[t+1] - 1.
    # opens 10, 11, 12, 13, 14 (constant +1 per day, so ratios are exact).
    open_ = np.array([[10.0], [11.0], [12.0], [13.0], [14.0]])
    close = open_ + 0.5  # close is required by the API but unused for next_open
    out = forward_returns(close, open_, [1], execution="next_open")
    h1 = out[1][:, 0]
    # Defined for t+1+h <= T-1 -> t <= T-3 == 2, so rows 0..2 valid, last 2 NaN.
    # fwd[0] = open[2]/open[1]-1 = 12/11-1; fwd[1] = 13/12-1; fwd[2] = 14/13-1.
    np.testing.assert_allclose(
        h1[:3], [12.0 / 11.0 - 1.0, 13.0 / 12.0 - 1.0, 14.0 / 13.0 - 1.0]
    )
    assert np.isnan(h1[3]) and np.isnan(h1[4])


def test_forward_returns_next_open_requires_open():
    with pytest.raises(ValueError, match="requires the open_"):
        forward_returns(np.ones((4, 2)), None, [1], execution="next_open")


def test_forward_returns_vwap_raises():
    # MASSIVE provides OHLCV + transactions only — no VWAP / intraday data.
    with pytest.raises(ValueError, match="vwap"):
        forward_returns(np.ones((4, 2)), np.ones((4, 2)), [1], execution="vwap")


def test_forward_returns_unknown_execution_raises():
    with pytest.raises(ValueError, match="unknown execution"):
        forward_returns(np.ones((4, 2)), None, [1], execution="midpoint")


def test_forward_returns_nan_on_nonpositive_price():
    # A non-finite or non-positive price entering the ratio yields NaN, not a blow-up.
    close = np.array([[1.0], [2.0], [0.0], [8.0]])  # zero *price* at t=2
    out = forward_returns(close, None, [1], execution="next_close")[1][:, 0]
    # fwd[0]=2/1-1=1 (fine); fwd[1]=0/2-1=-1 (zero numerator is allowed, finite).
    np.testing.assert_allclose(out[:2], [1.0, -1.0])
    assert np.isnan(out[2])  # close[3]/close[2] divides by a zero price -> NaN
    assert np.isnan(out[3])  # NaN tail


# ---------------------------------------------------------------------------
# ic_series / rank_ic_series — perfect correlation, constants, NaN-awareness
# ---------------------------------------------------------------------------


def _toy_factor_fwd(rng_seed: int = 0):
    """Two dates, five symbols, distinct values so ranks are unambiguous."""
    rng = np.random.default_rng(rng_seed)
    factor = rng.normal(size=(2, 5))
    return factor


def test_ic_perfect_positive_and_negative():
    factor = _toy_factor_fwd(7)
    # factor == fwd  -> IC ≈ RankIC ≈ +1 on every date.
    ic = ic_series(factor, factor)
    ric = rank_ic_series(factor, factor)
    np.testing.assert_allclose(ic, 1.0, atol=1e-9)
    np.testing.assert_allclose(ric, 1.0, atol=1e-9)
    # factor == -fwd -> IC ≈ RankIC ≈ -1.
    ic_neg = ic_series(factor, -factor)
    ric_neg = rank_ic_series(factor, -factor)
    np.testing.assert_allclose(ic_neg, -1.0, atol=1e-9)
    np.testing.assert_allclose(ric_neg, -1.0, atol=1e-9)


def test_ic_constant_date_is_nan():
    # A date whose factor row is constant has zero variance -> IC/RankIC NaN there.
    factor = np.array([[5.0, 5.0, 5.0, 5.0], [1.0, 2.0, 3.0, 4.0]])
    fwd = np.array([[1.0, 2.0, 3.0, 4.0], [1.0, 2.0, 3.0, 4.0]])
    ic = ic_series(factor, fwd)
    ric = rank_ic_series(factor, fwd)
    assert np.isnan(ic[0]) and np.isnan(ric[0])  # constant factor row
    np.testing.assert_allclose([ic[1], ric[1]], [1.0, 1.0], atol=1e-9)


def test_ic_nan_aware_per_symbol():
    # A missing symbol on a date must not poison that date's cross-section: the
    # IC is computed over the jointly-finite symbols only. Here both rows agree
    # perfectly on the finite symbols, so IC == +1 despite the NaN holes.
    factor = np.array([[1.0, 2.0, np.nan, 4.0], [4.0, np.nan, 2.0, 1.0]])
    fwd = np.array([[1.0, 2.0, 99.0, 4.0], [4.0, 99.0, 2.0, 1.0]])
    ic = ic_series(factor, fwd)
    ric = rank_ic_series(factor, fwd)
    np.testing.assert_allclose(ic, 1.0, atol=1e-9)
    np.testing.assert_allclose(ric, 1.0, atol=1e-9)


def test_ic_fewer_than_two_valid_is_nan():
    # Only one jointly-finite symbol on the date -> undefined correlation -> NaN.
    factor = np.array([[1.0, np.nan, np.nan, np.nan]])
    fwd = np.array([[1.0, 2.0, 3.0, 4.0]])
    assert np.isnan(ic_series(factor, fwd)[0])
    assert np.isnan(rank_ic_series(factor, fwd)[0])


def test_rank_ic_is_spearman_not_pearson():
    # Monotone-but-nonlinear relation: Spearman (RankIC) == 1, Pearson (IC) < 1.
    factor = np.array([[1.0, 2.0, 3.0, 4.0]])
    fwd = np.array([[1.0, 8.0, 27.0, 64.0]])  # cube — monotone, convex
    np.testing.assert_allclose(rank_ic_series(factor, fwd), 1.0, atol=1e-9)
    assert ic_series(factor, fwd)[0] < 1.0 - 1e-6


# ---------------------------------------------------------------------------
# ic_summary — hand-checked mean and mean/std
# ---------------------------------------------------------------------------


def test_ic_summary_hand_checked():
    # series = [0.1, 0.2, 0.3, NaN]; NaN ignored.
    # mean = 0.2; ddof=1 std of {0.1,0.2,0.3} = sqrt(((-.1)^2+0+(.1)^2)/2)=0.1.
    # icir = mean/std = 0.2 / 0.1 = 2.0.
    series = np.array([0.1, 0.2, 0.3, np.nan])
    mean, icir = ic_summary(series)
    assert mean == pytest.approx(0.2)
    assert icir == pytest.approx(2.0)


def test_ic_summary_empty_and_singleton():
    # All-NaN -> (NaN, NaN).
    m, ir = ic_summary(np.array([np.nan, np.nan]))
    assert np.isnan(m) and np.isnan(ir)
    # Single finite point -> mean defined, icir undefined (ddof=1 std needs >=2).
    m1, ir1 = ic_summary(np.array([0.4, np.nan]))
    assert m1 == pytest.approx(0.4) and np.isnan(ir1)


# ---------------------------------------------------------------------------
# evaluate_ic — keys, per-horizon means, min-horizon headline
# ---------------------------------------------------------------------------


def test_evaluate_ic_keys_and_headline_min_horizon():
    rng = np.random.default_rng(11)
    factor = rng.normal(size=(8, 6))
    # Build forward-return panels per horizon. h=1 is a perfect copy of the
    # factor (IC==1); h=3 is the negated factor (IC==-1). Headline must come
    # from the *minimum* horizon (h=1).
    fwd_by_h = {3: -factor, 1: factor.copy()}
    res = evaluate_ic(factor, fwd_by_h)

    expected_keys = {
        "ic",
        "icir",
        "rank_ic",
        "rank_icir",
        "ic_by_horizon",
        "ic_series",
        "rank_ic_series",
    }
    assert set(res.keys()) == expected_keys

    # Headline == min horizon (h=1), which is the perfect-positive copy.
    assert res["ic"] == pytest.approx(1.0, abs=1e-9)
    assert res["rank_ic"] == pytest.approx(1.0, abs=1e-9)
    # ic_by_horizon carries the mean RankIC of *each* horizon.
    assert set(res["ic_by_horizon"].keys()) == {1, 3}
    assert res["ic_by_horizon"][1] == pytest.approx(1.0, abs=1e-9)
    assert res["ic_by_horizon"][3] == pytest.approx(-1.0, abs=1e-9)
    # The returned series are the headline (h=1) series.
    assert np.asarray(res["ic_series"]).shape == (8,)
    np.testing.assert_allclose(
        np.asarray(res["rank_ic_series"]), 1.0, atol=1e-9
    )


def test_evaluate_ic_empty_raises():
    with pytest.raises(ValueError, match="at least one horizon"):
        evaluate_ic(np.ones((4, 3)), {})


# ---------------------------------------------------------------------------
# decay_halflife — log-linear fit and degenerate cases
# ---------------------------------------------------------------------------


def test_decay_halflife_exponential():
    # IC(h) = 0.1 * exp(-0.1 * h) -> lambda = 0.1 -> half-life = ln(2)/0.1.
    lam = 0.1
    ic_by_horizon = {h: 0.1 * math.exp(-lam * h) for h in (1, 2, 5, 10, 20)}
    hl = decay_halflife(ic_by_horizon)
    assert hl == pytest.approx(math.log(2.0) / lam, rel=1e-9)


def test_decay_halflife_degenerate_returns_none():
    # Fewer than two positive-IC horizons -> unidentifiable slope -> None.
    assert decay_halflife({1: 0.2}) is None
    assert decay_halflife({1: 0.2, 5: -0.1, 10: -0.3}) is None  # only one positive
    # Non-decaying / strengthening signal (lambda <= 0) -> None.
    assert decay_halflife({1: 0.1, 5: 0.2, 10: 0.3}) is None
    # Empty mapping -> None.
    assert decay_halflife({}) is None


# ---------------------------------------------------------------------------
# group_returns — monotonicity and long-short spread
# ---------------------------------------------------------------------------


def test_group_returns_monotone_top_beats_bottom():
    # Construct a panel where higher factor => higher forward return, on every
    # date. With fwd == factor the quantile means strictly increase Q1..Qn.
    rng = np.random.default_rng(21)
    # 30 dates x 25 symbols so each of 5 buckets gets 5 names.
    factor = rng.normal(size=(30, 25))
    fwd = factor.copy()  # perfectly aligned: monotone by construction
    res = group_returns(factor, fwd, n_groups=5)
    q = res["quantile_returns"]
    assert q["Q5"] > q["Q1"]  # top quantile beats bottom
    assert res["long_short"] > 0.0
    assert res["long_short"] == pytest.approx(q["Q5"] - q["Q1"])
    assert res["monotonic"] is True
    # Strictly increasing quantile means.
    means = [q[f"Q{g}"] for g in range(1, 6)]
    assert all(b > a for a, b in zip(means, means[1:]))


def test_group_returns_long_short_sign_flips_with_factor():
    rng = np.random.default_rng(22)
    factor = rng.normal(size=(30, 25))
    # Negatively-aligned forward returns -> long-short spread is negative.
    res = group_returns(factor, -factor, n_groups=5)
    assert res["long_short"] < 0.0
    assert res["monotonic"] is False  # decreasing, not strictly increasing


# ---------------------------------------------------------------------------
# turnover — static (~0) vs reshuffled (>0)
# ---------------------------------------------------------------------------


def test_turnover_identical_ranking_is_zero():
    # Every date has an identical cross-sectional ranking -> autocorr ≈ 1 ->
    # turnover ≈ 0. (Values differ in level but the *ordering* is constant.)
    base = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
    factor = np.vstack([base + d for d in range(10)])  # 10 dates, same ordering
    tn = turnover(factor, lag=1)
    assert tn == pytest.approx(0.0, abs=1e-9)
    # Underlying autocorrelation is ~1 on every comparable date.
    ac = rank_autocorr(factor, lag=1)
    np.testing.assert_allclose(ac[1:], 1.0, atol=1e-9)
    assert np.isnan(ac[0])  # no prior cross-section for the first row


def test_turnover_reshuffled_ranking_is_positive():
    # Alternate between an ordering and its exact reverse every day. Each step
    # perfectly anti-correlates (autocorr == -1) -> turnover = 1 - (-1) = 2 > 0.
    asc = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
    desc = asc[::-1].copy()
    factor = np.vstack([asc if d % 2 == 0 else desc for d in range(10)])
    tn = turnover(factor, lag=1)
    assert tn > 0.0
    assert tn == pytest.approx(2.0, abs=1e-9)


def test_turnover_random_ordering_in_open_interval():
    # A genuinely random new ranking each day churns but is not a clean reversal:
    # turnover sits strictly inside (0, 2) and is clearly > a static factor's ~0.
    rng = np.random.default_rng(31)
    factor = rng.normal(size=(40, 12))
    tn = turnover(factor, lag=1)
    assert 0.0 < tn < 2.0
    assert tn > 0.1  # plainly larger than the ~0 of a static ranking
