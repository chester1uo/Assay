"""Offline tests for NASDAQ-100 point-in-time membership (vendored YAML)."""

import datetime as dt

from assay.data.universe import nasdaq100


def test_tickers_as_of_known_facts():
    # docstring example from the reference repo
    assert "TSLA" in nasdaq100.tickers_as_of(2020, 9, 1)
    # AAL was removed and DXCM added effective 2020-04-20
    assert "AAL" in nasdaq100.tickers_as_of(2020, 4, 19)
    assert "AAL" not in nasdaq100.tickers_as_of(2020, 4, 20)
    assert "DXCM" not in nasdaq100.tickers_as_of(2020, 4, 19)
    assert "DXCM" in nasdaq100.tickers_as_of(2020, 4, 20)


def test_membership_size_reasonable():
    members = nasdaq100.tickers_as_of(2023, 6, 1)
    # NASDAQ-100 has 100-104 names depending on dual-class listings
    assert 95 <= len(members) <= 110


def test_yaml_boolean_tickers_stay_strings():
    """Tickers that are YAML-1.1 boolean keywords (e.g. ``ON``) must load as strings.

    Regression: ``yaml.safe_load`` coerces bare ``ON`` (ON Semiconductor) to ``True``,
    poisoning membership sets and breaking ``sorted()`` on a mixed ``{bool, str}`` set.
    """
    # No covered year may yield a non-string / bool ticker anywhere in its membership.
    for year in nasdaq100.covered_years():
        members = nasdaq100.tickers_as_of(year, 1, 1)
        assert not any(isinstance(t, bool) for t in members), f"bool ticker in {year}"
        assert all(isinstance(t, str) for t in members), f"non-string ticker in {year}"
    # The raw loader must keep the literal ``ON`` as a string (not True).
    raw_2025 = nasdaq100._load_year(2025)["tickers_on_Jan_1"]
    assert "ON" in raw_2025 and all(isinstance(t, str) for t in raw_2025)
    # A union spanning the affected years contains ON (as str) and sorts cleanly.
    union = nasdaq100.union_over_range(dt.date(2024, 1, 1), dt.date(2025, 12, 31))
    assert "ON" in union
    assert sorted(union)  # no TypeError from a mixed {bool, str} set


def test_baseline_membership_matches_reference():
    base = nasdaq100.baseline_membership()
    assert nasdaq100.BASELINE_DATE == dt.date(2020, 1, 1)
    assert "AAPL" in base and "AAL" in base and "FB" in base
    assert len(base) == 103


def test_union_over_range_superset():
    start, end = dt.date(2020, 1, 1), dt.date(2020, 12, 31)
    union = nasdaq100.union_over_range(start, end)
    jan = nasdaq100.members_on(start)
    dec = nasdaq100.members_on(end)
    assert jan <= union and dec <= union
    # names removed during 2020 (e.g. AAL) must still be in the union
    assert "AAL" in union and "DXCM" in union


def test_membership_snapshots_reconstruct_pit():
    start, end = dt.date(2020, 1, 1), dt.date(2020, 12, 31)
    snaps = nasdaq100.membership_snapshots(start, end)
    assert snaps, "expected at least one snapshot"
    # for a set of probe dates, the latest snapshot with effective_date <= d
    # must equal tickers_as_of(d)
    for probe in [dt.date(2020, 3, 1), dt.date(2020, 4, 20), dt.date(2020, 7, 21),
                  dt.date(2020, 12, 31)]:
        active = [m for eff, m in snaps if eff <= probe]
        assert active, f"no snapshot covers {probe}"
        assert set(active[-1]) == set(nasdaq100.members_on(probe))


def test_changes_since_folds_onto_baseline():
    # Folding forward changes onto the baseline reproduces a later membership.
    state = set(nasdaq100.baseline_membership())
    target = dt.date(2020, 12, 31)
    for ch in nasdaq100.changes_since():
        if ch.effective_date > target:
            break
        state |= set(ch.additions)
        state -= set(ch.removals)
    assert state == set(nasdaq100.members_on(target))


def test_covered_years_contiguous():
    years = nasdaq100.covered_years()
    assert list(years) == list(range(years[0], years[-1] + 1))
    assert years[0] <= 2015 and years[-1] >= 2023
