"""Point-in-time trailing market-notional inputs for daily execution."""
from datetime import date, timedelta

import numpy as np


def trailing_notional(store, dates, symbols, *, as_of, window, volume_multiplier, calendar='XNYS'):
    """Mean raw close*volume for the preceding complete window, excluding trade day.

    Missing or nonpositive observations invalidate a window. The caller must block
    trading for such names, rather than silently disabling capacity and impact.
    """
    target_days = np.asarray(dates, dtype='datetime64[D]')
    if not len(target_days):
        return np.empty((0, len(symbols)))
    start = date.fromisoformat(str(target_days[0])) - timedelta(days=3 * window + 30)
    frame = store.get_panel(['close', 'volume'], list(map(str, symbols)), str(start),
                            str(target_days[-1]), str(as_of), adj='none')
    out = np.full((len(target_days), len(symbols)), np.nan)
    if frame.is_empty():
        return out
    days = frame['date'].to_numpy().astype('datetime64[D]')
    names = frame['symbol'].to_numpy().astype(str)
    prices = frame['close'].to_numpy().astype(float)
    volumes = frame['volume'].to_numpy().astype(float)
    ticker = np.asarray(symbols).astype(str)
    if len(ticker) == 0 or len(np.unique(ticker)) != len(ticker) or np.any(ticker[1:] < ticker[:-1]):
        raise ValueError('liquidity symbols must be nonempty, unique, and sorted')
    # Use the exchange calendar, not just observed bars: a day missing for the
    # entire universe must invalidate its window instead of disappearing.
    # Construct the requested range explicitly: the library's default calendar
    # is a moving window and excludes parts of Assay's historical data. Reading
    # sessions directly also permits start/end dates that fall on holidays.
    import exchange_calendars as xc
    end = date.fromisoformat(str(target_days[-1]))
    try:
        cal = xc.get_calendar(calendar, start=str(start), end=str(end))
    except ValueError as exc:
        raise ValueError(f'{calendar} calendar does not cover {start} through {end}: {exc}') from exc
    calendar_days = np.asarray(cal.sessions, dtype='datetime64[D]')
    all_days = np.unique(np.concatenate((calendar_days, days, target_days)))
    di = np.searchsorted(all_days, days)
    si = np.searchsorted(ticker, names)
    valid = (si < len(ticker))
    valid &= ticker[np.minimum(si, len(ticker)-1)] == names
    keys = di[valid] * len(ticker) + si[valid]
    if len(np.unique(keys)) != len(keys):
        raise ValueError('duplicate liquidity observations for date and symbol')
    notionals = np.full((len(all_days), len(ticker)), np.nan)
    values = prices * volumes * volume_multiplier
    values[~(np.isfinite(prices) & (prices > 0) & np.isfinite(volumes) & (volumes > 0))] = np.nan
    notionals[di[valid], si[valid]] = values[valid]
    good = np.isfinite(notionals) & (notionals > 0)
    totals = np.vstack([np.zeros((1, len(ticker))), np.cumsum(np.where(good, notionals, 0.0), axis=0)])
    counts = np.vstack([np.zeros((1, len(ticker)), dtype=np.int64), np.cumsum(good, axis=0)])
    for i, day in enumerate(target_days):
        end = np.searchsorted(all_days, day, side='left')
        if end < window:
            continue
        n = counts[end] - counts[end-window]
        out[i] = np.where(n == window, (totals[end] - totals[end-window]) / window, np.nan)
    return out
