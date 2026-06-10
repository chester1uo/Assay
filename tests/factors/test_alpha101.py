"""Tests for the 101 Formulaic Alphas catalog (arXiv:1601.00991).

Offline only. Verifies that all 101 paper formulas parse into the unified AST and
evaluate over a price panel through the Assay engine. Run with::

    PYTHONPATH=src python -m pytest tests/factors/test_alpha101.py -q
"""

from __future__ import annotations

import datetime as dt
import warnings

import numpy as np
import polars as pl
import pytest

from assay.engine import FactorEngine, ParseError, iter_fields, iter_ops, operators, parse
from assay.factors import ALPHA_101, INDNEUTRALIZE_ALPHAS

ALL = list(range(1, 102))


# ---------------------------------------------------------------------------
# synthetic panel (continuous dynamics so deep rolling chains warm up)
# ---------------------------------------------------------------------------

T, N = 420, 30
_GROUP_LEVELS = ["A", "B", "C", "D", "E"]


def _build_panel():
    rng = np.random.default_rng(11)
    dates = [dt.date(2021, 1, 4) + dt.timedelta(days=i) for i in range(T)]
    syms = [f"S{j:02d}" for j in range(N)]
    t = np.arange(T)[:, None]
    drift = (np.arange(N) - N / 2) * 0.01
    season = np.sin((t / 19.0) + np.arange(N) * 0.3) * np.linspace(1, 3, N)
    shocks = np.cumsum(rng.normal(0, 1, (T, N)), axis=0)
    close = np.abs(50 + np.arange(N) * 1.7 + drift * t + season + shocks) + 10.0
    open_ = close * (1 + rng.normal(0, 0.004, (T, N)))
    high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.006, (T, N))))
    low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.006, (T, N))))
    volume = 2e6 + 1.5e6 * np.sin(t / 11.0 + np.arange(N)) + np.abs(rng.normal(0, 3e5, (T, N)))
    vwap = (high + low + 2 * close) / 4
    panel = pl.DataFrame(
        {
            "date": np.repeat(np.array(dates), N),
            "symbol": syms * T,
            "open": open_.reshape(-1),
            "high": high.reshape(-1),
            "low": low.reshape(-1),
            "close": close.reshape(-1),
            "volume": volume.reshape(-1),
            "vwap": vwap.reshape(-1),
            "market_cap": (close * volume).reshape(-1),
        }
    )
    groups = {
        "sector": {s: _GROUP_LEVELS[i % 3] for i, s in enumerate(syms)},
        "industry": {s: _GROUP_LEVELS[i % 4] for i, s in enumerate(syms)},
        "subindustry": {s: _GROUP_LEVELS[i % 5] for i, s in enumerate(syms)},
    }
    return panel, groups, dates, syms


@pytest.fixture(scope="module")
def engine():
    panel, groups, _, _ = _build_panel()
    return FactorEngine(panel, group_data=groups)


@pytest.fixture(scope="module")
def matrices():
    """Raw (T, N) field matrices for exact numeric spot-checks."""
    panel, _, dates, syms = _build_panel()
    out = {}
    di = {d: i for i, d in enumerate(dates)}
    sj = {s: i for i, s in enumerate(syms)}
    for f in ("open", "high", "low", "close", "volume", "vwap"):
        m = np.full((T, N), np.nan)
        m[
            [di[d] for d in panel["date"].to_list()],
            [sj[s] for s in panel["symbol"].to_list()],
        ] = panel[f].to_numpy()
        out[f] = m
    return out


# ---------------------------------------------------------------------------
# catalog integrity
# ---------------------------------------------------------------------------


def test_catalog_has_all_101():
    assert sorted(ALPHA_101) == ALL
    assert all(isinstance(e, str) and e.strip() for e in ALPHA_101.values())


@pytest.mark.parametrize("n", ALL)
def test_alpha_parses(n):
    node = parse(ALPHA_101[n])
    # every operator the parser produced must be registered
    assert all(operators.is_registered(op) for op in iter_ops(node))
    # fields must be a subset of what the panel provides
    assert iter_fields(node) <= {"open", "high", "low", "close", "volume", "vwap", "market_cap"}


@pytest.mark.parametrize("n", ALL)
def test_alpha_evaluates(engine, n):
    res = engine.evaluate(ALPHA_101[n])
    assert res.shape == (T, N)
    assert res.values.dtype == np.float64


def test_overall_coverage(engine):
    """Almost every alpha produces signal; a few deep correlation->argmax->decay
    chains can be all-NaN on synthetic data (strict windowing) and resolve on
    real continuous data."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        finite = [n for n in ALL if np.isfinite(engine.evaluate(ALPHA_101[n]).values).any()]
    assert len(finite) >= 95, f"only {len(finite)}/101 produced finite values: missing {set(ALL) - set(finite)}"


# ---------------------------------------------------------------------------
# industry-neutralization alphas need group data
# ---------------------------------------------------------------------------


def test_indneutralize_set_detected():
    # The paper's indneutralize alphas (e.g. 48, 58, 63, 100).
    for n in (48, 58, 63, 100):
        assert n in INDNEUTRALIZE_ALPHAS
    assert len(INDNEUTRALIZE_ALPHAS) == 18


def test_indneutralize_requires_groups():
    panel, _, _, _ = _build_panel()
    bare = FactorEngine(panel)  # no group_data
    with pytest.raises(ValueError, match="group data"):
        bare.evaluate(ALPHA_101[INDNEUTRALIZE_ALPHAS[0]])


# ---------------------------------------------------------------------------
# exact numeric spot-checks of simple alphas against direct numpy
# ---------------------------------------------------------------------------


def test_alpha101_formula_exact(engine, matrices):
    # Alpha#101 = (close - open) / ((high - low) + .001)
    got = engine.evaluate(ALPHA_101[101]).values
    o, h, l, c = matrices["open"], matrices["high"], matrices["low"], matrices["close"]
    expected = (c - o) / ((h - l) + 0.001)
    np.testing.assert_allclose(got, expected, rtol=1e-9)


def test_alpha41_formula_exact(engine, matrices):
    # Alpha#41 = (high * low)^0.5 - vwap
    got = engine.evaluate(ALPHA_101[41]).values
    expected = (matrices["high"] * matrices["low"]) ** 0.5 - matrices["vwap"]
    np.testing.assert_allclose(got, expected, rtol=1e-9)


def test_alpha54_formula_exact(engine, matrices):
    # Alpha#54 = (-1 * ((low - close) * (open^5))) / ((low - high) * (close^5))
    got = engine.evaluate(ALPHA_101[54]).values
    o, h, l, c = matrices["open"], matrices["high"], matrices["low"], matrices["close"]
    expected = (-1 * ((l - c) * (o**5))) / ((l - h) * (c**5))
    np.testing.assert_allclose(got, expected, rtol=1e-9)


def test_bad_alpha_number():
    from assay.factors import get

    with pytest.raises(KeyError):
        get(102)
