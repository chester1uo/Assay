"""Frequency value object: constants, derived properties, and parsing."""

from __future__ import annotations

import pytest

from assay.data.frequency import (
    DAILY,
    MINUTE_1,
    MINUTE_5,
    MINUTE_15,
    Frequency,
    parse_frequency,
)


def test_daily_constants():
    assert DAILY.is_intraday is False
    assert DAILY.time_col == "date"
    assert DAILY.partition_grain == "month"
    assert DAILY.step_seconds == 0
    assert DAILY.nominal_bars_per_day == 1
    assert DAILY.polars_every() == "1d"


@pytest.mark.parametrize(
    "freq, step, bars",
    [(MINUTE_1, 60, 390), (MINUTE_5, 300, 78), (MINUTE_15, 900, 26)],
)
def test_minute_constants(freq, step, bars):
    assert freq.is_intraday is True
    assert freq.time_col == "ts"
    assert freq.partition_grain == "day"
    assert freq.step_seconds == step
    assert freq.nominal_bars_per_day == bars       # 390 / multiple, sizing hint
    assert freq.polars_every() == f"{freq.multiple}m"


def test_frozen_and_hashable():
    assert hash(MINUTE_1) == hash(MINUTE_1)
    with pytest.raises(Exception):
        MINUTE_1.multiple = 2  # frozen dataclass


@pytest.mark.parametrize(
    "alias, expected",
    [
        (None, DAILY), ("1d", DAILY), ("daily", DAILY), ("DAY", DAILY), (" Daily ", DAILY),
        ("1m", MINUTE_1), ("1min", MINUTE_1), ("minute", MINUTE_1),
        ("5m", MINUTE_5), ("5min", MINUTE_5),
        ("15m", MINUTE_15), ("15MIN", MINUTE_15),
    ],
)
def test_parse_frequency_aliases(alias, expected):
    assert parse_frequency(alias) is expected


def test_parse_frequency_passthrough():
    assert parse_frequency(MINUTE_5) is MINUTE_5


def test_parse_frequency_rejects_unknown():
    for bad in ("2m", "1h", "weekly", "", "tick", 5):
        with pytest.raises(ValueError):
            parse_frequency(bad)
