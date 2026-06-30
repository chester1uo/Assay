"""Offline tests for the FastAPI REST surface (architecture §4).

No network, no ingested data, no MASSIVE credentials. Every test runs against
``assay.api.app.app`` through :class:`fastapi.testclient.TestClient`, with the
singleton :class:`~assay.service.AssayService` swapped for a small *fake* whose
methods return prebuilt / documented payloads. This isolates the routing, request
validation, error envelope (§4.6) and SSE framing (§4.2) from the data store and the
numeric core — the engine/evaluator are exercised by ``tests/engine`` / ``tests/evaluator``.

Patch mechanism: the routes resolve the service via ``get_service()`` ->
``AssayService.get()`` -> ``AssayService._instance`` (service.py). Setting that
class attribute (restored by the ``fake_service`` fixture) makes ``get_service``
return the fake without ever building a :class:`DataStore`.

Run with::

    PYTHONPATH=src python -m pytest tests/api -q
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from assay.api.app import app
from assay.library import FactorReport, Lineage
from assay.service import AssayService


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------
def _make_report() -> FactorReport:
    """A fully-populated, successful report (mirrors AssayService._assemble_report)."""
    return FactorReport(
        factor_id="abc123def4560000",
        expr="ts_mean(close, 5)",
        expr_canonical="ts_mean(close,5)",
        ic=0.05,
        icir=0.5,
        rank_ic=0.06,
        rank_icir=0.6,
        ic_by_horizon={1: 0.05, 5: 0.04},
        decay_halflife_days=3,
        turnover_1d=0.12,
        eval_period=("2024-01-01", "2024-01-10"),
        universe_id="NASDAQ100",
        n_dates=10,
        n_symbols=4,
        execution="next_open",
        ic_series=[0.10, 0.20, 0.15],
        rank_ic_series=[0.11, 0.19, 0.14],
        dates=["2024-01-01", "2024-01-02", "2024-01-03"],
        quintile_returns=[0.01, 0.02, 0.03, 0.04, 0.05],
        duration_ms=1.5,
        lineage=Lineage(source="SDK"),
    )


class _FakeConfig:
    """Just the attributes the system/status + universes routes read."""

    market = "US"
    default_universe = "NASDAQ100"
    default_period = ("2024-01-01", "2024-01-10")


class _FakeSessions:
    def active_sessions(self) -> int:
        return 0


class _FakeStore:
    """A store that has no data — get_universe raises, like a credential-less deploy."""

    def get_universe(self, *_a, **_k):
        raise FileNotFoundError("no universe snapshot ingested")


class _FakeCache:
    def stats(self) -> dict:
        return {"entries": 0, "bytes": 0}


class FakeService:
    """Minimal stand-in for :class:`AssayService` covering every route under test.

    ``evaluate`` returns a prebuilt report; ``stream`` yields the documented
    ``eval.*`` events; ``library_query`` returns ``[]``; ``create_session`` returns
    ``{'session_id': 'sess_test'}``. ``library_query`` accepts arbitrary kwargs so the
    status route (``limit=-1``) and the library route (filters) both work.
    """

    def __init__(self) -> None:
        self.config = _FakeConfig()
        self.sessions = _FakeSessions()
        self.store = _FakeStore()
        self.cache = _FakeCache()
        self.report = _make_report()

    # -- library --
    def library_query(self, **_filters):
        return []

    # -- library advanced views --
    def ic_heatmap(self, factor_ids, **kw):
        return {"factor_ids": factor_ids, "exprs": factor_ids,
                "periods": ["2024-01", "2024-02"], "horizon": 1, "bucket": kw.get("bucket", "month"),
                "matrix": [[0.01, 0.02] for _ in factor_ids], "summary": [0.015 for _ in factor_ids]}

    def factor_embedding(self, factor_ids, **kw):
        return {"method": kw.get("method", "mds"), "factor_ids": factor_ids,
                "points": [{"id": f, "expr": f, "x": 0.0, "y": 0.0, "cluster": 0,
                            "rank_ic": 0.0, "rank_icir": 0.0, "source": "TEST"} for f in factor_ids]}

    def factor_lineage(self, factor_ids=None, **kw):
        ids = factor_ids or ["a", "b"]
        return {"nodes": [{"id": i, "expr": i, "depth": 1, "n_ops": 0, "ops": [], "fields": []} for i in ids],
                "edges": []}

    # -- evaluate / stream --
    def evaluate(self, expr, **_kw):
        return self.report

    async def stream(self, expr, **_kw):
        rep = self.report
        yield {"event": "eval.started", "data": {"factor_id": rep.factor_id, "expr": rep.expr}}
        yield {
            "event": "eval.ic_series",
            "data": {"ic": rep.ic_series, "rank_ic": rep.rank_ic_series, "dates": rep.dates},
        }
        yield {"event": "eval.decay", "data": {"ic_by_horizon": rep.ic_by_horizon}}
        yield {"event": "eval.groups", "data": {"quintile_returns": rep.quintile_returns}}
        yield {"event": "eval.complete", "data": rep.to_dict()}

    # -- session --
    def create_session(self, **_kw):
        return {"session_id": "sess_test"}

    # -- system settings --
    def apply_system_config(self):
        return {}

    # -- hot cache (precompute) --
    def precompute_status(self):
        return {
            "store": {"entries": 2, "bytes": 4096, "dir": "/tmp/precompute"},
            "current_data_latest": {"US": "2026-06-09", "CN": None, "HK": None},
            "scopes": [{
                "scope": "NASDAQ100", "universe": "NASDAQ100", "market": "US",
                "period": ["2025-01-02", "2026-06-09"], "as_of": "2026-06-09",
                "fingerprint": "abc", "built_at": "2026-06-10T00:00:00", "data_latest": "2026-06-09",
                "n_entries": 2, "fresh": True, "current_data_latest": "2026-06-09", "top": [],
            }],
        }

    def refresh_precompute_for_market(self, market, **kw):
        return {"market": market, "data_latest": "2026-06-09",
                "refreshed": [{"universe": "NASDAQ100", "built": 2}]}

    def cache_entries(self, scope):
        return {"scope": scope, "universe": scope, "fingerprint": "abc123def456",
                "period": ["2025-01-02", "2026-06-09"], "count": 1, "bytes": 19200,
                "entries": [{"struct_hash": "deadbeef", "expr": "sub(high, low)", "count": 3,
                             "n_factors": 3, "n_nodes": 3, "score": 6, "shape": [300, 100],
                             "bytes": 240000, "coverage": 1.0, "present": True}]}

    # -- combination --
    def combination_methods(self):
        return [
            {"name": "equal", "kind": "analytic", "available": True},
            {"name": "ridge", "kind": "analytic", "available": True},
            {"name": "lightgbm", "kind": "boost", "available": True},
        ]

    def combine_factors(self, factors, **kw):
        # Echo a minimal, well-formed combination payload (the kernel is unit-tested
        # separately; here we only assert the route wiring + arg forwarding).
        names = [f if isinstance(f, str) else (f.get("name") or "f") for f in factors]
        return {
            "method": kw.get("method", "icir_weight"),
            "factor_names": names,
            "weights": {n: 1.0 / len(names) for n in names},
            "train": {"n_dates": 5, "ic": 0.01},
            "val": {"n_dates": 2, "ic": 0.02},
            "test": {"n_dates": 2, "ic": 0.03},
            "splits": {"train": list(kw["train"]), "val": list(kw["val"]), "test": list(kw["test"])},
        }


class UnavailableService(FakeService):
    """A service whose data-touching methods raise as if no data were ingested.

    Mirrors the data layer's failure surface: ``FileNotFoundError`` for un-ingested
    partitions / missing creds. The app's unhandled-exception handler maps these to
    HTTP 503 (``ASSAY-S503``) rather than 500 (app.py ``_is_data_unavailable``).
    """

    def evaluate(self, expr, **_kw):
        raise FileNotFoundError("no price data ingested for NASDAQ100")

    def create_session(self, **_kw):
        raise RuntimeError("MASSIVE credentials not configured")


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def fake_service(monkeypatch):
    """Install a :class:`FakeService` as the live singleton; restore on teardown.

    ``get_service()`` -> ``AssayService.get()`` -> ``AssayService._instance``, so
    setting ``_instance`` is the whole patch. We also stub ``AssayService.get`` to a
    no-RuntimeError accessor for belt-and-suspenders against the lazy-init fallback.
    """
    svc = FakeService()
    monkeypatch.setattr(AssayService, "_instance", svc, raising=False)
    monkeypatch.setattr(AssayService, "get", classmethod(lambda cls: svc))
    return svc


@pytest.fixture
def client(fake_service):
    """TestClient bound to the app; server exceptions surface as responses (for 503)."""
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def no_auth(monkeypatch):
    """Ensure API-key auth is disabled (the offline default) regardless of the env."""
    monkeypatch.delenv("ASSAY_API_KEYS", raising=False)


# ---------------------------------------------------------------------------
# GET /v1/system/status  — always 200 (fully guarded), or 503, never a crash
# ---------------------------------------------------------------------------
def test_system_status_ok(client, no_auth):
    """/status answers 200 with the engine-version / data / cache / session block,
    even though the fake store raises on get_universe (every read is guarded)."""
    r = client.get("/v1/system/status")
    assert r.status_code == 200
    body = r.json()
    assert "engine_version" in body
    assert body["library_factors"] == 0  # library_query() -> []
    assert body["active_sessions"] == 0
    # The store raised internally; status still reports zeros rather than crashing.
    assert body["data"]["symbols_available"] == 0


def test_system_status_never_5xx(client, no_auth):
    """Contract: /status is in {200, 503} and never a 500 crash (architecture §4.4)."""
    r = client.get("/v1/system/status")
    assert r.status_code in (200, 503)


def test_system_status_503_when_unavailable(monkeypatch, no_auth):
    """An evaluate path with no data maps the data-layer error onto HTTP 503.

    /status itself can't 503 (it swallows store errors), so we assert the documented
    503 envelope on a data-touching route to cover the 200|503 branch end-to-end.
    """
    svc = UnavailableService()
    monkeypatch.setattr(AssayService, "_instance", svc, raising=False)
    monkeypatch.setattr(AssayService, "get", classmethod(lambda cls: svc))
    client = TestClient(app, raise_server_exceptions=False)
    r = client.post("/v1/factor/evaluate", json={"expr": "ts_mean(close, 5)"})
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "ASSAY-S503"


# ---------------------------------------------------------------------------
# POST /v1/factor/evaluate — blocking JSON report (stream=false)
# ---------------------------------------------------------------------------
def test_evaluate_blocking_returns_report(client, no_auth):
    """stream=false -> 200 with a JSON report dict carrying factor_id + metrics."""
    r = client.post("/v1/factor/evaluate", json={"expr": "ts_mean(close, 5)"})
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, dict)
    assert body["factor_id"] == "abc123def4560000"
    assert body["expr"] == "ts_mean(close, 5)"
    assert body["ic"] == pytest.approx(0.05)
    assert "error" not in body  # a successful report is not an error envelope


def test_evaluate_default_stream_is_blocking(client, no_auth):
    """Omitting ``stream`` defaults to the blocking JSON report (not SSE)."""
    r = client.post("/v1/factor/evaluate", json={"expr": "close"})
    assert r.status_code == 200
    assert "text/event-stream" not in r.headers.get("content-type", "")
    assert "factor_id" in r.json()


def test_evaluate_missing_expr_is_422(client, no_auth):
    """A body without the required ``expr`` fails validation -> 422 §4.6 envelope."""
    r = client.post("/v1/factor/evaluate", json={})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "ASSAY-P000"


# ---------------------------------------------------------------------------
# POST /v1/factor/evaluate — SSE stream (stream=true)
# ---------------------------------------------------------------------------
def test_evaluate_stream_is_sse_with_complete(client, no_auth):
    """stream=true -> 200 text/event-stream whose frames include 'eval.complete'."""
    with client.stream(
        "POST", "/v1/factor/evaluate", json={"expr": "ts_mean(close, 5)", "stream": True}
    ) as resp:
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]
        body = "".join(resp.iter_text())
    # Ordered SSE contract (architecture §4.2): started ... complete.
    assert "event: eval.started" in body
    assert "eval.complete" in body
    assert body.index("eval.started") < body.index("eval.complete")


def test_evaluate_stream_complete_carries_report(client, no_auth):
    """The terminal 'eval.complete' frame's data is the JSON report dict."""
    with client.stream(
        "POST", "/v1/factor/evaluate", json={"expr": "ts_mean(close, 5)", "stream": True}
    ) as resp:
        body = "".join(resp.iter_text())

    # Find the data line that belongs to the eval.complete event and parse it.
    complete_payload = None
    lines = body.splitlines()
    for i, line in enumerate(lines):
        if line.strip() == "event: eval.complete":
            for j in range(i + 1, len(lines)):
                if lines[j].startswith("data:"):
                    complete_payload = json.loads(lines[j][len("data:"):].strip())
                    break
            break
    assert complete_payload is not None
    assert complete_payload["factor_id"] == "abc123def4560000"


