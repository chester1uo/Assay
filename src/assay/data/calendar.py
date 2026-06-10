"""Trading-session calendar helpers (NYSE/Nasdaq = ``XNYS``).

Used to enumerate the trading days for which a flat file should exist, so the
price loader never wastes requests on weekends/holidays.
"""

from __future__ import annotations

import datetime as dt
import functools

import exchange_calendars as xc


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
