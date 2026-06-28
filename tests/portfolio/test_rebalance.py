"""Tests for the rebalance scheduler (design-doc section 3).

Offline, synthetic ``(T, N)`` factor panels and date axes built with numpy.
Run with::

    PYTHONPATH=src python -m pytest tests/portfolio/test_rebalance.py -q
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import pytest

from assay.portfolio import rebalance as rb
from assay.portfolio.config import PortfolioBacktestConfig


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _business_days(n: int, start=dt.date(2021, 1, 4)) -> list[dt.date]:
    """n consecutive Mon-Fri trading dates (skip weekends)."""
    out: list[dt.date] = []
    d = start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d = d + dt.timedelta(days=1)
    return out


def _cfg(**kw):
    base = dict(period_start="2021-01-04", period_end="2021-12-31")
    base.update(kw)
    return PortfolioBacktestConfig(**base)


# ---------------------------------------------------------------------------
# 3.1 calendar
# ---------------------------------------------------------------------------
def test_calendar_daily_returns_all():
    dates = _business_days(20)
    idx = rb.calendar_dates(dates, "daily")
    assert idx == list(range(20))


def test_calendar_monthly_last_and_first():
    dates = _business_days(70)  # ~ 3.5 months of business days
    last = rb.calendar_dates(dates, "monthly", "last")
    first = rb.calendar_dates(dates, "monthly", "first")
    # one pick per distinct (year, month) group
    months = sorted({(dates[i].year, dates[i].month) for i in range(len(dates))})
    assert len(last) == len(months)
    assert len(first) == len(months)
    # 'last' picks are strictly later within shared months than 'first'
    assert last[0] >= first[0]
    # each 'last' index is the final business day of its month in the panel
    for i in last:
        ym = (dates[i].year, dates[i].month)
        same = [j for j in range(len(dates)) if (dates[j].year, dates[j].month) == ym]
        assert i == same[-1]


def test_calendar_weekly_groups_by_iso_week():
    dates = _business_days(15)  # 3 ISO weeks of weekdays
    idx = rb.calendar_dates(dates, "weekly", "last")
    iso = [d.isocalendar()[:2] for d in dates]
    assert len(idx) == len({w for w in iso})
    # last business day of week 1 is a Friday
    assert dates[idx[0]].weekday() == 4


def test_calendar_quarterly():
    # span two quarters
    dates = _business_days(130)
    idx = rb.calendar_dates(dates, "quarterly", "last")
    quarters = sorted({(d.year, (d.month - 1) // 3 + 1) for d in dates})
    assert len(idx) == len(quarters)


def test_calendar_named_weekday():
    dates = _business_days(20)
    idx = rb.calendar_dates(dates, "weekly", "Wed")
    for i in idx:
        # each pick is a Wednesday when one exists in that ISO week
        wk = dates[i].isocalendar()[:2]
        weekdays = [dates[j].weekday() for j in range(len(dates))
                    if dates[j].isocalendar()[:2] == wk]
        if 2 in weekdays:
            assert dates[i].weekday() == 2


def test_calendar_index_recon_noop_when_absent_and_drops_when_present():
    dates = _business_days(40)
    base = rb.calendar_dates(dates, "weekly", "last")
    # no recon dates -> unchanged
    assert rb.calendar_dates(dates, "weekly", "last", index_recon_dates=None) == base
    # supply a recon date equal to one pick -> that pick (within 3d) is dropped
    drop_date = dates[base[1]]
    filtered = rb.calendar_dates(dates, "weekly", "last", index_recon_dates=[drop_date])
    assert base[1] not in filtered
    assert len(filtered) < len(base)


def test_calendar_string_dates():
    dates = [d.isoformat() for d in _business_days(30)]
    idx = rb.calendar_dates(dates, "monthly", "last")
    assert idx and all(isinstance(i, int) for i in idx)


# ---------------------------------------------------------------------------
# 3.2 threshold
# ---------------------------------------------------------------------------
def test_should_rebalance_weight_drift_trigger():
    cfg = _cfg(threshold_weight_drift=0.05, threshold_rank_shift=2)
    cur_w = np.array([0.2, 0.2, 0.2])
    tgt_w = np.array([0.2, 0.30, 0.2])  # +0.10 drift on idx 1 (> 0.05)
    ranks = np.array([1.0, 2.0, 3.0])
    fire, mask = rb.should_rebalance(cur_w, tgt_w, ranks, ranks, cfg)
    assert fire
    assert mask.tolist() == [False, True, False]


def test_should_rebalance_rank_shift_trigger():
    cfg = _cfg(threshold_rank_shift=2, threshold_weight_drift=0.20)
    w = np.array([0.3, 0.3, 0.3])
    old = np.array([1.0, 2.0, 3.0])
    new = np.array([1.0, 2.0, 7.0])  # idx 2 shifts by 4 (> 2)
    fire, mask = rb.should_rebalance(w, w, old, new, cfg)
    assert fire and mask.tolist() == [False, False, True]


def test_should_rebalance_no_trigger():
    cfg = _cfg(threshold_rank_shift=2, threshold_weight_drift=0.10)
    w = np.array([0.3, 0.3, 0.3])
    ranks = np.array([1.0, 2.0, 3.0])
    fire, mask = rb.should_rebalance(w, w, ranks, ranks, cfg)
    assert not fire and not mask.any()


def test_should_rebalance_nan_does_not_trigger():
    cfg = _cfg(threshold_rank_shift=2, threshold_weight_drift=0.05)
    cur_w = np.array([0.2, np.nan, 0.2])
    tgt_w = np.array([0.2, np.nan, 0.2])
    ranks = np.array([1.0, np.nan, 3.0])
    fire, mask = rb.should_rebalance(cur_w, tgt_w, ranks, ranks, cfg)
    assert not fire


# ---------------------------------------------------------------------------
# 3.4 signal
# ---------------------------------------------------------------------------
def test_signal_autocorr_dates_triggers_on_churn():
    rng = np.random.default_rng(0)
    T, N = 60, 30
    # first half: stable ranking (high autocorr); second half: random (churn)
    base = rng.normal(0, 1, N)
    factor = np.empty((T, N))
    for t in range(T):
        if t < 30:
            factor[t] = base + rng.normal(0, 0.01, N)  # near-identical ranking
        else:
            factor[t] = rng.normal(0, 1, N)            # reshuffled each day
    cfg = _cfg(rebalance_type="signal", signal_autocorr_floor=0.7,
               min_rebalance_interval=5)
    idx = rb.signal_autocorr_dates(factor, _business_days(T), cfg)
    # rebalances should occur only in the churning second half
    assert idx
    assert all(i >= 30 for i in idx)
    # min_rebalance_interval honoured
    assert all(b - a >= 5 for a, b in zip(idx, idx[1:]))


def test_signal_autocorr_stable_factor_no_rebalance():
    T, N = 40, 20
    base = np.linspace(-1, 1, N)
    factor = np.tile(base, (T, 1))  # identical every day -> autocorr ~ 1
    cfg = _cfg(rebalance_type="signal", signal_autocorr_floor=0.7)
    idx = rb.signal_autocorr_dates(factor, _business_days(T), cfg)
    assert idx == []


# ---------------------------------------------------------------------------
# dispatch + min interval
# ---------------------------------------------------------------------------
def test_rebalance_dates_dispatch_calendar():
    dates = _business_days(70)
    cfg = _cfg(rebalance_type="monthly", rebalance_day="last")
    factor = np.random.default_rng(1).normal(0, 1, (70, 10))
    idx = rb.rebalance_dates(factor, dates, cfg)
    assert idx == sorted(set(idx))
    assert idx == rb.calendar_dates(dates, "monthly", "last")


def test_rebalance_dates_min_interval_enforced():
    dates = _business_days(60)
    # daily would yield every index; min interval thins it out
    cfg = _cfg(rebalance_type="daily", min_rebalance_interval=10)
    factor = np.zeros((60, 5))
    idx = rb.rebalance_dates(factor, dates, cfg)
    assert all(b - a >= 10 for a, b in zip(idx, idx[1:]))
    assert idx[0] == 0


def test_rebalance_dates_threshold_yields_daily_candidates():
    dates = _business_days(30)
    cfg = _cfg(rebalance_type="threshold", min_rebalance_interval=1)
    factor = np.zeros((30, 5))
    idx = rb.rebalance_dates(factor, dates, cfg)
    assert idx == list(range(30))


def test_rebalance_dates_signal_path():
    rng = np.random.default_rng(2)
    T, N = 40, 15
    factor = rng.normal(0, 1, (T, N))  # fully random -> low autocorr -> triggers
    cfg = _cfg(rebalance_type="signal", signal_autocorr_floor=0.9,
               min_rebalance_interval=3)
    idx = rb.rebalance_dates(factor, _business_days(T), cfg)
    assert idx == sorted(set(idx))
    assert all(b - a >= 3 for a, b in zip(idx, idx[1:]))


def test_rebalance_dates_returns_ints():
    dates = _business_days(40)
    cfg = _cfg(rebalance_type="weekly")
    idx = rb.rebalance_dates(np.zeros((40, 3)), dates, cfg)
    assert all(isinstance(i, int) for i in idx)
