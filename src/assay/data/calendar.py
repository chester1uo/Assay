"""Trading-session calendar helpers (NYSE/Nasdaq = ``XNYS``).

Originally used only to enumerate the trading days for which a flat file should
exist (so the daily loader never wastes work on weekends/holidays). For
minute-level backtesting (``docs/design/minute-backtesting.md``) it also resolves
intraday session structure — open/close instants, the per-session bar grid,
half-day/DST-correct bar counts, the session-id segment vector, and bar
classification (pre/RTH/post). All of it wraps ``exchange_calendars`` (XNYS),
which is already DST- and half-day-aware.
"""

from __future__ import annotations

import datetime as dt
import functools
from zoneinfo import ZoneInfo

import exchange_calendars as xc
import numpy as np

from assay.data.frequency import MINUTE_1, Frequency

_ET = "America/New_York"
_ET_ZONE = ZoneInfo(_ET)
# US extended-hours convention used by the MASSIVE feed (ET wall-clock). RTH
# boundaries always come from the calendar; these only bound the pre/post grid
# for include_extended sizing (intraday-research path), never RTH correctness.
_EXT_OPEN = dt.time(4, 0)
_EXT_CLOSE = dt.time(20, 0)


@functools.lru_cache(maxsize=4)
def _calendar(name: str = "XNYS"):
    return xc.get_calendar(name)


def trading_days(start: dt.date, end: dt.date, calendar: str = "XNYS") -> list[dt.date]:
    """Return the list of trading sessions in ``[start, end]`` inclusive."""
    cal = _calendar(calendar)
    sessions = cal.sessions_in_range(str(start), str(end))
    return [s.date() for s in sessions]


def is_trading_day(day: dt.date, calendar: str = "XNYS") -> bool:
    return _calendar(calendar).is_session(str(day))


def session_count(start: dt.date, end: dt.date, calendar: str = "XNYS") -> int:
    """Number of distinct trading sessions in ``[start, end]`` inclusive."""
    return len(trading_days(start, end, calendar))


# -- intraday session structure ----------------------------------------------
def session_open_close(
    day: dt.date, calendar: str = "XNYS"
) -> tuple[dt.datetime, dt.datetime]:
    """Regular-hours open/close for ``day`` as tz-aware **ET** datetimes.

    Half-days resolve to a 13:00 ET close automatically (the calendar knows).
    Raises if ``day`` is not a trading session.
    """
    cal = _calendar(calendar)
    open_utc = cal.session_open(str(day))
    close_utc = cal.session_close(str(day))
    return (
        open_utc.tz_convert(_ET).to_pydatetime(),
        close_utc.tz_convert(_ET).to_pydatetime(),
    )


def bars_per_session(
    day: dt.date, *, freq: Frequency = MINUTE_1, include_extended: bool = False,
    calendar: str = "XNYS",
) -> int:
    """Count of bars in ``day``'s session at ``freq`` (RTH by default).

    Derived from the calendar's actual open/close, so a half-day yields ~210
    1-minute bars and a full day 390 — never a nominal constant. Daily -> 1.

    Defined as ``len(session_bars(...))`` so the count is, by construction, the
    same grid the engine iterates — it cannot drift from :func:`session_bars`
    even for a future freq whose step does not evenly divide the session span.
    """
    if not freq.is_intraday:
        return 1
    return len(session_bars(day, freq=freq, include_extended=include_extended, calendar=calendar))


def session_bars(
    day: dt.date, *, freq: Frequency = MINUTE_1, include_extended: bool = False,
    calendar: str = "XNYS",
) -> list[dt.datetime]:
    """Ordered bar-start instants (tz-aware ET) for ``day``'s session at ``freq``.

    ``window_start`` semantics: each bar is labelled by its start, so the final
    bar starts one ``step`` before the close. RTH by default; ``include_extended``
    uses the fixed 04:00–20:00 ET window (half-day extended-close nuance is
    deferred — intraday-research path only).
    """
    if not freq.is_intraday:
        raise ValueError("session_bars requires an intraday frequency")
    open_et, close_et = session_open_close(day, calendar)
    if include_extended:
        start = dt.datetime.combine(day, _EXT_OPEN, tzinfo=open_et.tzinfo)
        end = dt.datetime.combine(day, _EXT_CLOSE, tzinfo=close_et.tzinfo)
    else:
        start, end = open_et, close_et
    step = dt.timedelta(seconds=freq.step_seconds)
    out: list[dt.datetime] = []
    t = start
    while t < end:
        out.append(t)
        t += step
    return out


def session_type(ts_et: dt.datetime, day: dt.date, calendar: str = "XNYS") -> int:
    """Classify a bar timestamp: 0=RTH, 1=pre-market, 2=post-market.

    A **tz-aware** ``ts_et`` is converted to ET first (so a UTC instant is
    classified correctly); a **naive** ``ts_et`` is taken as ET wall-clock.
    Before the open is pre, at/after the close is post, otherwise regular hours.
    """
    open_et, close_et = session_open_close(day, calendar)
    if ts_et.tzinfo is not None:
        ts_et = ts_et.astimezone(_ET_ZONE)
    t = ts_et.replace(tzinfo=None)
    if t < open_et.replace(tzinfo=None):
        return 1
    if t >= close_et.replace(tzinfo=None):
        return 2
    return 0


def session_ids(time_index_et) -> np.ndarray:
    """Map an ET time index to an ``int32`` ``YYYYMMDD`` session-id vector.

    This is the cheap ``(T,)`` segment vector the intraday engine uses to keep
    time-series windows within a session. Accepts a numpy ``datetime64`` array
    (any resolution) or any sequence of ``date``/``datetime`` objects.

    .. important::
       The input MUST be **ET wall-clock** (tz-naive). The id is the local
       calendar date, so feeding a UTC ``datetime64`` (as the minute store keeps
       ``ts``) would land evening ET bars in the wrong session — callers must
       convert UTC→ET *before* calling (see ``DataStore`` minute read path).
       ``NaT`` is rejected loudly rather than mapped to a bogus id.
    """
    arr = np.asarray(time_index_et)
    if np.issubdtype(arr.dtype, np.datetime64):
        if np.isnat(arr).any():
            raise ValueError("session_ids: input contains NaT")
        days = arr.astype("datetime64[D]")
        years = days.astype("datetime64[Y]").astype(int) + 1970
        months = days.astype("datetime64[M]").astype(int) % 12 + 1
        day_of_month = (days - days.astype("datetime64[M]")).astype("timedelta64[D]").astype(int) + 1
        return (years * 10000 + months * 100 + day_of_month).astype(np.int32)
    return np.array(
        [t.year * 10000 + t.month * 100 + t.day for t in time_index_et], dtype=np.int32
    )