# ---------------------------------------------------------------------------
# GET /v1/library/factors — {total, factors}
# ---------------------------------------------------------------------------
def test_library_factors_shape(client, no_auth):
    """Empty library -> 200 {'total': 0, 'factors': []} (architecture §4.3)."""
    r = client.get("/v1/library/factors")
    assert r.status_code == 200
    body = r.json()
    assert set(body) >= {"total", "factors"}
    assert body["total"] == 0
    assert body["factors"] == []


def test_library_factors_accepts_filters(client, no_auth):
    """Filter / paging query params are accepted and don't change the empty shape."""
    r = client.get(
        "/v1/library/factors",
        params={"min_rank_icir": 0.1, "sort_by": "ic", "limit": 10, "offset": 0},
    )
    assert r.status_code == 200
    assert r.json()["total"] == 0


# ---------------------------------------------------------------------------
# POST /v1/session/create — {session_id, ...}
# ---------------------------------------------------------------------------
def test_session_create_returns_session_id(client, no_auth):
    """create -> 200 with the session descriptor carrying session_id (architecture §4.2)."""
    r = client.post("/v1/session/create", json={})
    assert r.status_code == 200
    assert r.json()["session_id"] == "sess_test"


def test_session_create_with_params(client, no_auth):
    """An explicit universe/period body is accepted and still yields a session id."""
    r = client.post(
        "/v1/session/create",
        json={"universe": "NASDAQ100", "period": ["2024-01-01", "2024-01-10"]},
    )
    assert r.status_code == 200
    assert r.json()["session_id"] == "sess_test"


