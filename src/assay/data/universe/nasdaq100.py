"""NASDAQ-100 point-in-time constituent history.

Data and API are modelled on jmccarrell/n100tickers: per-year YAML files holding
``tickers_on_Jan_1`` plus dated ``union`` (additions) / ``difference`` (removals)
operations. ``tickers_as_of`` folds the changes up to a query date to recover the
exact membership on any day.

The YAML files under ``data/`` are vendored verbatim from that repository
(sourced primarily from Nasdaq annual-change announcements and Wikipedia).

Beyond the reference API, this module adds the helpers the Assay preparer needs:
:func:`members_on`, :func:`union_over_range`, and :func:`membership_snapshots`.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Iterator
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

INDEX_ID = "NASDAQ100"
_DATA_DIR = Path(__file__).resolve().parent / "data"
_YAML_RE = re.compile(r"^n100-ticker-changes-(\d{4})\.yaml$")


class _TickerSafeLoader(yaml.SafeLoader):
    """``SafeLoader`` that does NOT coerce YAML-1.1 booleans.

    PyYAML resolves bare ``on``/``off``/``yes``/``no``/``true``/``false`` (any case)
    to ``bool`` — which silently turns the real NASDAQ-100 ticker ``ON`` (ON
    Semiconductor) into ``True``, poisoning the membership sets (and breaking
    ``sorted()`` on a mixed ``{bool, str}`` set). These files contain only dates
    and ticker strings, so dropping bool resolution keeps every scalar a string.
    """


# Rebuild the implicit-resolver table without the bool tag (new lists/dict, so the
# base SafeLoader is untouched). Dates and ints still resolve normally.
_TickerSafeLoader.yaml_implicit_resolvers = {
    ch: [(tag, rx) for tag, rx in resolvers if tag != "tag:yaml.org,2002:bool"]
    for ch, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}

#: Fixed anchor for the membership-changes iterators (matches the reference repo).
BASELINE_DATE: dt.date = dt.date(2020, 1, 1)


@dataclass(frozen=True)
class MembershipChange:
    """A single membership-change event.

    ``additions``/``removals`` interpretation depends on the iterator that
    produced it (see :func:`changes_since` / :func:`changes_before`).
    """

    effective_date: dt.date
    additions: frozenset[str]
    removals: frozenset[str]


@lru_cache(maxsize=32)
def _load_year(year: int) -> dict:
    path = _DATA_DIR / f"n100-ticker-changes-{year}.yaml"
    if not path.is_file():
        raise NotImplementedError(
            f"no NASDAQ-100 tickers defined for {year}; missing resource {path.name}"
        )
    data = yaml.load(path.read_text(encoding="utf-8"), Loader=_TickerSafeLoader)
    # Normalize change-date keys to plain ISO strings (PyYAML may parse unquoted
    # dates into datetime.date objects; the vendored files quote them, but be safe).
    changes = data.get("changes") or {}
    data["changes"] = {
        (k.isoformat() if isinstance(k, (dt.date, dt.datetime)) else str(k)): v
        for k, v in changes.items()
    }
    return data


@lru_cache(maxsize=1)
def covered_years() -> tuple[int, ...]:
    """Sorted tuple of years with vendored YAML data; must be contiguous."""
    years = sorted(
        int(m.group(1))
        for p in _DATA_DIR.iterdir()
        if (m := _YAML_RE.match(p.name))
    )
    if not years:
        raise RuntimeError(f"no n100-ticker-changes-*.yaml resources found in {_DATA_DIR}")
    expected = list(range(years[0], years[-1] + 1))
    if years != expected:
        raise RuntimeError(f"non-contiguous YAML coverage: {years}")
    return tuple(years)


def coverage_bounds() -> tuple[dt.date, dt.date]:
    """First and last dates for which membership can be resolved."""
    years = covered_years()
    return dt.date(years[0], 1, 1), dt.date(years[-1], 12, 31)


def tickers_as_of(year: int, month: int = 1, day: int = 1) -> frozenset[str]:
    """Return the NASDAQ-100 membership as a frozenset on the given date.

    >>> "TSLA" in tickers_as_of(2020, 9, 1)
    True
    """
    data = _load_year(year)
    result = set(data["tickers_on_Jan_1"])
    query_date = dt.date(year, month, day)
    for date_str in sorted(data.get("changes", {})):
        d = dt.date.fromisoformat(date_str)
        if d > query_date:
            break
        ops = data["changes"][date_str]
        result |= set(ops.get("union", ()))
        result -= set(ops.get("difference", ()))
    return frozenset(result)


def members_on(date: dt.date) -> frozenset[str]:
    """Membership on an arbitrary :class:`datetime.date`."""
    return tickers_as_of(date.year, date.month, date.day)


# -- changes iterators (parity with the reference repo) -----------------------
def _changes_for_year(year: int) -> Iterator[tuple[dt.date, frozenset[str], frozenset[str]]]:
    data = _load_year(year)
    for date_str in sorted(data.get("changes", {})):
        d = dt.date.fromisoformat(date_str)
        ops = data["changes"][date_str]
        yield d, frozenset(ops.get("union", ())), frozenset(ops.get("difference", ()))


def changes_since() -> Iterator[MembershipChange]:
    """Yield forward-sense membership changes with ``effective_date > BASELINE_DATE``."""
    for year in covered_years():
        if year < BASELINE_DATE.year:
            continue
        for d, additions, removals in _changes_for_year(year):
            if d > BASELINE_DATE:
                yield MembershipChange(d, additions, removals)


def changes_before() -> Iterator[MembershipChange]:
    """Yield inverse-sense changes with ``effective_date <= BASELINE_DATE`` in reverse order."""
    pre: list[tuple[dt.date, frozenset[str], frozenset[str]]] = []
    for year in covered_years():
        if year > BASELINE_DATE.year:
            continue
        for d, additions, removals in _changes_for_year(year):
            if d <= BASELINE_DATE:
                pre.append((d, additions, removals))
    pre.sort(key=lambda x: x[0], reverse=True)
    for d, fwd_add, fwd_rem in pre:
        # Walking backward inverts the sense of each forward change.
        yield MembershipChange(effective_date=d, additions=fwd_rem, removals=fwd_add)


@lru_cache(maxsize=1)
def baseline_membership() -> frozenset[str]:
    """Membership on :data:`BASELINE_DATE`."""
    return tickers_as_of(BASELINE_DATE.year, BASELINE_DATE.month, BASELINE_DATE.day)


# -- preparer helpers ---------------------------------------------------------
def _boundary_dates() -> list[dt.date]:
    """All dates where membership can change: each year's Jan-1 plus change dates."""
    boundaries: set[dt.date] = set()
    for year in covered_years():
        boundaries.add(dt.date(year, 1, 1))
        for date_str in _load_year(year).get("changes", {}):
            boundaries.add(dt.date.fromisoformat(date_str))
    return sorted(boundaries)


