"""Research-state contracts: restart, lineage, failure retention and retry budgets."""
from __future__ import annotations

import importlib
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from assay.research import ResearchWorkspace


class Backend:
    def __init__(self):
        self.calls = []

    def evaluate(self, expr, dataset):
        self.calls.append((expr, dataset))
        if expr == "crash":
            raise RuntimeError("backend unavailable")
        if expr == "bad":
            return {"failure_mode": "SYNTAX_ERROR", "suggestion": "repair expression"}
        return {"failure_mode": None, "rank_ic": 0.05, "rank_icir": 0.4}


@pytest.fixture
def workspace(tmp_path):
    backend = Backend()
    ws = ResearchWorkspace(tmp_path / "research.sqlite3", {"test": backend})
    dataset = ws.register_dataset("daily", "TEST", ["2024-01-01", "2024-06-30"], backend="test")
    session = ws.create_session(dataset["id"], "Explore reversal", max_experiments=3)
    return ws, backend, dataset, session["id"]


def test_restart_and_agent_handoff_preserve_history(workspace):
    ws, backend, dataset, sid = workspace
    first = ws.evaluate(sid, "factor", "reversal", request_id="a", agent="agent-a")
    failed = ws.evaluate(sid, "bad", "alternative", request_id="b", parent_id=first["id"])
    ws.add_note(sid, "Avoid the malformed alternative", agent="agent-a")
    resumed = ResearchWorkspace(ws.path, {"test": backend})
    state = resumed.get_session(sid)
    assert state["dataset"] == dataset
    assert state["experiments"] == [first, failed]
    assert state["notes"][0]["agent"] == "agent-a"
    assert state["remaining_experiments"] == 1
    third = resumed.evaluate(sid, "factor", "retest", request_id="c", agent="agent-b")
    assert third["id"] != first["id"]  # same expression never overwrites an attempt
    assert resumed.list_sessions()[0]["attempts"] == 3
    comparison = resumed.compare_experiments([first["id"], failed["id"]])
    assert comparison["comparable"]
    assert comparison["experiments"][1]["failure_mode"] == "SYNTAX_ERROR"


def test_retry_identity_and_failed_attempts_consume_budget(workspace):
    ws, backend, _, sid = workspace
    attempt = ws.evaluate(sid, "crash", "failure test", request_id="one")
    assert attempt["status"] == "failed"
    assert attempt["error"]["type"] == "RuntimeError"
    assert ws.evaluate(sid, "crash", "failure test", request_id="one") == attempt
    assert len(backend.calls) == 1
    with pytest.raises(ValueError, match="different experiment inputs"):
        ws.evaluate(sid, "different", "failure test", request_id="one")
    for key in ("two", "three"):
        ws.evaluate(sid, "bad", "failure test", request_id=key)
    with pytest.raises(ValueError, match="budget exhausted"):
        ws.evaluate(sid, "factor", "next", request_id="four")
    assert len(backend.calls) == 3


def test_fixed_specs_comparison_and_validation(workspace):
    ws, _, dataset, sid = workspace
    dataset["horizons"].append(999)
    assert 999 not in ws.get_dataset(dataset["id"])["horizons"]
    ds2 = ws.register_dataset("different", "TEST", ["2024-01-01", "2024-05-31"], backend="test")
    other = ws.create_session(ds2["id"], "different window")
    a = ws.evaluate(sid, "a", "one", request_id="a")
    b = ws.evaluate(other["id"], "a", "one", request_id="a")
    assert not ws.compare_experiments([a["id"], b["id"]])["comparable"]
    with pytest.raises(ValueError, match="parent experiment"):
        ws.evaluate(sid, "b", "two", request_id="b", parent_id=b["id"])
    with pytest.raises(ValueError, match="start <= end <= as_of"):
        ws.register_dataset("invalid", "TEST", ["2024-01-01", "2024-06-30"],
                            as_of="2024-05-01", backend="test")
    with pytest.raises(ValueError, match="positive integer"):
        ws.register_dataset("invalid", "TEST", ["2024-01-01", "2024-06-30"],
                            horizons=[0], backend="test")
    with pytest.raises(ValueError, match="unknown research session"):
        ws.add_note("missing", "note")


def test_concurrent_retry_and_budget_across_connections(workspace):
    ws, backend, dataset, _ = workspace
    sid = ws.create_session(dataset["id"], "one attempt only", max_experiments=1)["id"]
    other = ResearchWorkspace(ws.path, {"test": backend})
    entered, release = Event(), Event()
    original = backend.evaluate

    def blocked(expr, dataset):
        entered.set()
        assert release.wait(5)
        return original(expr, dataset)

    backend.evaluate = blocked
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(ws.evaluate, sid, "a", "one", request_id="same")
        try:
            assert entered.wait(5)
            assert other.evaluate(sid, "a", "one", request_id="same")["status"] == "running"
            with pytest.raises(ValueError, match="budget exhausted"):
                other.evaluate(sid, "b", "two", request_id="different")
        finally:
            release.set()
        assert future.result()["status"] == "completed"
    assert len(backend.calls) == 1


