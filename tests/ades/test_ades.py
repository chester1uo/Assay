"""ADES: the leased task queue, and a controller + worker round trip over the API."""

from __future__ import annotations

import time

import numpy as np
import pytest

pytest.importorskip("pyarrow")
pytest.importorskip("sqlite3")

from assay.ades.slice import decode_print, encode_print  # noqa: E402
from assay.ades.tasks import TaskQueue  # noqa: E402
from tests.data.test_qfv_store import write_store  # noqa: E402


# ── queue ────────────────────────────────────────────────────────────────────
def _items(n, tag="f"):
    return [[f"{tag}{i}", f"ts_mean(close, {i + 2})"] for i in range(n)]


def test_slices_are_leased_once_and_owners_take_turns(tmp_path):
    q = TaskQueue(tmp_path / "q.sqlite3")
    assert q.enqueue("j1", "ann", "s", "v1", _items(130), 64) == 3
    q.enqueue("j2", "bob", "s", "v1", _items(5, "g"), 64)
    a = q.lease("w1", "v1")
    b = q.lease("w2", "v1")
    assert {a["owner"], b["owner"]} == {"ann", "bob"}          # bob is not stuck behind ann's sweep
    assert q.lease("w3", "v0") is None                         # a worker on another panel gets nothing
    assert q.complete(a["task_id"], "w2", 1, 0, 1.0) is None   # only the holder may complete
    assert q.complete(a["task_id"], "w1", len(a["items"]), 0, 1.0) is not None
    assert q.complete(a["task_id"], "w1", len(a["items"]), 0, 1.0) is None   # never twice
    assert q.status()["tasks"]["done"] == 1


def test_expired_leases_are_retried_then_failed(tmp_path):
    q = TaskQueue(tmp_path / "q.sqlite3", lease_s=10, max_attempts=2)
    q.enqueue("j1", "ann", "s", "v1", _items(3), 64)
    t = q.lease("w1", "v1")
    assert q.heartbeat(t["task_id"], "w1")
    assert q.reap(now=time.time() + 60) == []                  # first expiry: queued again
    t2 = q.lease("w2", "v1")
    assert t2["task_id"] == t["task_id"] and t2["attempt"] == 2
    assert not q.heartbeat(t["task_id"], "w1")                 # w1 lost it
    gone = q.reap(now=time.time() + 60)
    assert len(gone) == 1 and gone[0]["n"] == 3
    assert q.status()["tasks"] == {"failed": 1}


def test_fingerprint_codec_round_trips():
    r = np.array([[-1.0, 0.0, np.nan, 1.0], [0.5, -0.5, 0.25, np.nan]])
    back = decode_print(encode_print(r))
    assert np.isnan(back[0, 2]) and np.isnan(back[1, 3])
    np.testing.assert_allclose(back[np.isfinite(r)], r[np.isfinite(r)], atol=1 / 127)


# ── controller + worker over the API ─────────────────────────────────────────
class _ClientTransport:
    def __init__(self, client, key):
        self.c, self.h = client, {"Authorization": f"Bearer {key}"}

    def post(self, path, body):
        r = self.c.post(path, json=body, headers=self.h)
        return r.status_code, (r.json() if r.content else None)


@pytest.fixture
def controller(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from assay.api import auth
    from assay.api.app import app
    from assay.factordb import service

    panel = tmp_path / "panel"
    panel.mkdir()
    write_store(panel)
    for k in ("ASSAY_ACCOUNTS", "ASSAY_API_KEYS", "ASSAY_ADMIN_KEYS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("ASSAY_FACTORDB_DIR", str(tmp_path / "fdb"))
    monkeypatch.setenv("ASSAY_QFV_PANEL_DIR", str(panel))
    monkeypatch.setenv("ASSAY_FACTORDB_START", "2024-12-02")
    monkeypatch.setenv("ASSAY_FACTORDB_DOMAIN", "TOP500")
    monkeypatch.setenv("ASSAY_ADES", "1")
    monkeypatch.setenv("ASSAY_WORKER_KEYS", "wk-1")
    auth._reset_for_tests()
    monkeypatch.setattr(service, "_service", None)
    yield TestClient(app), panel
    monkeypatch.setattr(service, "_service", None)


def test_workers_need_a_worker_key(controller):
    client, _ = controller
    assert client.post("/v1/ades/lease", json={"worker": "w", "panel": "x"}).status_code == 401
    assert client.post("/v1/ades/lease", json={"worker": "w", "panel": "x"},
                       headers={"Authorization": "Bearer nope"}).status_code == 401


def test_a_job_is_scored_by_a_remote_worker(controller):
    from assay.ades.worker import Worker
    from assay.data.store import QFVPanelStore
    from assay.factordb import service

    client, panel = controller
    svc = service.get()
    out = svc.submit("ann", ["ts_delta(close, 2)", "cs_rank(volume)", "ts_mean(close, 2.0) / open", "nosuch(close)"],
                     universe="TOP500", start="2024-12-02", horizons=[1])
    assert out["queued"] == 3 and len(out["rejected"]) == 1
    w = Worker(_ClientTransport(client, "wk-1"), QFVPanelStore(panel), "w-test")
    while w.run_once():
        pass
    job = svc.job(out["job_id"])
    assert job["status"] == "done" and job["done"] == 3 and job["failed"] == 0
    assert {r["expr"] for r in job["top"]} <= {"ts_delta(close, 2)", "cs_rank(volume)", "ts_mean(close, 2.0) / open"}
    st = svc.ades.status()
    assert st["tasks"] == {"done": 1} and st["workers"][0]["name"] == "w-test"
    # the worker's fingerprints feed novelty on the controller (the toy panel is too
    # narrow for a correlation, so only the library size is checked)
    nov = svc.novelty("ts_delta(close, 2)", universe="TOP500", start="2024-12-02", horizons=[1])
    assert nov["library"] == 3 and "error" not in nov
