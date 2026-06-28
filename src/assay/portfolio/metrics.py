"""Performance metrics over a daily NAV series — design-doc section 4.

Pure numpy (pandas only for the calendar grouping in :func:`monthly_returns`). The
input is a ``(T,)`` daily NAV array indexed from 1.0 at period start, optionally
paired with a benchmark NAV of the same length. Every function is **NaN-safe**:
non-finite NAV/return entries are dropped (or yield ``NaN``) rather than poisoning
the whole series, and a metric that cannot be defined (empty series, zero
variance, no drawdown) returns ``NaN`` rather than raising.

Sections:

* 4.1 return metrics  — :func:`returns_from_nav`, :func:`total_return`,
  :func:`annualized_return`, :func:`excess_return`.
* 4.2 risk-adjusted   — :func:`sharpe`, :func:`sortino`, :func:`calmar`,
  :func:`information_ratio`, :func:`max_drawdown`, :func:`tracking_error`,
  :func:`beta`, :func:`alpha_capm`.
* 4.3 turnover & cost — :func:`annual_turnover`, :func:`avg_holding_days`,
  :func:`cost_drag`.
* :func:`monthly_returns` — ``'YYYY-MM' -> return`` for the heatmap (4.x display).

:func:`compute_metrics` is the headline reducer the simulator calls: it returns a
flat dict of every section-4 scalar (plus drawdown date-indices and the monthly
map), each entry already NaN-safe, keyed to mirror :class:`PortfolioReport` fields.
Annualisation uses 252 trading days/year throughout.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Sequence

import numpy as np

_PPY = 252  # trading days per year (annualisation constant, design-doc §4)


# ---------------------------------------------------------------------------
# internal helpers
# ---------------------------------------------------------------------------
def _finite(a: np.ndarray) -> np.ndarray:
    """Return the finite entries of ``a`` as a contiguous float64 1-D array."""
    a = np.asarray(a, dtype=np.float64).reshape(-1)
    return a[np.isfinite(a)]


def _as_nav(nav: Sequence[float] | np.ndarray) -> np.ndarray:
    """Coerce a NAV sequence to a float64 1-D array (no filtering)."""
    return np.asarray(nav, dtype=np.float64).reshape(-1)


def _degenerate_std(sd: float, scale: float) -> bool:
    """True if dispersion ``sd`` is effectively zero for a series of scale ``scale``.

    A perfectly (or near-perfectly) constant series has a ``std`` that is either
    exactly 0 or floating-point dust (~1e-16 of the data scale) from non-exact
    summation. Dividing a ratio metric by that dust yields an absurd ~1e16 value,
    so we treat such dispersion as zero (-> the metric is undefined / ``NaN``).
    """
    if not np.isfinite(sd) or sd <= 0.0:
        return True
    return sd <= 1e-12 * max(abs(scale), 1.0)


# ---------------------------------------------------------------------------
# 4.1  Return metrics
# ---------------------------------------------------------------------------
def returns_from_nav(nav: Sequence[float] | np.ndarray) -> np.ndarray:
    """Simple period returns ``NAV_t / NAV_{t-1} - 1`` from a NAV series.

    Returns a ``(T-1,)`` float64 array; an entry is ``NaN`` where either adjacent
    NAV is non-finite or the prior NAV is non-positive (return undefined).
    """
    v = _as_nav(nav)
    if v.size < 2:
        return np.empty(0, dtype=np.float64)
    prev, cur = v[:-1], v[1:]
    out = np.full(prev.shape, np.nan)
    ok = np.isfinite(prev) & np.isfinite(cur) & (prev > 0)
    out[ok] = cur[ok] / prev[ok] - 1.0
    return out


def total_return(nav: Sequence[float] | np.ndarray) -> float:
    """Cumulative return ``NAV_end / NAV_start - 1`` over the full series (4.1).

    Uses the first/last finite NAV points; ``NaN`` if fewer than two finite points
    or the starting NAV is non-positive.
    """
    v = _as_nav(nav)
    fin = np.flatnonzero(np.isfinite(v))
    if fin.size < 2:
        return float("nan")
    start, end = v[fin[0]], v[fin[-1]]
    if start <= 0:
        return float("nan")
    return float(end / start - 1.0)


def annualized_return(
    nav: Sequence[float] | np.ndarray, periods_per_year: int = _PPY
) -> float:
    """Geometric annualised return ``(1 + total)^(252/T) - 1`` (4.1).

    ``T`` is the number of return periods (finite-NAV span − 1). ``NaN`` if total
    return is undefined or the period spans fewer than two points; gross losses to
    ``<= -100%`` clamp the base to 0 so the power is well-defined.
    """
    v = _as_nav(nav)
    fin = np.flatnonzero(np.isfinite(v))
    if fin.size < 2:
        return float("nan")
    tot = total_return(v)
    if not np.isfinite(tot):
        return float("nan")
    n_periods = float(fin[-1] - fin[0])  # number of compounding steps
    if n_periods <= 0:
        return float("nan")
    base = max(1.0 + tot, 0.0)
    return float(base ** (periods_per_year / n_periods) - 1.0)


def excess_return(
    port_nav: Sequence[float] | np.ndarray,
    bench_nav: Sequence[float] | np.ndarray | None,
) -> float:
    """Total portfolio return minus total benchmark return (4.1).

    ``NaN`` if no benchmark is supplied or either total return is undefined.
    """
    if bench_nav is None:
        return float("nan")
    pr = total_return(port_nav)
    br = total_return(bench_nav)
    if not (np.isfinite(pr) and np.isfinite(br)):
        return float("nan")
    return float(pr - br)


def log_return(nav: Sequence[float] | np.ndarray) -> float:
    """Total log return ``sum(log(NAV_t / NAV_{t-1}))`` over finite steps (4.1)."""
    r = returns_from_nav(nav)
    r = r[np.isfinite(r)]
    if r.size == 0:
        return float("nan")
    base = 1.0 + r
    base = base[base > 0]
    if base.size == 0:
        return float("nan")
    return float(np.log(base).sum())


# ---------------------------------------------------------------------------
# 4.2  Risk-adjusted metrics
# ---------------------------------------------------------------------------
def sharpe(
    returns: Sequence[float] | np.ndarray, rf_annual: float = 0.0, ppy: int = _PPY
) -> float:
    """Annualised Sharpe ``(mean(R) - rf_daily) / std(R) * sqrt(ppy)`` (4.2).

    ``rf_annual`` is de-annualised to a per-period rate; ``std`` uses ``ddof=1``
    over the finite returns. ``NaN`` if fewer than two finite returns or zero
    volatility.
    """
    r = _finite(returns)
    if r.size < 2:
        return float("nan")
    sd = r.std(ddof=1)
    if _degenerate_std(sd, r.mean()):
        return float("nan")
    rf_daily = rf_annual / ppy
    return float((r.mean() - rf_daily) / sd * np.sqrt(ppy))


def sortino(
    returns: Sequence[float] | np.ndarray, rf_annual: float = 0.0, ppy: int = _PPY
) -> float:
    """Annualised Sortino: excess mean over downside deviation (4.2).

    Downside deviation is the RMS of the negative-side excess returns (returns
    below the per-period ``rf``), ``ddof=0`` per convention. ``NaN`` if fewer than
    two finite returns or no downside dispersion.
    """
    r = _finite(returns)
    if r.size < 2:
        return float("nan")
    rf_daily = rf_annual / ppy
    excess = r - rf_daily
    downside = np.minimum(excess, 0.0)
    dd = np.sqrt(np.mean(downside**2))
    if _degenerate_std(dd, r.mean()):
        return float("nan")
    return float(excess.mean() / dd * np.sqrt(ppy))


def calmar(annual_ret: float, max_dd: float) -> float:
    """Calmar ratio ``annual_return / max_drawdown`` (4.2).

    ``max_dd`` is the positive drawdown magnitude. ``NaN`` if either input is
    non-finite or the drawdown is zero.
    """
    if not (np.isfinite(annual_ret) and np.isfinite(max_dd)) or max_dd <= 0:
        return float("nan")
    return float(annual_ret / max_dd)


def information_ratio(
    port_ret: Sequence[float] | np.ndarray,
    bench_ret: Sequence[float] | np.ndarray | None,
    ppy: int = _PPY,
) -> float:
    """Annualised information ratio: mean active return over its std (4.2).

    Active return is ``port_ret - bench_ret`` over their jointly-finite entries;
    annualised by ``sqrt(ppy)``. ``NaN`` if no benchmark, fewer than two paired
    finite points, or zero active-return dispersion.
    """
    if bench_ret is None:
        return float("nan")
    p = np.asarray(port_ret, dtype=np.float64).reshape(-1)
    b = np.asarray(bench_ret, dtype=np.float64).reshape(-1)
    n = min(p.size, b.size)
    if n < 2:
        return float("nan")
    p, b = p[:n], b[:n]
    ok = np.isfinite(p) & np.isfinite(b)
    active = (p - b)[ok]
    if active.size < 2:
        return float("nan")
    sd = active.std(ddof=1)
    if _degenerate_std(sd, active.mean()):
        return float("nan")
    return float(active.mean() / sd * np.sqrt(ppy))


def max_drawdown(
    nav: Sequence[float] | np.ndarray,
) -> tuple[float, int, int, int | None]:
    """Maximum peak-to-trough drawdown and its key indices (4.2).

    Returns ``(mdd, peak_idx, trough_idx, recovery_idx)`` where ``mdd`` is the
    positive magnitude ``max(1 - NAV_t / running_peak)``; ``peak_idx`` is the
    running-peak position that preceded the trough; ``trough_idx`` is the trough;
    and ``recovery_idx`` is the first index after the trough where NAV regains the
    peak, or ``None`` if never recovered. Indices are into the **original** array
    (non-finite NAVs are forward-filled with the running peak so they neither set
    nor break a drawdown). ``(nan, -1, -1, None)`` if no positive drawdown exists.
    """
    v = _as_nav(nav)
    if v.size == 0:
        return float("nan"), -1, -1, None
    # Forward-fill non-finite NAVs so they do not register as drawdowns or peaks.
    filled = v.copy()
    last = np.nan
    for i in range(filled.size):
        if np.isfinite(filled[i]):
            last = filled[i]
        elif np.isfinite(last):
            filled[i] = last
    running_peak = np.maximum.accumulate(np.where(np.isfinite(filled), filled, -np.inf))
    with np.errstate(invalid="ignore", divide="ignore"):
        dd = 1.0 - filled / running_peak  # 0 at/above peak, >0 below
    dd = np.where(np.isfinite(dd), dd, 0.0)
    trough_idx = int(np.argmax(dd))
    mdd = float(dd[trough_idx])
    if not np.isfinite(mdd) or mdd <= 0:
        return float("nan"), -1, -1, None
    peak_val = running_peak[trough_idx]
    # Peak index: last position at/before the trough that attained the running peak.
    pre = filled[: trough_idx + 1]
    peak_idx = int(np.flatnonzero(np.isfinite(pre) & (pre >= peak_val))[-1])
    # Recovery: first index after the trough that regains the prior peak value.
    recovery_idx: int | None = None
    post = filled[trough_idx + 1 :]
    rec = np.flatnonzero(np.isfinite(post) & (post >= peak_val))
    if rec.size:
        recovery_idx = int(rec[0] + trough_idx + 1)
    return mdd, peak_idx, trough_idx, recovery_idx


def drawdown_duration(nav: Sequence[float] | np.ndarray) -> int:
    """Longest run of consecutive periods strictly below the prior peak (4.2).

    Measures recovery speed; ``0`` if NAV never falls below a prior peak.
    """
    v = _as_nav(nav)
    if v.size == 0:
        return 0
    filled = v.copy()
    last = np.nan
    for i in range(filled.size):
        if np.isfinite(filled[i]):
            last = filled[i]
        elif np.isfinite(last):
            filled[i] = last
    peak = np.maximum.accumulate(np.where(np.isfinite(filled), filled, -np.inf))
    below = np.isfinite(filled) & (filled < peak)
    best = run = 0
    for b in below:
        run = run + 1 if b else 0
        best = max(best, run)
    return int(best)


def tracking_error(
    port_ret: Sequence[float] | np.ndarray,
    bench_ret: Sequence[float] | np.ndarray | None,
    ppy: int = _PPY,
) -> float:
    """Annualised tracking error: ``std(R_p - R_bench) * sqrt(ppy)`` (4.2).

    Computed over jointly-finite paired returns (``ddof=1``). ``NaN`` if no
    benchmark or fewer than two paired finite points.
    """
    if bench_ret is None:
        return float("nan")
    p = np.asarray(port_ret, dtype=np.float64).reshape(-1)
    b = np.asarray(bench_ret, dtype=np.float64).reshape(-1)
    n = min(p.size, b.size)
    if n < 2:
        return float("nan")
    p, b = p[:n], b[:n]
    ok = np.isfinite(p) & np.isfinite(b)
    active = (p - b)[ok]
    if active.size < 2:
        return float("nan")
    return float(active.std(ddof=1) * np.sqrt(ppy))


def beta(
    port_ret: Sequence[float] | np.ndarray,
    bench_ret: Sequence[float] | np.ndarray | None,
) -> float:
    """Market beta ``Cov(R_p, R_m) / Var(R_m)`` over paired returns (4.2).

    ``NaN`` if no benchmark, fewer than two paired finite points, or the benchmark
    has zero variance.
    """
    if bench_ret is None:
        return float("nan")
    p = np.asarray(port_ret, dtype=np.float64).reshape(-1)
    b = np.asarray(bench_ret, dtype=np.float64).reshape(-1)
    n = min(p.size, b.size)
    if n < 2:
        return float("nan")
    p, b = p[:n], b[:n]
    ok = np.isfinite(p) & np.isfinite(b)
    p, b = p[ok], b[ok]
    if p.size < 2:
        return float("nan")
    var_m = b.var(ddof=1)
    if _degenerate_std(np.sqrt(var_m) if var_m >= 0 else -1.0, b.mean()):
        return float("nan")
    cov = np.cov(p, b, ddof=1)[0, 1]
    return float(cov / var_m)


def alpha_capm(
    port_ret: Sequence[float] | np.ndarray,
    bench_ret: Sequence[float] | np.ndarray | None,
    rf_annual: float = 0.0,
    ppy: int = _PPY,
) -> float:
    """Annualised CAPM alpha ``R_p - R_f - beta*(R_m - R_f)`` (4.2).

    Per-period means are differenced against the de-annualised ``rf`` and the
    estimated ``beta``, then annualised by ``* ppy``. ``NaN`` if beta is undefined
    or fewer than two paired finite points.
    """
    if bench_ret is None:
        return float("nan")
    b_val = beta(port_ret, bench_ret)
    if not np.isfinite(b_val):
        return float("nan")
    p = np.asarray(port_ret, dtype=np.float64).reshape(-1)
    b = np.asarray(bench_ret, dtype=np.float64).reshape(-1)
    n = min(p.size, b.size)
    if n < 2:
        return float("nan")
    p, b = p[:n], b[:n]
    ok = np.isfinite(p) & np.isfinite(b)
    p, b = p[ok], b[ok]
    if p.size < 2:
        return float("nan")
    rf_daily = rf_annual / ppy
    daily_alpha = (p.mean() - rf_daily) - b_val * (b.mean() - rf_daily)
    return float(daily_alpha * ppy)


# ---------------------------------------------------------------------------
# 4.3  Turnover and cost metrics
# ---------------------------------------------------------------------------
def annual_turnover(one_way_per_rebal: float, rebals_per_year: float) -> float:
    """Annualised one-way turnover ``one_way_per_rebal * rebals_per_year`` (4.3).

    ``one_way_per_rebal`` is the average per-rebalance one-way turnover
    (``sum|w_t - w_{t-1}| / 2``). ``NaN`` if either input is non-finite.
    """
    if not (np.isfinite(one_way_per_rebal) and np.isfinite(rebals_per_year)):
        return float("nan")
    return float(one_way_per_rebal * rebals_per_year)


def avg_holding_days(annual_one_way: float, ppy: int = _PPY) -> float:
    """Implied average holding period in **trading days** (4.3).

    ``1 / annual_one_way`` is the holding period in years; multiplying by ``ppy``
    expresses it in trading days. ``NaN`` if turnover is non-finite or
    non-positive (a never-traded portfolio has no finite implied turnover).
    """
    if not np.isfinite(annual_one_way) or annual_one_way <= 0:
        return float("nan")
    return float(ppy / annual_one_way)


def cost_drag(gross_return: float, net_return: float) -> float:
    """Total performance lost to transaction costs ``gross - net`` (4.3).

    ``NaN`` if either input is non-finite.
    """
    if not (np.isfinite(gross_return) and np.isfinite(net_return)):
        return float("nan")
    return float(gross_return - net_return)


# ---------------------------------------------------------------------------
# Monthly returns (heatmap display)
# ---------------------------------------------------------------------------
def _to_pydate(d: Any) -> dt.date | None:
    """Best-effort coercion of one date-like value to a ``datetime.date``.

    Accepts ``datetime.date``/``datetime``, numpy ``datetime64``, ``YYYY-MM-DD``
    (or longer ISO) strings, and pandas ``Timestamp``. Returns ``None`` when the
    value cannot be parsed (the caller drops it).
    """
    if d is None:
        return None
    if isinstance(d, dt.datetime):
        return d.date()
    if isinstance(d, dt.date):
        return d
    if isinstance(d, np.datetime64):
        return np.datetime64(d, "D").astype("datetime64[D]").astype(dt.date)
    if hasattr(d, "to_pydatetime"):  # pandas Timestamp
        try:
            return d.to_pydatetime().date()
        except Exception:  # pragma: no cover - defensive
            return None
    s = str(d)
    try:
        return dt.date.fromisoformat(s[:10])
    except ValueError:
        return None


def monthly_returns(
    nav: Sequence[float] | np.ndarray, dates: Sequence[Any]
) -> dict[str, float]:
    """Calendar-month returns keyed ``'YYYY-MM' -> return`` for the heatmap (4.x).

    Each month's return is ``NAV_last(month) / NAV_last(prev period) - 1``, i.e.
    the compounded daily return within the month, chained off the last NAV of the
    preceding calendar month (or the period's opening NAV for the first month).
    NaN NAVs are skipped; a month with no finite NAV is omitted. ``dates`` aligns
    1:1 with ``nav``; unparseable dates drop their NAV from the grouping.
    """
    v = _as_nav(nav)
    n = min(v.size, len(dates))
    if n == 0:
        return {}
    # Build (month-key, nav) keeping only finite NAVs with parseable dates.
    keys: list[str] = []
    vals: list[float] = []
    for i in range(n):
        if not np.isfinite(v[i]):
            continue
        pd = _to_pydate(dates[i])
        if pd is None:
            continue
        keys.append(f"{pd.year:04d}-{pd.month:02d}")
        vals.append(float(v[i]))
    if not vals:
        return {}
    out: dict[str, float] = {}
    prev_close = vals[0]  # opening reference for the very first month
    cur_key = keys[0]
    cur_last = vals[0]
    for k, val in zip(keys[1:], vals[1:]):
        if k != cur_key:
            out[cur_key] = cur_last / prev_close - 1.0 if prev_close > 0 else float("nan")
            prev_close = cur_last
            cur_key = k
        cur_last = val
    out[cur_key] = cur_last / prev_close - 1.0 if prev_close > 0 else float("nan")
    return out


# ---------------------------------------------------------------------------
# Headline reducer
# ---------------------------------------------------------------------------
def compute_metrics(
    nav: Sequence[float] | np.ndarray,
    dates: Sequence[Any],
    bench_nav: Sequence[float] | np.ndarray | None = None,
    config: Any | None = None,
    *,
    gross_nav: Sequence[float] | np.ndarray | None = None,
    one_way_per_rebal: float | None = None,
    n_rebalances: int | None = None,
) -> dict[str, Any]:
    """Reduce a NAV series to every section-4 metric as a flat, NaN-safe dict.

    Keys mirror :class:`~assay.portfolio.report.PortfolioReport` fields:
    ``total_return``, ``annual_return``, ``gross_return``, ``excess_return``,
    ``sharpe``, ``sortino``, ``calmar``, ``information_ratio``, ``max_drawdown``
    (+ ``max_drawdown_peak_idx``/``_trough_idx``/``_recovery_idx`` and
    ``drawdown_recovery_days``/``drawdown_duration``), ``beta``, ``alpha_capm``,
    ``tracking_error``, ``annual_turnover``, ``cost_drag``, ``avg_holding_days``,
    plus ``monthly_returns`` and ``log_return``.

    The risk-free rate is read from ``config.risk_free_rate`` when a config is
    given (else 0.0). Turnover/cost inputs are optional: pass ``gross_nav`` to get
    ``cost_drag``/``gross_return``; pass ``one_way_per_rebal`` and
    ``n_rebalances`` (with ``dates`` to infer the year-fraction) to get
    ``annual_turnover``/``avg_holding_days``. Anything not derivable is ``NaN``.
    """
    rf = float(getattr(config, "risk_free_rate", 0.0) or 0.0) if config is not None else 0.0
    v = _as_nav(nav)
    port_ret = returns_from_nav(v)
    bench_ret = returns_from_nav(bench_nav) if bench_nav is not None else None

    ann_ret = annualized_return(v)
    mdd, peak_i, trough_i, rec_i = max_drawdown(v)

    out: dict[str, Any] = {
        # 4.1
        "total_return": total_return(v),
        "annual_return": ann_ret,
        "log_return": log_return(v),
        "gross_return": (total_return(gross_nav) if gross_nav is not None else float("nan")),
        "excess_return": excess_return(v, bench_nav),
        # 4.2
        "sharpe": sharpe(port_ret, rf),
        "sortino": sortino(port_ret, rf),
        "calmar": calmar(ann_ret, mdd),
        "information_ratio": information_ratio(port_ret, bench_ret),
        "max_drawdown": mdd,
        "max_drawdown_peak_idx": peak_i,
        "max_drawdown_trough_idx": trough_i,
        "max_drawdown_recovery_idx": rec_i,
        "drawdown_recovery_days": (None if rec_i is None or trough_i < 0 else int(rec_i - trough_i)),
        "drawdown_duration": drawdown_duration(v),
        "beta": beta(port_ret, bench_ret),
        "alpha_capm": alpha_capm(port_ret, bench_ret, rf),
        "tracking_error": tracking_error(port_ret, bench_ret),
        # 4.3
        "annual_turnover": float("nan"),
        "cost_drag": float("nan"),
        "avg_holding_days": float("nan"),
        # display
        "monthly_returns": monthly_returns(v, dates),
    }

    # cost drag from a parallel gross NAV (4.3)
    if gross_nav is not None:
        out["cost_drag"] = cost_drag(out["gross_return"], out["total_return"])

    # turnover / holding period (4.3) — needs the per-rebalance one-way turnover
    if one_way_per_rebal is not None and n_rebalances is not None and n_rebalances > 0:
        # rebalances per year from the observed period length (finite-NAV span).
        fin = np.flatnonzero(np.isfinite(v))
        years = (fin[-1] - fin[0]) / _PPY if fin.size >= 2 else float("nan")
        rebals_per_year = n_rebalances / years if years and years > 0 else float("nan")
        at = annual_turnover(one_way_per_rebal, rebals_per_year)
        out["annual_turnover"] = at
        out["avg_holding_days"] = avg_holding_days(at)

    return out
