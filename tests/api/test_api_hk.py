"""Every REST endpoint, driven against a real Hong Kong store (architecture §4).

Unlike ``test_api.py`` — which swaps in a fake service to isolate routing — this
module wires the **real** :class:`~assay.service.AssayService` over a **real**
``market=HK`` store, so the HK path is exercised end to end: universe resolution,
per-market store routing, the engine, the portfolio cost model, and the JSON
envelope. The store is built inside ``tmp_path`` by running the production
ingester (:func:`assay.data.tushare.ingest.prepare_hk`) over a synthetic Tushare
mirror, so the test is hermetic — it needs no downloaded data and no credentials.

What it is guarding against: the HK wiring is spread across a service registry, a
config store, four route modules and the portfolio preset table, and a gap in any
one of them fails quietly rather than loudly — a US cost model on HK names, a
report filed under the wrong market, or a universe that silently resolves to the
US partition. Each of those has happened; each has a test here.

Run with::

    PYTHONPATH=src python -m pytest tests/api/test_api_hk.py -q
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import polars as pl
import pytest
from fastapi.testclient import TestClient

from assay.api.app import app
from assay.config import AssayConfig
from assay.data.tushare.ingest import prepare_hk
from assay.service import AssayService

# A synthetic board: 12 names, 8 of them mainland-incorporated (CNE ISIN) so the
# H-share carve-out has something to select, and 4 red chips / local issuers.
_HSHARES = [f"0{i:04d}.HK" for i in range(1, 9)]
_OTHERS = [f"0{i:04d}.HK" for i in range(9, 13)]
_SYMBOLS = _HSHARES + _OTHERS
_START = dt.date(2021, 1, 4)
_N_DAYS = 400  # calendar days; ~285 weekday bars — enough for monthly snapshots + IC


def _business_days(start: dt.date, n: int) -> list[dt.date]:
    out, d = [], start
    for _ in range(n):
        if d.weekday() < 5:
            out.append(d)
        d += dt.timedelta(days=1)
    return out


@pytest.fixture(scope="module")
def hk_store(tmp_path_factory) -> AssayConfig:
    """Build a real ``market=HK`` store from a synthetic Tushare mirror."""
    root = tmp_path_factory.mktemp("hk_api")
    src, out = root / "raw", root / "assay"
    dates = _business_days(_START, _N_DAYS)
    rng = np.random.default_rng(7)
    n = len(_SYMBOLS)
    close = 20.0 * np.cumprod(1 + rng.normal(0.0004, 0.02, (len(dates), n)), axis=0)

    for i, d in enumerate(dates):
        ymd = d.strftime("%Y%m%d")
        prev = close[i - 1] if i else close[0]
        base = dict(ts_code=_SYMBOLS, trade_date=[ymd] * n)
        (src / "hk" / "daily" / ymd[:4]).mkdir(parents=True, exist_ok=True)
        pl.DataFrame(
            {
                **base,
                "open": prev, "high": np.maximum(prev, close[i]) * 1.01,
                "low": np.minimum(prev, close[i]) * 0.99, "close": close[i],
                "pre_close": prev,
                "vol": np.full(n, 1e6), "amount": close[i] * 1e6,
            }
        ).write_parquet(src / "hk" / "daily" / ymd[:4] / f"{ymd}.parquet")
        (src / "hk" / "adj_factor" / ymd[:4]).mkdir(parents=True, exist_ok=True)
        pl.DataFrame(
            {**base, "cum_adjfactor": np.ones(n), "close_price": close[i]}
        ).write_parquet(src / "hk" / "adj_factor" / ymd[:4] / f"{ymd}.parquet")

    (src / "meta").mkdir(parents=True, exist_ok=True)
    pl.DataFrame(
        {
            "ts_code": _SYMBOLS,
            "isin": [f"CNE{i:09d}" for i in range(len(_HSHARES))]
            + [f"KYG{i:09d}" for i in range(len(_OTHERS))],
        }
    ).write_parquet(src / "meta" / "hk_basic.parquet")
    pl.DataFrame(
        {"ts_code": _SYMBOLS[:6], "index": ["HSI"] * 3 + ["HSTECH"] * 3}
    ).write_parquet(src / "hk" / "constituents.parquet")

    prepare_hk(out, start=dates[0], end=dates[-1], tushare_data_dir=src, min_bars=5)
    return AssayConfig.for_tests(out, market="HK", market_dirs={"HK": out}, n_workers=1)


@pytest.fixture(scope="module")
def client(hk_store) -> TestClient:
    """A TestClient whose service is a real AssayService over the HK store."""
    prev = AssayService._instance
    AssayService.init(hk_store)
    try:
        yield TestClient(app)
    finally:
        AssayService._instance = prev


PERIOD = ("2021-06-01", "2022-03-31")
EXPR = "ts_delta(close, 20) / delay(close, 20)"


# ---------------------------------------------------------------------------
# system
# ---------------------------------------------------------------------------
def test_status_reports_hk(client):
    body = client.get("/v1/system/status").json()
    hk = [m for m in body.get("markets", []) if m.get("market") == "HK"]
    assert hk and hk[0]["ingested"] is True


def test_universes_lists_every_hk_pool_with_symbols(client):
    rows = client.get("/v1/system/universes").json()
    hk = {r["id"]: r for r in rows if r.get("market") == "HK"}
    assert set(hk) == {"HK100", "HSHARE100", "HSI", "HSTECH"}
    assert hk["HK100"]["n_symbols"] == len(_SYMBOLS)
    # the H-share carve-out selects only the CNE-incorporated names
    assert hk["HSHARE100"]["n_symbols"] == len(_HSHARES)
    # ...and the two vendored snapshots must declare their survivorship bias
    assert "survivorship" in hk["HSI"]["caveat"]
    assert "survivorship" in hk["HSTECH"]["caveat"]


def test_data_calendar_is_scoped_to_hk(client):
    days = client.get("/v1/system/data-calendar", params={"market": "HK"}).json()
    assert days and all(d["market"] == "HK" for d in days)
    assert all(dt.date.fromisoformat(d["date"]).weekday() < 5 for d in days)


# ---------------------------------------------------------------------------
# factor: evaluate / lint / batch
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("universe", ["HK100", "HSHARE100", "HSI", "HSTECH"])
def test_evaluate_every_hk_universe(client, universe):
    r = client.post(
        "/v1/factor/evaluate",
        json={"expr": EXPR, "universe": universe, "period": list(PERIOD)},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["market"] == "HK"
    assert body["universe_id"] == universe
    assert body["failure_mode"] is None
    assert body["n_symbols"] > 0 and body["n_dates"] > 0
    assert body["ic"] is not None


def test_evaluate_declines_to_claim_absent_neutralization(client):
    """HK has no security_groups; the report must say so rather than imply it ran."""
    body = client.post(
        "/v1/factor/evaluate",
        json={
            "expr": EXPR, "universe": "HK100", "period": list(PERIOD),
            "neutralize": ["sector"],
        },
    ).json()
    assert body["neutralize"] == []
    assert "no group/sector data" in (body["suggestion"] or "")


def test_lint_accepts_an_hk_expression(client):
    body = client.post("/v1/factor/lint", json={"expr": EXPR}).json()
    assert body["canonical"]
    assert "close" in body["fields"]


def test_batch_files_every_report_under_the_requested_hk_universe(client):
    """Regression: batch used to stamp reports with the config default (US)."""
    body = client.post(
        "/v1/factor/batch",
        json={
            "exprs": [EXPR, "ts_delta(close, 5) / delay(close, 5)"],
            "universe": "HSHARE100", "period": list(PERIOD), "save": False,
        },
    ).json()
    reports = body["reports"] if isinstance(body, dict) else body
    assert len(reports) == 2
    for rep in reports:
        assert rep["market"] == "HK"
        assert rep["universe_id"] == "HSHARE100"
        assert tuple(rep["eval_period"]) == PERIOD


# ---------------------------------------------------------------------------
# session
# ---------------------------------------------------------------------------
def test_session_create_and_delete_over_hk(client):
    body = client.post(
        "/v1/session/create", json={"universe": "HK100", "period": list(PERIOD)}
    ).json()
    assert body["n_symbols"] == len(_SYMBOLS) and body["n_dates"] > 0
    assert client.delete(f"/v1/session/{body['session_id']}").status_code in (200, 204)


# ---------------------------------------------------------------------------
# market
# ---------------------------------------------------------------------------
def test_market_bars_route_hk_symbols_to_the_hk_store(client):
    body = client.get(
        "/v1/market/bars",
        params={"symbol": _SYMBOLS[0], "start": PERIOD[0], "end": PERIOD[1]},
    ).json()
    assert body["market"] == "HK"
    assert len(body["bars"]) > 0


def test_market_factor_series_over_hk(client):
    r = client.post(
        "/v1/market/factor-series",
        json={"symbol": _SYMBOLS[0], "expr": EXPR, "start": PERIOD[0], "end": PERIOD[1]},
    )
    assert r.status_code == 200, r.text[:200]
    body = r.json()
    assert body["market"] == "HK"
    assert body["dates"]


# ---------------------------------------------------------------------------
# portfolio
# ---------------------------------------------------------------------------
def test_backtest_infers_the_hk_cost_model_from_the_universe(client):
    """No explicit market: HK names must not be run under the US preset."""
    body = client.post(
        "/v1/portfolio/backtest",
        json={
            "expr": EXPR, "universe": "HK100",
            "period_start": PERIOD[0], "period_end": PERIOD[1],
        },
    ).json()
    cfg = body["config"]
    assert cfg["market"] == "HK"
    assert cfg["stamp_duty_rate"] == 0.001  # both sides in HK
    assert cfg["price_limit_pct"] is None   # HKEX has no daily bands
    assert cfg["t_plus_1"] is False
    assert body["a_share_metrics"] is None
    assert body["lineage"]["adj_version"] == "total"
    assert body["n_trading_days"] > 0 and body["n_rebalances"] > 0


def test_backtest_declares_a_constraint_it_could_not_apply(client):
    body = client.post(
        "/v1/portfolio/backtest",
        json={
            "expr": EXPR, "universe": "HK100",
            "period_start": PERIOD[0], "period_end": PERIOD[1],
            "sector_neutral": True, "max_sector_weight": 0.30,
        },
    ).json()
    assert any("sector_neutral" in w for w in body["warnings"])


def test_backtest_accepts_initial_capital_and_minimum_commission(client):
    response = client.post('/v1/portfolio/backtest', json={
        'expr': EXPR, 'universe': 'HK100', 'period_start': PERIOD[0], 'period_end': PERIOD[1],
        'initial_capital': 10000., 'commission_min': 5., 'slippage_model': 'zero',
    })
    assert response.status_code == 200
    body = response.json()
    assert body['config']['initial_capital'] == 10000.
    assert body['config']['commission_min'] == 5.
    liquidity = body['attribution']['liquidity']
    assert liquidity['model'] == 'trailing_notional_v1'
    assert liquidity['volume_multiplier'] == 1
    assert liquidity['impact_applied'] is False  # slippage_model is zero
    assert not any('minimum commission' in warning for warning in body['warnings'])


def test_backtest_allows_long_short_on_hk(client):
    """The A-share long-only rule must not leak onto HK."""
    r = client.post(
        "/v1/portfolio/backtest",
        json={
            "expr": EXPR, "universe": "HK100", "market": "HK",
            "period_start": PERIOD[0], "period_end": PERIOD[1],
            "long_short": True, "net_exposure": 0.0,
        },
    )
    assert r.status_code == 200
    assert r.json()["config"]["long_short"] is True


# ---------------------------------------------------------------------------
# library
# ---------------------------------------------------------------------------
def test_library_round_trip_preserves_the_hk_market(client):
    """The route takes a whole report body, so /evaluate feeds /library/factors."""
    report = client.post(
        "/v1/factor/evaluate",
        json={"expr": EXPR, "universe": "HSHARE100", "period": list(PERIOD)},
    ).json()
    assert report["market"] == "HK"
    saved = client.post("/v1/library/factors", json=report)
    assert saved.status_code == 200
    fid = saved.json()["factor_id"]
    try:
        got = client.get(f"/v1/library/factors/{fid}").json()
        assert got["market"] == "HK"
        assert got["universe_id"] == "HSHARE100"
        listed = client.get("/v1/library/factors", params={"universe": "HSHARE100"}).json()
        rows = listed["factors"] if isinstance(listed, dict) else listed
        assert any(f["factor_id"] == fid for f in rows)
    finally:
        client.request("DELETE", "/v1/library/factors", json={"factor_ids": [fid]})


def test_library_analytics_endpoints_serve_hk(client):
    for path in ("/v1/library/correlation-matrix", "/v1/library/ic-heatmap",
                 "/v1/library/embedding"):
        r = client.get(path, params={"universe": "HK100"})
        assert r.status_code == 200, f"{path} -> {r.status_code} {r.text[:200]}"


# ---------------------------------------------------------------------------
# combination
# ---------------------------------------------------------------------------
def test_combination_over_hk(client):
    """Combination splits the window into train/val/test; all three must be HK."""
    assert client.get("/v1/combination/methods").status_code == 200
    r = client.post(
        "/v1/combination",
        json={
            "factors": [EXPR, "ts_delta(close, 5) / delay(close, 5)"],
            "universe": "HK100",
            "train": ["2021-06-01", "2021-11-30"],
            "val": ["2021-12-01", "2022-01-31"],
            "test": ["2022-02-01", "2022-06-30"],
            "method": "equal",
        },
    )
    assert r.status_code == 200, r.text[:300]
    body = r.json()
    assert body.get("universe") in (None, "HK100")
    assert body.get("test") or body.get("test_metrics") or body.get("weights")


# ---------------------------------------------------------------------------
# admin / data manager
# ---------------------------------------------------------------------------
def test_admin_data_status_includes_hk(client):
    body = client.get("/v1/admin/data/status").json()
    rows = body["markets"] if isinstance(body, dict) else body
    assert any(r.get("market") == "HK" for r in rows)


def test_admin_cache_rebuild_accepts_hk(client):
    r = client.post("/v1/admin/cache/rebuild", json={"market": "HK", "universe": "HK100"})
    assert r.status_code in (200, 202)


def test_admin_rejects_an_unknown_market(client):
    r = client.post("/v1/admin/data/jobs", json={"market": "ZZ", "mode": "ingest"})
    assert r.status_code == 422
