"""Rebalance scheduling — design-doc section 3 (rebalance algorithms).

Pure numpy/pandas over the ``(T,)`` date axis and the ``(T, N)`` factor matrix.
Every scheduler returns **integer indices** into the date axis (positions where a
rebalance occurs), so the simulator can index NAV/price matrices directly without
reconciling date types. ``dates`` may be ``datetime.date``/``datetime``, numpy
``datetime64``, pandas ``Timestamp`` or ``YYYY-MM-DD`` strings — all are coerced
to a pandas ``DatetimeIndex`` for the calendar grouping.

Four algorithm families (section 3):

* 3.1 calendar  — :func:`calendar_dates` groups by ISO week / calendar month /
  quarter and picks the first or last trading day of each group (or the nearest
  occurrence of a named weekday, best-effort).
* 3.2 threshold — :func:`should_rebalance` fires when any symbol's decile-rank
  shift or weight drift exceeds the configured threshold, returning the triggered
  mask for a partial rebalance.
* 3.4 signal    — :func:`signal_autocorr_dates` rebalances when the 5-day rolling
  cross-sectional rank autocorrelation of the factor drops below the floor.
* :func:`rebalance_dates` dispatches on ``config.rebalance_type`` and enforces
  ``min_rebalance_interval`` (measured in **index steps**), returning a sorted,
  unique ``list[int]``.

Section 3.3 (optimisation-based) is a *weight* method, not a schedule — it runs on
the calendar/threshold dates produced here — so it is handled in the weight layer,
not this module.

**Grounding to reality.** The A-share ``rebalance_around_index`` /
``inclusion_anticipation`` adjustments (3.1, section 7.3) need index-reconstitution
dates the DataStore does not provide for US equities; :func:`calendar_dates`
accepts an optional ``index_recon_dates`` argument and no-ops gracefully (drops
nothing) when it is absent, exactly as documented in :mod:`assay.portfolio.config`.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Sequence

import numpy as np
import pandas as pd

from assay.evaluator.turnover import rank_autocorr

# Lowercase weekday name -> Python weekday() ordinal (Mon=0 .. Sun=6). Accepts the
# common 3-letter abbreviations the spec uses ('Wed') plus full names.
_WEEKDAYS = {
    "mon": 0, "monday": 0,
    "tue": 1, "tues": 1, "tuesday": 1,
    "wed": 2, "weds": 2, "wednesday": 2,
    "thu": 3, "thur": 3, "thurs": 3, "thursday": 3,
    "fri": 4, "friday": 4,
    "sat": 5, "saturday": 5,
    "sun": 6, "sunday": 6,
}


# ---------------------------------------------------------------------------
# date coercion
# ---------------------------------------------------------------------------
def _to_index(dates: Sequence[Any]) -> pd.DatetimeIndex:
    """Coerce a heterogeneous date sequence to a pandas ``DatetimeIndex``.

    Tolerant of ``datetime.date``/``datetime``, numpy ``datetime64``, pandas
    ``Timestamp`` and ISO strings. Unparseable entries become ``NaT`` (they are
    skipped by the groupers, which only emit indices for valid dates).
    """
    return pd.DatetimeIndex(pd.to_datetime(list(dates), errors="coerce"))


# ---------------------------------------------------------------------------
# 3.1  Calendar-based
# ---------------------------------------------------------------------------
def calendar_dates(
    dates: Sequence[Any],
    frequency: str,
    rebalance_day: str = "last",
    *,
    index_recon_dates: Sequence[Any] | None = None,
) -> list[int]:
    """Rebalance indices for a fixed calendar schedule (design-doc 3.1).

    ``frequency`` is one of ``daily`` / ``weekly`` / ``monthly`` / ``quarterly``.
    Trading dates are grouped by ISO week (weekly), calendar month (monthly) or
    calendar quarter (quarterly); within each group the ``first`` or ``last``
    trading day is selected. ``daily`` returns every valid date index.

    ``rebalance_day`` accepts ``'first'``, ``'last'``, or a weekday name
    (e.g. ``'Wed'``): for a weekday the chosen date in each group is the one whose
    weekday matches (the last such match in the group), falling back to the
    group's last trading day when no exact match exists (best-effort).

    ``index_recon_dates`` (A-share, section 7.3) — when supplied, any candidate
    within 3 calendar days of a reconstitution date is dropped; ``None`` (the only
    case for US data) is a graceful no-op.
    """
    freq = frequency.lower()
    idx = _to_index(dates)
    valid = np.flatnonzero(~idx.isna())
    if valid.size == 0:
        return []

    if freq == "daily":
        picks = list(valid)
    else:
        if freq == "weekly":
            iso = idx.isocalendar()
            group_key = list(zip(iso["year"].to_numpy(), iso["week"].to_numpy()))
        elif freq == "monthly":
            group_key = list(zip(idx.year.to_numpy(), idx.month.to_numpy()))
        elif freq == "quarterly":
            group_key = list(zip(idx.year.to_numpy(), idx.quarter.to_numpy()))
        else:
            raise ValueError(
                f"frequency={frequency!r} not a calendar frequency "
                "(daily|weekly|monthly|quarterly)"
            )
        picks = _pick_per_group(idx, valid, group_key, rebalance_day)

    # A-share: drop candidates near an index reconstitution (no-op when absent).
    if index_recon_dates is not None:
        recon = _to_index(index_recon_dates)
        recon = recon[~recon.isna()]
        if len(recon):
            recon_days = recon.normalize().to_numpy()
            kept = []
            for i in picks:
                d = idx[i].normalize().to_numpy()
                gap = np.abs((recon_days - d) / np.timedelta64(1, "D"))
                if not (gap <= 3).any():
                    kept.append(i)
            picks = kept

    return sorted(set(int(i) for i in picks))


def _pick_per_group(
    idx: pd.DatetimeIndex,
    valid: np.ndarray,
    group_key: list[tuple],
    rebalance_day: str,
) -> list[int]:
    """Select one trading-day index per consecutive calendar group.

    Groups are formed over the valid (non-NaT) positions in chronological order;
    ``rebalance_day`` chooses first / last / nearest-named-weekday within each.
    """
    day = rebalance_day.lower()
    want_wd = _WEEKDAYS.get(day)
    picks: list[int] = []
    cur_key: tuple | None = None
    group: list[int] = []

    def _emit(g: list[int]) -> None:
        if not g:
            return
        if day == "first":
            picks.append(g[0])
        elif want_wd is not None:
            matches = [i for i in g if idx[i].weekday() == want_wd]
            picks.append(matches[-1] if matches else g[-1])
        else:  # 'last' (default) and any unrecognised value
            picks.append(g[-1])

    for i in valid:
        k = group_key[i]
        if cur_key is None or k != cur_key:
            _emit(group)
            group = []
            cur_key = k
        group.append(int(i))
    _emit(group)
    return picks


# ---------------------------------------------------------------------------
# 3.2  Threshold-based
# ---------------------------------------------------------------------------
def should_rebalance(
    current_w: np.ndarray,
    target_w: np.ndarray,
    current_ranks: np.ndarray,
    new_ranks: np.ndarray,
    config: Any,
) -> tuple[bool, np.ndarray]:
    """Threshold rebalance check over aligned ``(N,)`` symbol vectors (design-doc 3.2).

    Fires when, for any symbol, the decile-rank shift exceeds
    ``config.threshold_rank_shift`` **or** the weight drift exceeds
    ``config.threshold_weight_drift``. Returns ``(should_rebalance, triggered)``
    where ``triggered`` is a boolean ``(N,)`` mask of the symbols that crossed a
    threshold — the simulator trades only those, leaving the rest unchanged
    (partial rebalance). NaN entries in any input never trigger.
    """
    cw = np.asarray(current_w, dtype=np.float64).reshape(-1)
    tw = np.asarray(target_w, dtype=np.float64).reshape(-1)
    cr = np.asarray(current_ranks, dtype=np.float64).reshape(-1)
    nr = np.asarray(new_ranks, dtype=np.float64).reshape(-1)
    n = cw.size
    triggered = np.zeros(n, dtype=bool)
    if not (tw.size == cr.size == nr.size == n) or n == 0:
        return False, triggered

    with np.errstate(invalid="ignore"):
        rank_shift = np.abs(nr - cr)
        weight_drift = np.abs(tw - cw)
        trig_rank = np.isfinite(rank_shift) & (rank_shift > config.threshold_rank_shift)
        trig_weight = np.isfinite(weight_drift) & (weight_drift > config.threshold_weight_drift)
    triggered = trig_rank | trig_weight
    return bool(triggered.any()), triggered


# ---------------------------------------------------------------------------
# 3.4  Signal-based
# ---------------------------------------------------------------------------
def signal_autocorr_dates(
    factor: np.ndarray, dates: Sequence[Any], config: Any
) -> list[int]:
    """Rebalance indices driven by factor stability (design-doc 3.4).

    Computes the per-date 1-day cross-sectional rank autocorrelation of the
    ``(T, N)`` factor, smooths it with a 5-day trailing mean, and emits a rebalance
    index whenever the smoothed autocorrelation drops below
    ``config.signal_autocorr_floor`` — i.e. the ranking has churned enough to be
    worth re-trading. ``config.min_rebalance_interval`` (in index steps) is honoured
    between consecutive emissions. NaN autocorrelations (warm-up rows, degenerate
    cross-sections) are skipped.
    """
    f = np.ascontiguousarray(factor, dtype=np.float64)
    if f.ndim != 2:
        raise ValueError("factor must be a 2-D (T, N) matrix")
    t_n = f.shape[0]
    if t_n == 0:
        return []
    ac = rank_autocorr(f, lag=1)  # (T,) per-date Spearman rank autocorr, NaN-aware
    rolling = _trailing_mean(ac, window=5)

    floor = config.signal_autocorr_floor
    min_gap = max(1, int(config.min_rebalance_interval))
    out: list[int] = []
    last = None
    for i in range(t_n):
        v = rolling[i]
        if not np.isfinite(v):
            continue
        if last is not None and (i - last) < min_gap:
            continue
        if v < floor:
            out.append(i)
            last = i
    return out


def _trailing_mean(a: np.ndarray, window: int) -> np.ndarray:
    """NaN-aware trailing mean over ``window`` periods (``(T,)`` -> ``(T,)``).

    Each position averages the up-to-``window`` finite values ending at it; a
    position with no finite value in its window stays ``NaN``. Mirrors pandas
    ``rolling(window).mean()`` but tolerant of NaN gaps (skipna)."""
    a = np.asarray(a, dtype=np.float64).reshape(-1)
    n = a.size
    out = np.full(n, np.nan)
    w = max(1, int(window))
    for i in range(n):
        lo = max(0, i - w + 1)
        seg = a[lo : i + 1]
        seg = seg[np.isfinite(seg)]
        if seg.size:
            out[i] = seg.mean()
    return out


# ---------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------
def _enforce_min_interval(indices: Sequence[int], min_gap: int) -> list[int]:
    """Greedily drop indices closer than ``min_gap`` steps to the prior kept one."""
    gap = max(1, int(min_gap))
    kept: list[int] = []
    last = None
    for i in sorted(set(int(x) for x in indices)):
        if last is None or (i - last) >= gap:
            kept.append(i)
            last = i
    return kept


def rebalance_dates(factor: np.ndarray, dates: Sequence[Any], config: Any) -> list[int]:
    """Dispatch on ``config.rebalance_type`` to produce rebalance indices (section 3).

    * ``daily`` / ``weekly`` / ``monthly`` / ``quarterly`` -> :func:`calendar_dates`
      with ``config.rebalance_day`` (A-share ``rebalance_around_index`` is a no-op
      here since the DataStore provides no reconstitution dates).
    * ``signal`` -> :func:`signal_autocorr_dates`.
    * ``threshold`` -> threshold checks are *per-date* (3.2) and depend on the
      evolving live portfolio, which only the simulator holds; the schedule layer
      therefore proposes a daily candidate set and lets the simulator apply
      :func:`should_rebalance` at each step. (A pure daily candidate list, clamped
      by ``min_rebalance_interval``, is the correct conservative schedule.)

    All paths return a sorted, unique ``list[int]`` with
    ``config.min_rebalance_interval`` (in index steps) enforced. The ``signal``
    path already honours the interval internally; re-clamping is idempotent.
    """
    rtype = config.rebalance_type
    if rtype in ("daily", "weekly", "monthly", "quarterly"):
        idxs = calendar_dates(dates, rtype, config.rebalance_day)
    elif rtype == "signal":
        idxs = signal_autocorr_dates(factor, dates, config)
    elif rtype == "threshold":
        # Daily candidates; the simulator's should_rebalance() gates each one.
        idxs = calendar_dates(dates, "daily", "last")
    else:  # pragma: no cover - config validation already constrains the set
        raise ValueError(f"unknown rebalance_type {config.rebalance_type!r}")
    return _enforce_min_interval(idxs, config.min_rebalance_interval)
