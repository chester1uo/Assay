"""Tests for :class:`assay.service.AssayService` — the singleton facade (architecture §2).

Offline only — no network, no MASSIVE credentials, no ingested data. The service's
only credential-hungry dependency is the lazily-built :class:`DataStore`, reached
exclusively through :meth:`FactorEngine.from_store`. We monkeypatch ``from_store``
to build an engine directly from a synthetic OHLCV panel, so every service path
(cold ``evaluate``, ``stream``, ``batch``, session reuse) runs fully in-process.

Run with::

    PYTHONPATH=src python -m pytest tests/service -q
"""

from __future__ import annotations

import asyncio
import datetime as dt

import numpy as np
import polars as pl
import pytest

from assay.engine import FactorEngine
from assay.library import FactorReport, FactorSummary
from assay.service import AssayService


# ---------------------------------------------------------------------------
# synthetic data + injection
# ---------------------------------------------------------------------------


def make_ohlcv_panel(t: int = 60, n: int = 8, *, seed: int = 7) -> pl.DataFrame:
    """Long (date, symbol, open, high, low, close, volume) panel from (T, N) matrices.

    A random-walk close with realistic OHLC ordering and positive volume — enough
    cross-sectional spread per date that ``cs_rank``/IC are non-degenerate.
    """
    rng = np.random.default_rng(seed)
    close = 100.0 + rng.normal(size=(t, n)).cumsum(axis=0)
    open_ = close + rng.normal(size=(t, n)) * 0.3
    high = np.maximum(open_, close) + np.abs(rng.normal(size=(t, n))) * 0.2
    low = np.minimum(open_, close) - np.abs(rng.normal(size=(t, n))) * 0.2
    volume = 1e6 + np.abs(rng.normal(size=(t, n))) * 1e4

    dates = [dt.date(2024, 1, 1) + dt.timedelta(days=i) for i in range(t)]
    symbols = [f"S{j}" for j in range(n)]
    return pl.DataFrame(
        {
            "date": np.repeat(np.array(dates), n),
            "symbol": symbols * t,
            "open": open_.reshape(-1),
            "high": high.reshape(-1),
            "low": low.reshape(-1),
            "close": close.reshape(-1),
            "volume": volume.reshape(-1),
        }
    )


@pytest.fixture
def panel() -> pl.DataFrame:
    return make_ohlcv_panel()


@pytest.fixture
def service(monkeypatch, panel, tmp_path) -> AssayService:
    """A fresh singleton whose engine is built from the synthetic panel, not a store.

    Patches :meth:`FactorEngine.from_store` so no :class:`DataStore` (and thus no
    MASSIVE credential) is ever constructed; the ``group_data`` keyword still flows
    through so group ops remain testable. The ``_instance`` singleton is reset around
    each test for isolation.
    """

    def fake_from_store(store, *, universe, period, as_of, adj="split", group_data=None, **kw):
        # Honour the same OHLCV contract the real DataStore-backed path provides.
        return FactorEngine(panel, group_data=group_data)

    monkeypatch.setattr(FactorEngine, "from_store", staticmethod(fake_from_store))

    saved = AssayService._instance
    AssayService._instance = None
    cfg = AssayConfig_for_tests(tmp_path)
    svc = AssayService.init(cfg)
    try:
        yield svc
    finally:
        AssayService._instance = saved


def AssayConfig_for_tests(tmp_path):
    """Local import wrapper so the fixture reads ``AssayConfig.for_tests`` once."""
    from assay.config import AssayConfig

    # n_workers=2 keeps the ThreadPoolExecutor batch path exercised but cheap.
    return AssayConfig.for_tests(tmp_path, n_workers=2)


# ---------------------------------------------------------------------------
# init / singleton
# ---------------------------------------------------------------------------


def test_get_before_init_raises(monkeypatch):
    monkeypatch.setattr(AssayService, "_instance", None)
    with pytest.raises(RuntimeError, match="not initialized"):
        AssayService.get()


def test_init_returns_singleton(service):
    assert AssayService.get() is service
    # re-init replaces the process-wide singleton
    from assay.config import AssayConfig

    svc2 = AssayService.init(AssayConfig.for_tests(service.config.data_dir))
    assert AssayService.get() is svc2
    assert svc2 is not service


# ---------------------------------------------------------------------------
# evaluate: success path
# ---------------------------------------------------------------------------


def test_evaluate_success_report(service):
    report = service.evaluate("cs_rank(ts_returns(close, 5))")
    assert isinstance(report, FactorReport)

    # identity is set from the canonical-expression hash
    assert report.factor_id
    assert report.factor_id == FactorReport.compute_factor_id(report.expr_canonical)
    assert report.expr == "cs_rank(ts_returns(close, 5))"

    # a good factor diagnoses clean: no failure mode, no look-ahead
    assert report.failure_mode is None
    assert report.lookahead_detected is False

    # headline rank metrics are finite reals
    assert np.isfinite(report.rank_ic)
    assert np.isfinite(report.rank_icir)
    assert np.isfinite(report.ic)

    # ic_by_horizon populated for the default horizons (1, 5, 10, 20)
    assert report.ic_by_horizon
    assert set(report.ic_by_horizon) == {1, 5, 10, 20}
    assert all(np.isfinite(v) for v in report.ic_by_horizon.values())

    # context + detail series wired through
    assert report.universe_id == service.config.default_universe
    assert tuple(report.eval_period) == tuple(service.config.default_period)
    assert report.n_dates > 0 and report.n_symbols == 8
    assert report.ic_series and report.dates
    assert report.duration_ms is not None and report.duration_ms >= 0.0