# ---------------------------------------------------------------------------
# GET /v1/admin/config — data + system settings surface
# ---------------------------------------------------------------------------
def test_admin_config_exposes_system_settings(client, no_auth, monkeypatch, tmp_path):
    # point the config store at a fresh temp file -> returns seeded defaults
    monkeypatch.setenv("ASSAY_CONFIG_FILE", str(tmp_path / "cfg.json"))
    r = client.get("/v1/admin/config")
    assert r.status_code == 200
    body = r.json()
    # data settings present (existing)
    assert "dirs" in body and "massive_s3" in body
    # system settings present (new) — parallelism / cache / eval defaults
    sysc = body["system"]
    assert "n_workers" in sysc and "l2_max_gb" in sysc
    assert "precompute_top_k" in sysc and "precompute_auto_refresh" in sysc
    assert "default_universe" in sysc and "default_execution" in sysc and "default_horizons" in sysc


# ---------------------------------------------------------------------------
# GET /v1/admin/cache/status + POST /v1/admin/cache/rebuild — hot cache
# ---------------------------------------------------------------------------
def test_admin_cache_status(client, no_auth):
    r = client.get("/v1/admin/cache/status")
    assert r.status_code == 200
    body = r.json()
    assert body["store"]["entries"] == 2
    assert body["scopes"][0]["universe"] == "NASDAQ100" and body["scopes"][0]["fresh"] is True