def _clamp_to_coverage(start: dt.date, end: dt.date) -> tuple[dt.date, dt.date]:
    lo, hi = coverage_bounds()
    return max(start, lo), min(end, hi)


def union_over_range(start: dt.date, end: dt.date) -> frozenset[str]:
    """Every ticker that was a NASDAQ-100 member at any point in ``[start, end]``.

    This is the symbol set the price loader needs to download for a backtest.
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


def membership_snapshots(
    start: dt.date, end: dt.date
) -> list[tuple[dt.date, frozenset[str]]]:
    """Point-in-time snapshots covering ``[start, end]``.

    Returns ``(effective_date, members)`` pairs such that the membership for any
    query date ``d`` in range is the snapshot with the greatest
    ``effective_date <= d``. Consecutive identical member sets are de-duplicated.
    The first snapshot carries its true effective_date (which may precede
    ``start``) so PIT lookups at the start of the range are exact.
    """
    start, end = _clamp_to_coverage(start, end)
    if start > end:
        return []
    boundaries = _boundary_dates()
    # The snapshot in effect at `start` (greatest boundary <= start).
    active = [b for b in boundaries if b <= start]
    snaps: list[tuple[dt.date, frozenset[str]]] = []
    if active:
        b0 = active[-1]
        snaps.append((b0, members_on(b0)))
    else:  # start precedes the first boundary; clamp to start itself
        snaps.append((start, members_on(start)))
    for b in boundaries:
        if start < b <= end:
            snaps.append((b, members_on(b)))
    # De-duplicate consecutive identical compositions.
    deduped: list[tuple[dt.date, frozenset[str]]] = []
    for eff, members in snaps:
        if deduped and deduped[-1][1] == members:
            continue
        deduped.append((eff, members))
    return deduped
