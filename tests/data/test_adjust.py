"""Offline tests for corporate-action price adjustment."""

import datetime as dt

import polars as pl
import pytest

from assay.data.store.adjust import forward_adjust


def _prices(rows):
    return pl.DataFrame(
        rows, schema={"date": pl.Date, "symbol": pl.Utf8, "close": pl.Float64, "volume": pl.Float64}
    )


def _events(rows):
    return pl.DataFrame(
        rows,
        schema={
            "symbol": pl.Utf8,
            "ex_date": pl.Date,
            "split_ratio": pl.Float64,
            "dividend_cash": pl.Float64,
        },
    )


def test_split_only_forward_factor_aapl_like():
    # AAPL: 7:1 split on 2014-06-09, 4:1 split on 2020-08-31.
    prices = _prices([
        {"date": dt.date(2013, 1, 2), "symbol": "AAPL", "close": 100.0, "volume": 280.0},
        {"date": dt.date(2015, 1, 2), "symbol": "AAPL", "close": 100.0, "volume": 280.0},
        {"date": dt.date(2021, 1, 4), "symbol": "AAPL", "close": 100.0, "volume": 280.0},
    ])
    events = _events([
        {"symbol": "AAPL", "ex_date": dt.date(2014, 6, 9), "split_ratio": 7.0, "dividend_cash": 0.0},
        {"symbol": "AAPL", "ex_date": dt.date(2020, 8, 31), "split_ratio": 4.0, "dividend_cash": 0.0},
    ])
    out = forward_adjust(prices, events, mode="split").sort("date")
    close = out["close"].to_list()
    # before both splits: /28 ; between: /4 ; after both: unchanged
    assert close[0] == pytest.approx(100.0 / 28.0, rel=1e-6)   # 1/28 == provider 0.035714
    assert close[1] == pytest.approx(25.0, rel=1e-9)
    assert close[2] == pytest.approx(100.0, rel=1e-9)
    # cross-check the split-only factor against MASSIVE historical_adjustment_factor
    assert (close[0] / 100.0) == pytest.approx(0.035714, abs=1e-6)
    # volume scales inversely (more shares after splits)
    vol = out["volume"].to_list()
    assert vol[0] == pytest.approx(280.0 * 28.0, rel=1e-6)
    assert vol[1] == pytest.approx(280.0 * 4.0, rel=1e-9)
    assert vol[2] == pytest.approx(280.0, rel=1e-9)


def test_ex_date_bar_not_divided_by_its_own_split():
    # the split bar itself already reflects the new share count.
    prices = _prices([
        {"date": dt.date(2020, 8, 28), "symbol": "AAPL", "close": 500.0, "volume": 10.0},
        {"date": dt.date(2020, 8, 31), "symbol": "AAPL", "close": 125.0, "volume": 40.0},
    ])
    events = _events([
        {"symbol": "AAPL", "ex_date": dt.date(2020, 8, 31), "split_ratio": 4.0, "dividend_cash": 0.0},
    ])
    out = forward_adjust(prices, events, mode="split").sort("date")
    close = out["close"].to_list()
    assert close[0] == pytest.approx(125.0, rel=1e-9)  # pre-split 500 -> 125
    assert close[1] == pytest.approx(125.0, rel=1e-9)  # ex-date bar unchanged


def test_reverse_split():
    # 1:10 reverse split (split_from=10, split_to=1 -> ratio 0.1): prices before scale up 10x.
    prices = _prices([
        {"date": dt.date(2022, 1, 3), "symbol": "ZZZ", "close": 1.0, "volume": 1000.0},
        {"date": dt.date(2022, 6, 1), "symbol": "ZZZ", "close": 10.0, "volume": 100.0},
    ])
    events = _events([
        {"symbol": "ZZZ", "ex_date": dt.date(2022, 6, 1), "split_ratio": 0.1, "dividend_cash": 0.0},
    ])
    out = forward_adjust(prices, events, mode="split").sort("date")
    assert out["close"].to_list()[0] == pytest.approx(10.0, rel=1e-9)
    assert out["volume"].to_list()[0] == pytest.approx(100.0, rel=1e-9)