def test_admin_cache_entries_lists_contents(client, no_auth):
    r = client.get("/v1/admin/cache/entries", params={"scope": "NASDAQ100"})
    assert r.status_code == 200
    body = r.json()
    assert body["universe"] == "NASDAQ100" and body["count"] == 1
    e = body["entries"][0]
    assert e["expr"] == "sub(high, low)" and e["shape"] == [300, 100]
    assert {"count", "n_nodes", "score", "bytes", "coverage", "present"} <= set(e)


def test_admin_cache_rebuild_queues_job(client, no_auth):
    r = client.post("/v1/admin/cache/rebuild", json={"market": "US"})
    # the route queues a background job and returns its descriptor
    assert r.status_code == 200
    assert "status" in r.json() or "id" in r.json()


def test_admin_cache_rebuild_bad_market_422(client, no_auth):
    r = client.post("/v1/admin/cache/rebuild", json={"market": "ZZ"})
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# GET /v1/library/{ic-heatmap,embedding,lineage} — advanced library views
# ---------------------------------------------------------------------------
def test_library_ic_heatmap(client, no_auth):
    r = client.get("/v1/library/ic-heatmap", params={"factor_ids": "a,b", "bucket": "month"})
    assert r.status_code == 200
    body = r.json()
    assert body["periods"] and len(body["matrix"]) == 2 and body["bucket"] == "month"