def test_evaluate_horizons_override(service):
    report = service.evaluate("cs_rank(close)", horizons=[1, 3])
    assert set(report.ic_by_horizon) == {1, 3}


def test_evaluate_to_dict_json_safe(service):
    # to_dict must be JSON-safe (NaN/inf -> None) for the SSE/REST surface.
    import json

    d = service.evaluate("cs_rank(ts_returns(close, 5))").to_dict()
    json.dumps(d)  # must not raise
    assert d["factor_id"]
    assert d["failure_mode"] is None


# ---------------------------------------------------------------------------
# evaluate: failure path (look-ahead via negative delay)
# ---------------------------------------------------------------------------


def test_evaluate_lookahead_failure(service):
    # ts_delay(close, -5) peeks 5 days into the future -> LOOKAHEAD, no crash.
    report = service.evaluate("ts_delay(close, -5)")
    assert isinstance(report, FactorReport)
    assert report.failure_mode is not None
    assert report.failure_mode == "LOOKAHEAD"
    assert report.lookahead_detected is True

    # diagnostics are attached and metrics are null (NaN -> None in to_dict)
    assert report.diagnostics is not None
    assert np.isnan(report.rank_ic)
    assert report.ic_by_horizon == {}
    d = report.to_dict()
    assert d["rank_ic"] is None
    assert d["failure_mode"] == "LOOKAHEAD"


def test_evaluate_syntax_error_failure(service):
    # An unparseable expression must still yield a report, not raise.
    report = service.evaluate("ts_mean(close")  # unbalanced paren
    assert isinstance(report, FactorReport)
    assert report.failure_mode is not None
    assert report.diagnostics is not None


# ---------------------------------------------------------------------------
# batch: sorted desc + save -> library_query finds it
# ---------------------------------------------------------------------------


def test_batch_sorted_desc_and_saved(service):
    exprs = [
        "cs_rank(ts_returns(close, 5))",
        "cs_rank(ts_returns(close, 10))",
        "cs_rank(-1 * ts_returns(close, 5))",
        "cs_rank(ts_mean(close, 5))",
    ]
    reports = service.batch(exprs, sort_by="rank_icir", save=True)
    assert len(reports) == len(exprs)
    assert all(isinstance(r, FactorReport) for r in reports)

    # descending by the sort key, with None/NaN ranking last
    keys = [
        r.rank_icir if (r.rank_icir is not None and np.isfinite(r.rank_icir)) else float("-inf")
        for r in reports
    ]
    assert keys == sorted(keys, reverse=True)

    # save=True persisted every report; library_query surfaces them as summaries
    summaries = service.library_query(min_rank_icir=float("-inf"))
    assert all(isinstance(s, FactorSummary) for s in summaries)
    found = {s.factor_id for s in summaries}
    for r in reports:
        assert r.factor_id in found

    # round-trip a full report through the library by id
    one = reports[0]
    stored = service.library.get(one.factor_id)
    assert stored is not None
    assert stored.expr == one.expr


def test_batch_empty_returns_empty(service):
    assert service.batch([]) == []


# ---------------------------------------------------------------------------
# stream: events ordered eval.started .. eval.complete
# ---------------------------------------------------------------------------


def _collect(agen):
    async def _run():
        return [ev async for ev in agen]

    return asyncio.run(_run())


def test_stream_success_event_order(service):
    events = _collect(service.stream("cs_rank(ts_returns(close, 5))"))
    names = [e["event"] for e in events]

    assert names[0] == "eval.started"
    assert names[-1] == "eval.complete"
    # the full ordered sequence for a healthy factor
    assert names == [
        "eval.started",
        "eval.ic_series",
        "eval.decay",
        "eval.groups",
        "eval.complete",
    ]

    started = events[0]["data"]
    assert started["expr"] == "cs_rank(ts_returns(close, 5))"
    assert started["factor_id"]

    complete = events[-1]["data"]  # to_dict() payload
    assert complete["factor_id"] == started["factor_id"]
    assert complete["failure_mode"] is None


def test_stream_failure_still_brackets(service):
    # A failed factor emits at least started .. complete (no detail events required).
    events = _collect(service.stream("ts_delay(close, -5)"))
    names = [e["event"] for e in events]
    assert names[0] == "eval.started"
    assert names[-1] == "eval.complete"
    assert events[-1]["data"]["failure_mode"] == "LOOKAHEAD"