def test_total_return_dividend_adjustment():
    prices = _prices([
        {"date": dt.date(2021, 2, 4), "symbol": "AAA", "close": 100.0, "volume": 50.0},
        {"date": dt.date(2021, 2, 5), "symbol": "AAA", "close": 99.0, "volume": 50.0},
        {"date": dt.date(2021, 2, 8), "symbol": "AAA", "close": 99.5, "volume": 50.0},
    ])
    events = _events([
        {"symbol": "AAA", "ex_date": dt.date(2021, 2, 5), "split_ratio": 1.0, "dividend_cash": 1.0},
    ])
    total = forward_adjust(prices, events, mode="total").sort("date")
    # day before ex gets * (1 - 1/100) = 0.99 ; ex-date & later unchanged
    assert total["close"].to_list()[0] == pytest.approx(99.0, rel=1e-9)
    assert total["close"].to_list()[1] == pytest.approx(99.0, rel=1e-9)
    # volume is unaffected by dividends
    assert total["volume"].to_list()[0] == pytest.approx(50.0, rel=1e-9)
    # split mode ignores dividends
    split = forward_adjust(prices, events, mode="split").sort("date")
    assert split["close"].to_list()[0] == pytest.approx(100.0, rel=1e-9)


def test_mode_none_is_identity():
    prices = _prices([
        {"date": dt.date(2021, 2, 4), "symbol": "AAA", "close": 100.0, "volume": 50.0},
    ])
    events = _events([
        {"symbol": "AAA", "ex_date": dt.date(2099, 1, 1), "split_ratio": 4.0, "dividend_cash": 0.0},
    ])
    out = forward_adjust(prices, events, mode="none")
    assert out["close"].to_list() == [100.0]


def test_nan_close_prev_does_not_poison_history():
    # A missing raw close on the bar before an ex-date must NOT turn every earlier
    # adjusted bar into NaN (regression for the `NaN <= 0 is False` guard bug).
    prices = _prices([
        {"date": dt.date(2021, 2, 1), "symbol": "AAA", "close": 100.0, "volume": 10.0},
        {"date": dt.date(2021, 2, 4), "symbol": "AAA", "close": None, "volume": 10.0},  # data hole
        {"date": dt.date(2021, 2, 5), "symbol": "AAA", "close": 99.0, "volume": 10.0},  # ex-date
    ])
    events = _events([
        {"symbol": "AAA", "ex_date": dt.date(2021, 2, 5), "split_ratio": 1.0, "dividend_cash": 1.0},
    ])
    out = forward_adjust(prices, events, mode="total").sort("date")
    closes = out["close"].to_list()
    # the dividend is skipped (no valid prior close); the earlier finite bar survives
    assert closes[0] == pytest.approx(100.0, rel=1e-9)
    assert closes[2] == pytest.approx(99.0, rel=1e-9)


def test_dividend_skipped_when_prior_bar_not_adjacent():
    # If the nearest earlier bar is far from the ex-date (lead-in gap / data hole),
    # the dividend must not be applied against that distant bar.
    prices = _prices([
        {"date": dt.date(2021, 1, 1), "symbol": "AAA", "close": 100.0, "volume": 10.0},
        {"date": dt.date(2021, 2, 15), "symbol": "AAA", "close": 99.0, "volume": 10.0},  # ex-date
    ])
    events = _events([
        {"symbol": "AAA", "ex_date": dt.date(2021, 2, 15), "split_ratio": 1.0, "dividend_cash": 1.0},
    ])
    out = forward_adjust(prices, events, mode="total").sort("date")
    assert out["close"].to_list()[0] == pytest.approx(100.0, rel=1e-9)  # not 99.0


def test_empty_events_unchanged():
    prices = _prices([
        {"date": dt.date(2021, 2, 4), "symbol": "AAA", "close": 100.0, "volume": 50.0},
    ])
    events = _events([])
    out = forward_adjust(prices, events, mode="split")
    assert out["close"].to_list() == [100.0]
    assert out["volume"].to_list() == [50.0]