def test_library_embedding(client, no_auth):
    r = client.get("/v1/library/embedding", params={"factor_ids": "a,b,c", "method": "mds"})
    assert r.status_code == 200
    body = r.json()
    assert body["method"] == "mds" and len(body["points"]) == 3
    assert all({"x", "y", "cluster"} <= set(p) for p in body["points"])


def test_library_lineage(client, no_auth):
    r = client.get("/v1/library/lineage", params={"factor_ids": "a,b"})
    assert r.status_code == 200
    body = r.json()
    assert [n["id"] for n in body["nodes"]] == ["a", "b"] and "edges" in body


# ---------------------------------------------------------------------------
# POST /v1/combination — factor combination with train/val/test
# ---------------------------------------------------------------------------
def test_combination_runs_and_returns_scorecard(client, no_auth):
    """A valid body -> 200 with the chosen method, weights and train/val/test splits."""
    r = client.post("/v1/combination", json={
        "factors": ["rank(close)", {"name": "mom", "expr": "delta(close, 10)"}],
        "train": ["2021-01-01", "2021-06-30"],
        "val": ["2021-07-01", "2021-08-31"],
        "test": ["2021-09-01", "2021-12-31"],
        "method": "auto",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["method"] == "auto"
    assert body["factor_names"] == ["rank(close)", "mom"]
    assert body["splits"]["test"] == ["2021-09-01", "2021-12-31"]
    for split in ("train", "val", "test"):
        assert "ic" in body[split]


def test_combination_methods_lists_schemes(client, no_auth):
    """GET /v1/combination/methods -> available analytic + learned-model schemes."""
    r = client.get("/v1/combination/methods")
    assert r.status_code == 200
    methods = r.json()["methods"]
    names = {m["name"] for m in methods}
    assert {"equal", "ridge"} <= names


def test_combination_empty_factors_is_422(client, no_auth):
    """No factors -> 422 (client error), never a 500."""
    r = client.post("/v1/combination", json={
        "factors": [],
        "train": ["2021-01-01", "2021-06-30"],
        "val": ["2021-07-01", "2021-08-31"],
        "test": ["2021-09-01", "2021-12-31"],
    })
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# health probe — always cheap, never touches the service
# ---------------------------------------------------------------------------
def test_health_ok(client, no_auth):
    """/health is a pure liveness probe -> 200 {'status': 'ok', ...}."""
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
