"""Bar-frequency value object — the single source of truth for granularity.

Assay is daily-by-default; minute-level backtesting (engineering design
``docs/design/minute-backtesting.md``) adds intraday bars. Rather than scatter
``390``/``252`` constants and ``"1d"`` string checks across every layer, the
:class:`Frequency` object centralizes every granularity-dependent constant so
each layer asks the frequency instead of hard-coding a number.

``nominal_bars_per_day`` is a **sizing / default-horizon hint only**. Every
correctness-bearing count (segmentation, scheduling, annualization) must derive
bars *per session* from the calendar (:mod:`assay.data.calendar`), so half-days
and DST never shift boundaries.
"""

from __future__ import annotations

from dataclasses import dataclass

# Regular-trading-hours minutes in a full US session (09:30–16:00 ET).
_RTH_MINUTES = 390


@dataclass(frozen=True)
class Frequency:
    """An immutable description of a bar granularity (daily or intraday)."""

    code: str            # canonical id: "1d" | "1m" | "5m" | "15m"
    base_unit: str       # "day" | "minute"
    multiple: int        # 1, 5, 15
    is_intraday: bool
    time_col: str        # panel/store time column: "date" (Date) | "ts" (Datetime)
    partition_grain: str # parquet partition leaf: "month" | "day"

    @property
    def step_seconds(self) -> int:
        """Seconds per bar; ``0`` for daily (no intraday step)."""
        return 0 if self.base_unit == "day" else 60 * self.multiple

    @property
    def nominal_bars_per_day(self) -> int:
        """RTH bars in a *full* session — sizing/default-horizon HINT only.

        Not authoritative: half-days and DST are handled per-session by the
        calendar. Equals 1 for daily; ``390 / multiple`` for minute frequencies.
        """
        if self.base_unit == "day":
            return 1
        return _RTH_MINUTES // self.multiple

    def polars_every(self) -> str:
        """``group_by_dynamic`` window size for resampling 1m → this frequency."""
        return f"{self.multiple}m" if self.is_intraday else "1d"


# Canonical instances ---------------------------------------------------------
DAILY = Frequency("1d", "day", 1, False, "date", "month")
MINUTE_1 = Frequency("1m", "minute", 1, True, "ts", "day")
MINUTE_5 = Frequency("5m", "minute", 5, True, "ts", "day")
MINUTE_15 = Frequency("15m", "minute", 15, True, "ts", "day")

_BY_CODE: dict[str, Frequency] = {f.code: f for f in (DAILY, MINUTE_1, MINUTE_5, MINUTE_15)}

# Forgiving aliases (lower-cased) -> canonical instance.
_ALIASES: dict[str, Frequency] = {
    "1d": DAILY, "d": DAILY, "day": DAILY, "daily": DAILY,
    "1m": MINUTE_1, "1min": MINUTE_1, "min": MINUTE_1, "minute": MINUTE_1,
    "5m": MINUTE_5, "5min": MINUTE_5,
    "15m": MINUTE_15, "15min": MINUTE_15,
}


def parse_frequency(code: "str | Frequency | None") -> Frequency:
    """Resolve a user-supplied frequency to a canonical :class:`Frequency`.

    ``None`` / ``"1d"`` / ``"daily"`` -> :data:`DAILY`; a :class:`Frequency` is
    returned unchanged; anything unrecognized raises ``ValueError``.
    """
    if isinstance(code, Frequency):
        return code
    if code is None:
        return DAILY
    if isinstance(code, str):
        key = code.strip().lower()
        if key in _ALIASES:
            return _ALIASES[key]
        if key in _BY_CODE:
            return _BY_CODE[key]
    raise ValueError(
        f"unknown frequency {code!r}; supported: 1d, 1m, 5m, 15m "
        "(aliases: daily, minute, 5min, 15min)"
    )
