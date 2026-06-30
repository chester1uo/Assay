"""S&P 500 membership history — survivorship-free, point-in-time.

Mirrors the :mod:`assay.data.universe.nasdaq100` public surface (:func:`members_on`,
:func:`union_over_range`, :func:`membership_snapshots`, :func:`coverage_bounds`) so
the :class:`~assay.data.ingest.universe.UniverseIngester` and the prepare pipeline
treat SP500 identically to NASDAQ100.

Source data is a compact ``baseline + dated add/remove deltas`` JSON
(``data/sp500-historical.json``), distilled from the public
``github.com/fja05680/sp500`` "S&P 500 Historical Components & Changes" dataset.
Class-share tickers carry a dot (``BRK.B``, ``BF.B``) — the same spelling the
MASSIVE day-aggregate flat files use, so prices align without symbol mapping.
"""

from __future__ import annotations

import datetime as dt
import json
from functools import lru_cache
from pathlib import Path

_DATA_FILE = Path(__file__).resolve().parent / "data" / "sp500-historical.json"


@lru_cache(maxsize=1)
def _snapshots() -> list[tuple[dt.date, frozenset[str]]]:
    """Reconstruct ``(effective_date, members)`` boundary snapshots from the deltas."""
    raw = json.loads(_DATA_FILE.read_text(encoding="utf-8"))
    cur = set(raw["baseline"])
    snaps: list[tuple[dt.date, frozenset[str]]] = [
        (dt.date.fromisoformat(raw["baseline_date"]), frozenset(cur))
    ]
    for ch in raw["changes"]:
        cur = (cur - set(ch.get("remove", []))) | set(ch.get("add", []))
        snaps.append((dt.date.fromisoformat(ch["date"]), frozenset(cur)))
    return snaps


def _boundary_dates() -> list[dt.date]:
    return [d for d, _ in _snapshots()]


def coverage_bounds() -> tuple[dt.date, dt.date]:
    """First and last effective dates with known membership."""
    snaps = _snapshots()
    return snaps[0][0], snaps[-1][0]


def _clamp_to_coverage(start: dt.date, end: dt.date) -> tuple[dt.date, dt.date]:
    lo, hi = coverage_bounds()
    return max(start, lo), min(end, hi)


def members_on(date: dt.date) -> frozenset[str]:
    """Exact S&P 500 membership on ``date`` (the snapshot with greatest eff_date ≤ date).

    Dates after the last known change return the latest composition (membership is
    constant until the next change); dates before coverage return ``frozenset()``.
    """
    out: frozenset[str] = frozenset()
    for eff, members in _snapshots():
        if eff <= date:
            out = members
        else:
            break
    return out


def union_over_range(start: dt.date, end: dt.date) -> frozenset[str]:
    """Every ticker that was an S&P 500 member at any point in ``[start, end]``.

    The symbol set the price loader downloads (survivorship-free).
    """
    start, end = _clamp_to_coverage(start, end)
    if start > end:
        return frozenset()
    result: set[str] = set(members_on(start))
    for b in _boundary_dates():
        if start < b <= end:
            result |= set(members_on(b))
    result |= set(members_on(end))
    return frozenset(result)


def membership_snapshots(start: dt.date, end: dt.date) -> list[tuple[dt.date, frozenset[str]]]:
    """PIT ``(effective_date, members)`` snapshots covering ``[start, end]`` (deduped).

    The membership for any query date ``d`` in range is the snapshot with the greatest
    ``effective_date ≤ d``. The first snapshot carries its true effective date (which
    may precede ``start``) so PIT lookups at the start of the range are exact.
    """
    start, end = _clamp_to_coverage(start, end)
    if start > end:
        return []
    boundaries = _boundary_dates()
    active = [b for b in boundaries if b <= start]
    snaps: list[tuple[dt.date, frozenset[str]]] = []
    if active:
        b0 = active[-1]
        snaps.append((b0, members_on(b0)))
    else:
        snaps.append((start, members_on(start)))
    for b in boundaries:
        if start < b <= end:
            snaps.append((b, members_on(b)))
    deduped: list[tuple[dt.date, frozenset[str]]] = []
    for eff, members in snaps:
        if not deduped or deduped[-1][1] != members:
            deduped.append((eff, members))
    return deduped
