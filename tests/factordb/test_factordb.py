"""FactorDB on a tiny QFinVerse panel: identity, storage, reuse, adjustment basis, appends, agents.

The panel (from ``tests/data/test_qfv_store.py``): AAA splits 2:1 on 2025-01-02, BBB pays a
dividend on 2024-12-16; ``top500`` holds AAA+BBB until 2025-01-02, then BBB+CCC.
"""

from __future__ import annotations

import time

import numpy as np
import pytest

pytest.importorskip("pyarrow")

from assay.data.store import QFVPanelStore  # noqa: E402
from assay.engine import parse  # noqa: E402
from assay.engine.engine import FactorEngine  # noqa: E402
from assay.factordb import FactorDB  # noqa: E402
from assay.factordb import canonical as cn  # noqa: E402
from tests.data.test_qfv_store import write_store  # noqa: E402


@pytest.fixture
def panel(tmp_path):
    root = tmp_path / "panel"
    root.mkdir()
    write_store(root)
    return root


@pytest.fixture
def db(tmp_path, panel):
    return FactorDB(tmp_path / "fdb", panel, start="2024-12-02", domain=("TOP500",), workers=1)


# ── canonical form ───────────────────────────────────────────────────────────
def test_equivalent_spellings_share_one_id():
    assert cn.factor_id(parse("close/open + 1")) == cn.factor_id(parse("add(1, div(close, open))"))
    assert cn.factor_id(parse("ts_mean(volume, 20.0)")) == cn.factor_id(parse("ts_mean(volume, 20)"))
    assert cn.factor_id(parse("close - open")) != cn.factor_id(parse("open - close"))


def test_frontier_lookback_and_degree():
    node = cn.canonicalize(parse("cs_rank(ts_mean(close, 5)) - cs_rank(volume)"))
    assert sorted(str(f) for f in cn.frontier(node)) == ["ts_mean(close, 5)"]
    assert cn.frontier(cn.canonicalize(parse("ts_corr(close, volume, 10)")))[0].op == "ts_corr"
    assert cn.lookback(parse("ts_mean(ts_delay(close, 3), 5)")) == 9
    assert cn.lookback(parse("ts_ema(close, 5)")) is None
    assert cn.degree(parse("close / open")) == (0, 0)
    assert cn.degree(parse("ts_mean(close, 5)")) == (1, 0)
    assert cn.degree(parse("close * volume")) == (1, 1)
    assert cn.degree(parse("log(close)")) is None


# ── values ───────────────────────────────────────────────────────────────────
def test_time_series_factor_matches_the_engine_and_is_reused(db, panel):
    expr = "ts_mean(close, 3) / ts_mean(volume, 3)"
    dates, labels, res, st = db.evaluate([expr], "TOP500", "2024-12-02", with_stats=True)
    assert st["computed"] == 1 and res[0].values is not None
    store = QFVPanelStore(panel)
    eng = FactorEngine.from_store(store, "TOP500", (str(dates[0]), str(dates[-1])), str(dates[-1]),
                                  fields=["close", "volume"], adj="total")
    ref = eng.evaluate(expr).values
    np.testing.assert_allclose(res[0].values, ref, rtol=1e-5, equal_nan=True)
    _, _, _, st2 = db.evaluate([expr, f"cs_rank({expr})"], "TOP500", "2024-12-02",
                               with_stats=True)
    assert st2["computed"] == 0 and st2["reused"] == 1


def test_values_follow_the_request_end_basis(db):
    # AAA trades at 100 before its 2:1 split; a request ending before the split must see 100, not 50
    dates, labels, res = db.evaluate(["ts_mean(close, 3)"], "TOP500", "2024-12-20", "2024-12-31")
    a = labels.index("AAA")
    assert res[0].values[-1, a] == pytest.approx(100.0)
    _, labels2, res2 = db.evaluate(["ts_mean(close, 3)"], "TOP500", "2024-12-20", "2025-01-24")
    assert res2[0].values[5, labels2.index("AAA")] == pytest.approx(50.0)


def test_cross_section_is_the_days_members(db):
    dates, labels, res = db.evaluate(["cs_rank(close)"], "TOP500", "2024-12-23", "2025-01-10")
    v, after = res[0].values, dates >= np.datetime64("2025-01-02")
    a, c = labels.index("AAA"), labels.index("CCC")
    assert np.isfinite(v[~after, a]).all() and np.isnan(v[after, a]).all()   # AAA left top500 on 2025-01-02
    assert np.isnan(v[~after, c]).all() and np.isfinite(v[after, c]).all()   # CCC joined then


def test_bad_expressions_fail_alone(db):
    _, _, res = db.evaluate(["ts_mean(close,", "nosuchop(close)", "ts_delta(close, 1)"], "TOP500", "2024-12-02")
    assert res[0].error and res[1].error and res[2].values is not None


def test_append_when_the_panel_grows(tmp_path, panel):
    db = FactorDB(tmp_path / "fdb", panel, start="2024-12-02", domain=("TOP500",), workers=1)
    db.evaluate(["ts_mean(close, 3)"], "TOP500", "2024-12-02")
    write_store(panel, version="v000002", last="2025-02-07")       # ten more days
    db.qfv.refresh(min_interval_s=0)
    dates, labels, res, st = db.evaluate(["ts_mean(close, 3)"], "TOP500", "2024-12-02", with_stats=True)
    assert st["appended"] == 1 and str(dates[-1]) == "2025-02-07"
    store = QFVPanelStore(panel)
    eng = FactorEngine.from_store(store, "TOP500", (str(dates[0]), str(dates[-1])), str(dates[-1]),
                                  fields=["close"], adj="total")
    np.testing.assert_allclose(res[0].values, eng.evaluate("ts_mean(close, 3)").values, rtol=1e-5, equal_nan=True)


# ── agents ───────────────────────────────────────────────────────────────────
def test_agent_submit_dedup_and_query(tmp_path, db):
    pytest.importorskip("sqlite3")
    from assay.factordb.agent import FactorService
    from assay.factordb.registry import Registry

    svc = FactorService(db, Registry(tmp_path / "reg.sqlite3"))
    out = svc.submit("ann", ["ts_delta(close, 2)", "ts_delta(close, 2.0)", "cs_rank(volume)", "bad(("],
                     universe="TOP500", start="2024-12-02", horizons=[1])
    assert out["queued"] == 2 and len(out["duplicates"]) == 1 and len(out["rejected"]) == 1
    deadline = time.time() + 60
    while svc.job(out["job_id"])["status"] not in ("done", "failed") and time.time() < deadline:
        time.sleep(0.2)
    job = svc.job(out["job_id"])
    assert job["status"] == "done" and job["done"] + job["failed"] == 2
    again = svc.submit("bob", ["ts_delta(close, 2)"], universe="TOP500", start="2024-12-02", horizons=[1])
    assert again["job_id"] is None and again["n_already_scored"] == 1
    rows = svc.query(spec=out["spec"])["factors"]
    assert {r["expr"] for r in rows} >= {"ts_delta(close, 2)"}
    assert svc.budget("ann")["seconds"] > 0
