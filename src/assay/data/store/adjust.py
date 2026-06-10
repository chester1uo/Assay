"""Corporate-action price adjustment (splits, reverse-splits, mergers, dividends).

Computes **forward** adjustment factors: prices are rescaled onto the basis of
the most recent date per symbol, so the latest bar equals its raw value and
history is scaled back. This is the convention factor research expects (recent
prices unchanged).

Conventions
-----------
* A share-change event (split / reverse-split / merger ratio) with forward ratio
  ``r = split_to / split_from`` divides every price strictly *before* its ex-date
  by ``r`` (the ex-date bar already reflects the new share count). Volume is
  multiplied by ``r`` over the same window.
* A cash dividend of ``D`` with ex-date ``e`` multiplies every price strictly
  before ``e`` by ``1 - D / close_prev``, where ``close_prev`` is the raw close
  on the trading day immediately preceding ``e``. Dividends do not affect volume.

Both ``D`` and ``close_prev`` are raw/contemporaneous, so the ratio is computed
in raw price space and composes correctly with splits.

The split-only cumulative factor at date ``d`` equals
``prod(1/r_i for ex_date_i > d)`` — verifiable against the provider's
``historical_adjustment_factor`` (kept in ``adj_events`` for reference).
"""

from __future__ import annotations

import bisect
import datetime as dt

import numpy as np
import polars as pl

_TOTAL_MODES = {"total", "forward"}
ADJ_MODES = {"none", "split", "total", "forward"}

_PRICE_COLS = ("open", "high", "low", "close")

# A dividend's prior-close lookup must land on the session immediately before the
# ex-date. If the nearest earlier in-array bar is more than this many calendar
# days back, it is a data hole (or outside the loaded lead-in) and the dividend
# is skipped rather than adjusted against a distant bar. Covers weekends + the
# longest routine US market closures.
_MAX_PRIOR_GAP_DAYS = 10


def _adjust_one_symbol(
    sym_prices: pl.DataFrame,
    splits: list[tuple[dt.date, float]],
    dividends: list[tuple[dt.date, float]],
    apply_dividends: bool,
) -> pl.DataFrame:
    dates: list[dt.date] = sym_prices["date"].to_list()
    n = len(dates)
    close_raw = sym_prices["close"].to_numpy().astype(np.float64)

    split_factor = np.ones(n, dtype=np.float64)
    for ex_date, ratio in splits:
        if np.isfinite(ratio) and ratio > 0 and ratio != 1.0:
            pos = bisect.bisect_left(dates, ex_date)  # indices [0, pos) are strictly before ex_date
            if pos > 0:
                split_factor[:pos] /= ratio

    div_factor = np.ones(n, dtype=np.float64)
    if apply_dividends:
        for ex_date, cash in dividends:
            if not np.isfinite(cash) or cash <= 0:
                continue
            pos = bisect.bisect_left(dates, ex_date)
            prev = pos - 1
            if prev < 0:
                continue  # no earlier bar in the (lead-in-extended) array
            # The prior-close must be the session immediately before the ex-date;
            # a large gap means it is outside the loaded window or a data hole.
            if (ex_date - dates[prev]).days > _MAX_PRIOR_GAP_DAYS:
                continue
            close_prev = close_raw[prev]
            # NaN-safe: `NaN <= 0` is False, so an explicit finite check is required
            # or a missing raw close would poison the whole pre-ex-date history.
            if not np.isfinite(close_prev) or close_prev <= 0:
                continue
            ratio = 1.0 - cash / close_prev
            if not np.isfinite(ratio) or ratio <= 0:  # dividend >= price: skip, don't flip sign
                continue
            div_factor[:pos] *= ratio

    price_factor = split_factor * div_factor
    out = {"date": sym_prices["date"], "symbol": sym_prices["symbol"]}
    for col in _PRICE_COLS:
        if col in sym_prices.columns:
            arr = sym_prices[col].to_numpy().astype(np.float64) * price_factor
            out[col] = pl.Series(col, arr)
    if "volume" in sym_prices.columns:
        vol = sym_prices["volume"].to_numpy().astype(np.float64) / split_factor
        out["volume"] = pl.Series("volume", vol)
    # passthrough any remaining (unadjusted) columns, e.g. transactions
    for col in sym_prices.columns:
        if col not in out:
            out[col] = sym_prices[col]
    return pl.DataFrame(out).select(sym_prices.columns)


def forward_adjust(prices: pl.DataFrame, events: pl.DataFrame, mode: str = "split") -> pl.DataFrame:
    """Return ``prices`` with OHLC(V) forward-adjusted per ``mode``.

    Parameters
    ----------
    prices : columns ``date``, ``symbol`` and any of ``open/high/low/close/volume``
        (plus passthrough columns like ``transactions``). Raw/unadjusted.
    events : columns ``symbol``, ``ex_date``, ``split_ratio``, ``dividend_cash``.
        Must already be point-in-time filtered (caller's responsibility).
    mode : ``none`` | ``split`` | ``total`` (alias ``forward``).
    """
    if mode not in ADJ_MODES:
        raise ValueError(f"unknown adjustment mode {mode!r}; choose from {sorted(ADJ_MODES)}")
    if mode == "none" or prices.is_empty():
        return prices

    apply_dividends = mode in _TOTAL_MODES

    # Pre-bucket events by symbol.
    splits_by: dict[str, list[tuple[dt.date, float]]] = {}
    divs_by: dict[str, list[tuple[dt.date, float]]] = {}
    if events is not None and not events.is_empty():
        ev = events.sort(["symbol", "ex_date"])
        for sym, ex_date, ratio, cash in zip(
            ev["symbol"], ev["ex_date"], ev["split_ratio"], ev["dividend_cash"]
        ):
            if ratio is not None and ratio != 1.0:
                splits_by.setdefault(sym, []).append((ex_date, float(ratio)))
            if cash is not None and cash > 0:
                divs_by.setdefault(sym, []).append((ex_date, float(cash)))

    out_frames: list[pl.DataFrame] = []
    for (sym,), sym_prices in prices.sort(["symbol", "date"]).group_by(
        ["symbol"], maintain_order=True
    ):
        out_frames.append(
            _adjust_one_symbol(
                sym_prices,
                splits_by.get(sym, []),
                divs_by.get(sym, []),
                apply_dividends,
            )
        )
    return pl.concat(out_frames, how="vertical") if out_frames else prices
