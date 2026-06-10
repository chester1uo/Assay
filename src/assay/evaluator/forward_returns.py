"""Forward returns over aligned ``(T, N)`` price matrices (engineering-docs §6.1).

Pure numpy: this module takes the price panels already laid out as ``(T, N)``
float matrices — ``T`` trading days on axis 0 (time, ascending), ``N`` symbols on
axis 1 (cross-section) — and returns one ``(T, N)`` forward-return matrix per
horizon. It imports neither the engine nor the data layer; the caller is
responsible for materialising ``close``/``open`` from :class:`DataStore`.

Execution conventions (§6.1, "Execution price conventions"):

* ``next_open``  — enter at the T+1 open, hold ``h`` days, exit at the T+1+h open.
  ``fwd[t] = open[t+1+h] / open[t+1] - 1``. Avoids trading on the same close the
  signal is computed from; this is the default for most factor strategies.
* ``next_close`` — enter/exit on the close: ``fwd[t] = close[t+h] / close[t] - 1``.
  Suitable for MOC / index-tracking strategies.
* ``vwap``       — unsupported: the MASSIVE source provides OHLCV + transactions
  only, with no VWAP / intraday data, so this raises :class:`ValueError`.

All return matrices are ``float64`` and NaN-aware: a return is ``NaN`` wherever
the required future bar falls off the end of the panel, or any price entering the
ratio is non-finite or non-positive (the latter guards division by zero).
"""

from __future__ import annotations

import numpy as np


def _ratio(numer: np.ndarray, denom: np.ndarray) -> np.ndarray:
    """Element-wise ``numer/denom - 1``, NaN where either side is non-finite or denom<=0."""
    numer = np.asarray(numer, dtype=np.float64)
    denom = np.asarray(denom, dtype=np.float64)
    bad = ~np.isfinite(numer) | ~np.isfinite(denom) | (denom <= 0.0)
    with np.errstate(invalid="ignore", divide="ignore"):
        out = numer / denom - 1.0
    out[bad] = np.nan
    return out


def forward_returns(
    close: np.ndarray,
    open_: np.ndarray | None,
    horizons: list[int],
    execution: str = "next_open",
) -> dict[int, np.ndarray]:
    """Forward returns for each horizon as a ``{h: (T, N) float64}`` dict.

    Parameters
    ----------
    close, open_
        ``(T, N)`` price matrices (time on axis 0). ``open_`` may be ``None`` for
        the ``next_close`` convention but is **required** for ``next_open``.
    horizons
        Holding periods in trading days, e.g. ``[1, 5, 10, 20]``. Must be >= 1.
    execution
        ``next_open`` | ``next_close``. ``vwap`` raises (no intraday in MASSIVE).
    """
    close = np.asarray(close, dtype=np.float64)
    if close.ndim != 2:
        raise ValueError("close must be a 2-D (T, N) matrix")
    t_n, n_sym = close.shape

    if execution == "vwap":
        raise ValueError(
            "execution='vwap' is unsupported: the MASSIVE source provides OHLCV + "
            "transactions only (no VWAP / intraday data)"
        )
    if execution not in ("next_open", "next_close"):
        raise ValueError(
            f"unknown execution {execution!r}; expected 'next_open' or 'next_close'"
        )

    if execution == "next_open":
        if open_ is None:
            raise ValueError("execution='next_open' requires the open_ price matrix")
        open_ = np.asarray(open_, dtype=np.float64)
        if open_.shape != close.shape:
            raise ValueError("open_ and close must share the same (T, N) shape")

    out: dict[int, np.ndarray] = {}
    for h in horizons:
        h = int(h)
        if h < 1:
            raise ValueError(f"horizon must be >= 1, got {h}")
        fwd = np.full((t_n, n_sym), np.nan, dtype=np.float64)
        if execution == "next_open":
            # fwd[t] = open[t+1+h] / open[t+1] - 1   (defined for t+1+h <= T-1)
            last = t_n - 2 - h  # largest t with t+1+h <= T-1
            if last >= 0:
                entry = open_[1 : last + 2]  # open[t+1]     for t in [0, last]
                exit_ = open_[1 + h : last + 2 + h]  # open[t+1+h]  for t in [0, last]
                fwd[: last + 1] = _ratio(exit_, entry)
        else:  # next_close
            # fwd[t] = close[t+h] / close[t] - 1     (defined for t+h < T)
            last = t_n - 1 - h  # largest t with t+h <= T-1
            if last >= 0:
                entry = close[: last + 1]  # close[t]
                exit_ = close[h : last + 1 + h]  # close[t+h]
                fwd[: last + 1] = _ratio(exit_, entry)
        out[h] = fwd
    return out
