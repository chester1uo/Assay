# Vendored from QFinVerse src/qfinverse/data/daily/securities.py by scripts/sync_qfv_panel.py.
# Do not edit here: change QFinVerse and sync.
"""Ticker runs: which security a ticker meant on a date."""

from __future__ import annotations

from typing import Optional


class RunIndex:
    """Which security a ticker meant on a date."""

    def __init__(self, runs: dict):
        self._by: dict[str, list[tuple[str, str, int]]] = {}
        for t, sid, a, b in zip(runs["ticker"], runs["sid"], runs["start"], runs["end"]):
            self._by.setdefault(t, []).append((a, b, int(sid)))
        for v in self._by.values():
            v.sort()

    def resolve(self, ticker: str, day: str, slack_days: int = 10) -> Optional[int]:
        """The run covering ``day``, else one that ended within ``slack_days`` calendar
        days before it (an ex-date on a day the stock did not trade)."""
        from datetime import date

        best = None
        for a, b, sid in self._by.get(ticker, ()):
            if a > day:
                break
            if b >= day:
                return sid
            if (date.fromisoformat(day) - date.fromisoformat(b)).days <= slack_days:
                best = sid
        return best

    def latest(self, ticker: str, day: str) -> Optional[int]:
        """The most recent security that used ``ticker`` on or before ``day`` (also
        after it stopped trading)."""
        best = None
        for a, _, sid in self._by.get(ticker, ()):
            if a > day:
                break
            best = sid
        return best
