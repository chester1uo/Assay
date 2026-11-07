"""Optional AlphaBench adapter rejects axis/endpoint drift before exposing scores."""
from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace

import pytest

from assay.integrations.alphabench import AlphaBenchBackend
from assay.research import ResearchWorkspace


@pytest.fixture
def adapter(tmp_path, monkeypatch):
    # The adapter imports only this optional DTO. No provider or live service is
    # needed to verify fail-closed contract behavior in Assay's own test suite.
    package = ModuleType("alphabench_v2")
    package.__path__ = []
    module = ModuleType("alphabench_v2.assay")
    module.EvaluationSpec = lambda **kw: SimpleNamespace(**kw)
    monkeypatch.setitem(sys.modules, "alphabench_v2", package)
    monkeypatch.setitem(sys.modules, "alphabench_v2.assay", module)
    report = dict(failure_mode=None, rank_ic=0.05, universe_id="NASDAQ100",
        eval_period=["2024-01-02", "2024-06-28"], universe_as_of="2024-06-28",
        universe_symbols_hash="axis", n_symbols=101, data_as_of="2024-06-28", execution="next_open")
    client = SimpleNamespace(base_url="http://127.0.0.1:8002", evaluate=lambda *a: dict(report))
    backend = AlphaBenchBackend(client)
    ws = ResearchWorkspace(tmp_path / "research.sqlite3", {"alphabench": backend})
    options = dict(endpoint=client.base_url, universe_as_of="2024-06-28", universe_symbols_hash="axis", n_symbols=101)
    dataset = ws.register_dataset("train", "NASDAQ100", report["eval_period"],
                                  backend="alphabench", backend_options=options)
    sid = ws.create_session(dataset["id"], "test adapter")["id"]
    return ws, backend, report, client, dataset, sid


def test_success_and_backend_settings_are_persisted_and_hashed(adapter):
    ws, _, _, _, dataset, sid = adapter
    first = ws.evaluate(sid, "factor", "test", request_id="one")
    assert first["status"] == "completed"
    assert ws.get_dataset(dataset["id"])["backend_options"] == dataset["backend_options"]
    other_options = dict(dataset["backend_options"], universe_symbols_hash="different-axis")
    other = ws.register_dataset("other", "NASDAQ100", dataset["period"], backend="alphabench", backend_options=other_options)
    assert other["spec_hash"] != dataset["spec_hash"]


@pytest.mark.parametrize("field,value", [
    ("universe_symbols_hash", "wrong-axis"), ("n_symbols", 100),
    ("universe_as_of", "2024-07-01"), ("data_as_of", "2024-07-31"),
    ("eval_period", ["2023-01-01", "2024-06-28"]), ("execution", "close"),
])
def test_contract_drift_is_recorded_as_failure(adapter, field, value):
    ws, _, report, _, _, sid = adapter
    report[field] = value
    attempt = ws.evaluate(sid, "factor", "test", request_id="one")
    assert attempt["status"] == "failed"
    assert field in attempt["error"]["message"]
    assert attempt["report"] is None


def test_wrong_endpoint_and_unresolvable_snapshot_rejected(adapter):
    _, backend, _, client, dataset, _ = adapter
    with pytest.raises(ValueError, match="snapshot"):
        backend.evaluate("factor", dict(dataset, snapshot_id="unknown"))
    client.base_url = "http://127.0.0.1:8000"
    with pytest.raises(ValueError, match="endpoint"):
        backend.evaluate("factor", dataset)


def test_diagnostics_remain_inspectable(adapter):
    ws, _, report, _, _, sid = adapter
    report.update(failure_mode="SYNTAX_ERROR", diagnostics={"ok": False})
    attempt = ws.evaluate(sid, "bad", "failure probe", request_id="one")
    assert attempt["status"] == "failed"
    assert attempt["report"]["diagnostics"] == {"ok": False}


def test_discovery_probes_remote_inventory_without_evaluation(adapter):
    ws, _, _, client, _, _ = adapter
    calls = []

    def provenance():
        calls.append("inventory")
        return {"health": {"status": "ok"}, "status": {"markets": [{"market": "US"}]},
                "data_calendar": [{"date": "2024-01-02"}]}

    client.provenance = provenance
    row, = ws.list_backends()
    assert row["capabilities"]["endpoint"] == "http://127.0.0.1:8002"
    assert row["capabilities"]["supports_universe_freeze"]
    assert calls == []
    row, = ws.list_backends(probe=True)
    assert calls == ["inventory"]
    assert row["availability"]["calendar_dates"] == 1
    assert row["availability"]["markets"] == [{"market": "US"}]


def test_unreachable_remote_backend_is_an_explicit_discovery_error(adapter):
    ws, _, _, client, _, _ = adapter

    def unavailable():
        raise ConnectionError("service unavailable")

    client.provenance = unavailable
    row, = ws.list_backends(probe=True)
    assert row["registered"]
    assert row["availability"]["status"] == "error"
    assert row["availability"]["error_type"] == "ConnectionError"
