"""Intraday session-structure calendar helpers (XNYS): bar grid, half-days, DST."""

from __future__ import annotations

import datetime as dt

import numpy as np
import pytest

from assay.data.calendar import (
    bars_per_session,
    session_bars,
    session_count,
    session_ids,
    session_open_close,
    session_type,
)
from assay.data.frequency import DAILY, MINUTE_1, MINUTE_5, MINUTE_15

# Known sessions: a normal summer (EDT) day, a winter (EST) day, and the
# half-day before Independence Day 2024 (early 13:00 ET close).
NORMAL = dt.date(2024, 6, 26)
WINTER = dt.date(2024, 1, 3)
HALF_DAY = dt.date(2024, 7, 3)


def test_session_open_close_et():
    o, c = session_open_close(NORMAL)
    assert (o.hour, o.minute) == (9, 30)
    assert (c.hour, c.minute) == (16, 0)
    # half-day closes at 13:00 ET
    _, hc = session_open_close(HALF_DAY)
    assert (hc.hour, hc.minute) == (13, 0)


def test_open_is_0930_et_across_dst():
    # ET wall-clock open is 09:30 in both EST and EDT — bar counts are DST-invariant.
    for day in (NORMAL, WINTER):
        o, _ = session_open_close(day)
        assert (o.hour, o.minute) == (9, 30)
        assert bars_per_session(day, freq=MINUTE_1) == 390


@pytest.mark.parametrize("freq, full, half", [(MINUTE_1, 390, 210), (MINUTE_5, 78, 42), (MINUTE_15, 26, 14)])
def test_bars_per_session(freq, full, half):
    assert bars_per_session(NORMAL, freq=freq) == full
    assert bars_per_session(HALF_DAY, freq=freq) == half


def test_bars_per_session_daily_is_one():
    assert bars_per_session(NORMAL, freq=DAILY) == 1


def test_session_bars_grid():
    bars = session_bars(NORMAL, freq=MINUTE_1)
    assert len(bars) == 390
    assert (bars[0].hour, bars[0].minute) == (9, 30)       # first bar starts at the open
    assert (bars[-1].hour, bars[-1].minute) == (15, 59)    # last bar starts one step before close
    # strictly increasing by one minute
    deltas = {(b2 - b1) for b1, b2 in zip(bars, bars[1:])}
    assert deltas == {dt.timedelta(minutes=1)}


def test_session_bars_requires_intraday():
    with pytest.raises(ValueError):
        session_bars(NORMAL, freq=DAILY)


def test_session_type_classifies():
    assert session_type(dt.datetime(2024, 6, 26, 8, 0), NORMAL) == 1     # pre
    assert session_type(dt.datetime(2024, 6, 26, 9, 30), NORMAL) == 0    # RTH (open inclusive)
    assert session_type(dt.datetime(2024, 6, 26, 12, 0), NORMAL) == 0    # RTH
    assert session_type(dt.datetime(2024, 6, 26, 16, 0), NORMAL) == 2    # post (close exclusive)
    assert session_type(dt.datetime(2024, 6, 26, 18, 30), NORMAL) == 2   # post


def test_session_ids_from_datetime64_array():
    arr = np.array(
        ["2024-06-26T09:30", "2024-06-26T16:00", "2024-07-03T10:00"], dtype="datetime64[m]"
    )
    out = session_ids(arr)
    assert out.dtype == np.int32
    assert out.tolist() == [20240626, 20240626, 20240703]


def test_session_ids_from_object_sequence():
    seq = [dt.datetime(2024, 1, 3, 9, 30), dt.date(2024, 12, 31)]
    assert session_ids(seq).tolist() == [20240103, 20241231]


def test_session_count():
    # Mon 2024-06-24 .. Fri 2024-06-28 -> 5 trading sessions
    assert session_count(dt.date(2024, 6, 24), dt.date(2024, 6, 28)) == 5
    # range spanning the July-4 holiday excludes the holiday
    assert session_count(dt.date(2024, 7, 1), dt.date(2024, 7, 5)) == 4  # 7/4 closed


# -- review-driven hardening --------------------------------------------------
@pytest.mark.parametrize("freq", [MINUTE_1, MINUTE_5, MINUTE_15])
@pytest.mark.parametrize("day", [NORMAL, WINTER, HALF_DAY])
def test_bars_per_session_matches_session_bars(freq, day):
    # single source of truth: the count never diverges from the actual grid
    assert bars_per_session(day, freq=freq) == len(session_bars(day, freq=freq))


def test_session_type_half_day():
    # half-day closes 13:00 ET: 12:59 is RTH, 13:00 is post
    assert session_type(dt.datetime(2024, 7, 3, 12, 59), HALF_DAY) == 0
    assert session_type(dt.datetime(2024, 7, 3, 13, 0), HALF_DAY) == 2


def test_session_type_converts_tz_aware_to_et():
    utc = dt.timezone.utc
    # 13:00 UTC = 08:00 ET (EST) -> pre-market; 20:00 UTC = 15:00 ET -> RTH
    assert session_type(dt.datetime(2023, 11, 27, 13, 0, tzinfo=utc), dt.date(2023, 11, 27)) == 1
    assert session_type(dt.datetime(2023, 11, 27, 20, 0, tzinfo=utc), dt.date(2023, 11, 27)) == 0


def test_session_ids_datetime64_ns_and_year_boundary():
    arr = np.array(
        ["2024-12-31T15:59", "2025-01-02T09:30"], dtype="datetime64[ns]"
    )
    assert session_ids(arr).tolist() == [20241231, 20250102]


def test_session_ids_rejects_nat():
    arr = np.array(["2024-06-26", "NaT"], dtype="datetime64[D]")
    with pytest.raises(ValueError, match="NaT"):
        session_ids(arr)


def test_session_bars_include_extended_grid():
    bars = session_bars(NORMAL, freq=MINUTE_1, include_extended=True)
    assert len(bars) == (20 - 4) * 60                       # 04:00–20:00 ET = 960 1m bars
    assert (bars[0].hour, bars[0].minute) == (4, 0)
    assert (bars[-1].hour, bars[-1].minute) == (19, 59)
