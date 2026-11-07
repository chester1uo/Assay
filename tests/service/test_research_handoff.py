"""Contracts for discovery, handoff, integrity and explicit budgeted reruns."""
import copy
import json

import pytest

from assay.research import ResearchWorkspace


class Backend:
    def __init__(self):
        self.calls = 0
        self.probes = 0
        self.private_token = "must-not-be-introspected"

    def describe(self):
        return {"kind": "test", "fields": ["close"], "private_token": self.private_token}

    def probe(self):
        self.probes += 1
        return {"status": "service_available"}

    def evaluate(self, expr, dataset):
        self.calls += 1
        if expr == "invalid":
            return {"failure_mode": "SYNTAX_ERROR", "suggestion": "repair"}
        return {"rank_ic": -.1, "rank_icir": -.5, "ic_series": [1, 2, 3]}


@pytest.fixture
def setup(tmp_path):
    backend = Backend()
    ws = ResearchWorkspace(tmp_path / "state.sqlite3", {"test": backend})
    dataset = ws.register_dataset("daily", "TEST", ["2024-01-01", "2024-06-30"], backend="test")
    sid = ws.create_session(dataset["id"], "Handoff", max_experiments=5)["id"]
    return ws, backend, dataset, sid


def test_discovery_only_probes_on_request_and_excludes_private_attributes(setup):
    ws, backend, _, _ = setup
    row, = ws.list_backends()
    assert row["registered"] and row["dataset_count"] == 1
    assert row["availability"]["status"] == "not_checked"
    assert backend.probes == 0
    assert "must-not-be-introspected" not in json.dumps(row)
    assert ws.list_backends(probe=True)[0]["availability"]["status"] == "service_available"
    assert backend.probes == 1
    missing, = ResearchWorkspace(ws.path).list_backends()
    assert not missing["registered"]
    assert missing["availability"]["status"] == "backend_not_registered"
    with pytest.raises(ValueError):
        ws.list_backends(probe=1)


def test_broken_adapter_does_not_hide_other_backends(setup):
    ws, _, _, _ = setup

    class Broken:
        def describe(self):
            return []

    ws.register_backend("broken", Broken())
    rows = {r["name"]: r for r in ws.list_backends(probe=True)}
    assert rows["broken"]["availability"]["error_type"] == "TypeError"
    assert rows["test"]["availability"]["status"] == "service_available"


def test_context_groups_normalized_expressions_and_keeps_failures(setup):
    ws, _, _, sid = setup
    a = ws.evaluate(sid, "ts_mean(close, 5)", "mean", request_id="a")
    ws.evaluate(sid, "ts_mean( close ,5 )", "mean again", request_id="b")
    ws.evaluate(sid, "invalid", "failure", request_id="c")
    ws.add_note(sid, "Inspect failed direction")
    context = ws.research_context(sid, limit=1)
    assert context["counts"]["attempts"] == 3
    assert context["counts"]["unique_canonical_expressions"] == 2
    assert context["repeated_expressions"][0]["count"] == 2
    assert context["failure_counts"] == {"SYNTAX_ERROR": 1}
    assert context["failed_attempts"][0]["suggestion"] == "repair"
    assert context["top_candidates"][0]["metrics"]["rank_ic"] == -.1
    assert context["truncated"]["recent_attempts"]
    assert "ic_series" not in json.dumps(context)
    revision = context["state_revision"]
    assert ws.research_context(sid)["state_revision"] == revision
    ws.add_note(sid, "Different state")
    assert ws.research_context(sid)["state_revision"] != revision
    assert ws.get_experiment(a["id"])["report"]["ic_series"] == [1, 2, 3]


@pytest.mark.parametrize("limit", [0, 51, True, 1.5])
def test_context_validates_limit(setup, limit):
    ws, _, _, sid = setup
    with pytest.raises(ValueError, match="limit"):
        ws.research_context(sid, limit=limit)


def test_manifest_stable_across_restart_and_detects_tampering(setup):
    ws, _, _, sid = setup
    ws.evaluate(sid, "invalid", "failure", request_id="a")
    manifest = ws.export_session(sid)
    assert manifest["state"] == ws.get_session(sid)
    assert ws.verify_manifest(manifest)
    assert ResearchWorkspace(ws.path).export_session(sid) == manifest
    changed = copy.deepcopy(manifest)
    changed["state"]["experiments"][0]["status"] = "completed"
    assert not ws.verify_manifest(changed)
    assert not ws.verify_manifest(dict(manifest, schema_version=True))
    assert not ws.verify_manifest(dict(manifest, unexpected=True))
    assert not ws.verify_manifest(None)


def test_replay_is_linked_budgeted_and_idempotent(setup):
    ws, backend, _, sid = setup
    source = ws.evaluate(sid, "close", "hypothesis", request_id="source")
    replay = ws.replay_experiment(source["id"], sid, request_id="replay", agent="next-agent")
    assert replay["id"] != source["id"]
    assert replay["replay_of"] == replay["parent_id"] == source["id"]
    assert replay["expr"] == source["expr"] and replay["spec_hash"] == source["spec_hash"]
    assert replay["agent"] == "next-agent"
    assert ws.get_session(sid)["remaining_experiments"] == 3
    assert ws.replay_experiment(source["id"], sid, request_id="replay") == replay
    assert backend.calls == 2 and ws.get_experiment(source["id"]) == source
    with pytest.raises(ValueError, match="different experiment inputs"):
        ws.evaluate(sid, "close", "hypothesis", request_id="replay", parent_id=source["id"])
    with pytest.raises(ValueError, match="cannot change"):
        ws.evaluate(sid, "open", "hypothesis", request_id="changed", replay_of=source["id"])


def test_cross_session_replay_checks_spec_before_reserving_budget(setup):
    ws, backend, dataset, sid = setup
    source = ws.evaluate(sid, "invalid", "hypothesis", request_id="source")
    compatible = ws.create_session(dataset["id"], "same", max_experiments=1)["id"]
    replay = ws.replay_experiment(source["id"], compatible, request_id="replay")
    assert replay["status"] == "failed" and replay["parent_id"] is None
    with pytest.raises(ValueError, match="budget exhausted"):
        ws.replay_experiment(source["id"], compatible, request_id="again")
    different = ws.register_dataset("other", "TEST", ["2024-02-01", "2024-06-30"], backend="test")
    target = ws.create_session(different["id"], "different")["id"]
    with pytest.raises(ValueError, match="identical dataset specification"):
        ws.replay_experiment(source["id"], target, request_id="bad")
    assert ws.get_session(target)["experiments"] == [] and backend.calls == 2


def test_interrupted_source_cannot_replay_and_handoff_explains_running_state(setup):
    ws, backend, _, sid = setup

    def interrupt(expr, dataset):
        raise KeyboardInterrupt()

    backend.evaluate = interrupt
    with pytest.raises(KeyboardInterrupt):
        ws.evaluate(sid, "close", "interrupted", request_id="source")
    state = ws.get_session(sid)
    source = state["experiments"][0]
    with pytest.raises(ValueError, match="running experiment"):
        ws.replay_experiment(source["id"], sid, request_id="rerun")
    assert ws.get_session(sid) == state
    resumed = ResearchWorkspace(ws.path)
    context = resumed.research_context(sid)
    assert context["running_attempts"][0]["id"] == source["id"]
    assert not context["backend"]["registered"]
    assert any("Register" in action for action in context["next_actions"])
    assert any("worker stopped" in action for action in context["next_actions"])