def test_interruption_retained_and_explicit_abandon(workspace):
    ws, backend, _, sid = workspace

    def interrupt(expr, dataset):
        raise KeyboardInterrupt()

    backend.evaluate = interrupt
    with pytest.raises(KeyboardInterrupt):
        ws.evaluate(sid, "a", "one", request_id="interrupted")
    resumed = ResearchWorkspace(ws.path)
    attempt = resumed.get_session(sid)["experiments"][0]
    assert attempt["status"] == "running"
    abandoned = resumed.abandon_experiment(attempt["id"], "worker stopped")
    assert abandoned["status"] == "abandoned"
    assert resumed.get_session(sid)["remaining_experiments"] == 2
    with pytest.raises(ValueError, match="register backend"):
        resumed.evaluate(sid, "b", "two", request_id="new")
    assert len(resumed.get_session(sid)["experiments"]) == 1


def test_late_backend_result_does_not_overwrite_abandon(workspace):
    ws, backend, _, sid = workspace
    entered, release = Event(), Event()

    def blocked(expr, dataset):
        entered.set()
        assert release.wait(5)
        return {"failure_mode": None, "rank_ic": 0.1}

    backend.evaluate = blocked
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(ws.evaluate, sid, "a", "one", request_id="a")
        try:
            assert entered.wait(5)
            attempt = ws.get_session(sid)["experiments"][0]
            abandoned = ws.abandon_experiment(attempt["id"], "operator closed attempt")
        finally:
            release.set()
        assert future.result() == abandoned
    assert ws.get_experiment(attempt["id"])["status"] == "abandoned"


def test_sdk_mcp_and_real_evaluator_persist_failed_and_successful_reports(tmp_path, monkeypatch):
    import assay
    from assay.config import AssayConfig
    from assay.service import AssayService
    from assay.data.schemas import PRICE_RAW_SCHEMA, UNIVERSE_SNAPSHOTS_SCHEMA, price_partition_path, universe_snapshots_path
    from datetime import date, timedelta
    import numpy as np
    import polars as pl

    rng = np.random.default_rng(3)
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, (80, 12)), axis=0))
    panel = pl.DataFrame({
        "date": [date(2024, 1, 1) + timedelta(days=d) for d in range(80) for _ in range(12)],
        "symbol": [f"S{i}" for i in range(12)] * 80,
        "open": (close * 0.999).ravel(), "high": (close * 1.01).ravel(),
        "low": (close * 0.99).ravel(), "close": close.ravel(),
        "volume": rng.uniform(1000, 10000, 80 * 12),
    })
    raw = panel.with_columns(pl.col("date").alias("as_of_date"),
                             pl.lit(1).alias("transactions"), pl.lit("synthetic-test").alias("source_id"))
    for month in (1, 2, 3):
        path = price_partition_path(tmp_path, "US", 2024, month)
        path.parent.mkdir(parents=True, exist_ok=True)
        raw.filter(pl.col("date").dt.month() == month).select(
            [pl.col(k).cast(v) for k, v in PRICE_RAW_SCHEMA.items()]
        ).write_parquet(path)
    snapshot_path = universe_snapshots_path(tmp_path, "US")
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame([dict(index_id="NASDAQ100", effective_date=date(2024, 1, 1),
                       as_of_date=date(2024, 1, 1), symbols=[f"S{i}" for i in range(12)])],
                 schema=UNIVERSE_SNAPSHOTS_SCHEMA).write_parquet(snapshot_path)
    service = AssayService(AssayConfig.for_tests(tmp_path))
    monkeypatch.setattr(AssayService, "_instance", service)
    mod = importlib.import_module("assay.mcp.server")
    tools = {t.name: t.fn for t in mod.mcp._tool_manager.list_tools()}
    dataset = tools["assay_dataset_register"]("synthetic", "NASDAQ100", ["2024-01-01", "2024-06-30"])
    session = tools["assay_research_session_create"](dataset["id"], "reversal")
    sid = session["id"]
    success = tools["assay_experiment_run"](sid, "cs_rank(ts_returns(close,5))", "momentum", "one")
    assert success["status"] == "completed"
    assert success["report"]["rank_ic"] is not None
    failed = tools["assay_experiment_run"](sid, "no_such_op(close)", "failure", "two")
    assert failed["status"] == "failed"
    assert failed["report"]["diagnostics"]
    assert tools["assay_experiment_get"](failed["id"]) == failed
    assert tools["assay_experiment_compare"]([success["id"], failed["id"]])["comparable"]
    assert assay.research_workspace().get_session(sid)["experiments"] == [success, failed]
    fresh_service = AssayService(AssayConfig.for_tests(tmp_path))
    assert fresh_service.research.get_session(sid)["experiments"] == [success, failed]
    with pytest.raises(ValueError, match="cannot resolve immutable snapshot"):
        service.research.register_dataset("invalid snapshot", "NASDAQ100", ["2024-01-01", "2024-06-30"],
                                          snapshot_id="not-verified")
