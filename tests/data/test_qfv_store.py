"""QFinVerse daily panel as the US store: adjustment on read, PIT membership, matrix engine."""

import datetime as dt
import json

import numpy as np
import pytest

pa = pytest.importorskip("pyarrow")
pq = pytest.importorskip("pyarrow.parquet")

from assay.data.store import QFVPanelStore  # noqa: E402
from assay.engine.engine import FactorEngine  # noqa: E402

TICKERS = ["AAA", "BBB", "CCC"]
SPLIT = "2025-01-02"   # AAA 2:1
DIV = "2024-12-16"     # BBB pays 1.0 on a 40.0 stock


def days(a, b):
    d, out = dt.date.fromisoformat(a), []
    while d <= dt.date.fromisoformat(b):
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += dt.timedelta(days=1)
    return out


def write_store(root, version="v000001", last="2025-01-24"):
    cal = days("2024-12-02", last)
    blocks = {}
    for year in sorted({d[:4] for d in cal}):
        rows = [d for d in cal if d.startswith(year)]
        folder = root / "blocks" / f"{year}-r1"
        folder.mkdir(parents=True, exist_ok=True)
        np.save(folder / "dates.npy", np.array(rows, dtype="datetime64[D]"))
        np.save(folder / "sids.npy", np.array([0, 1, 2], dtype=np.int32))
        close = np.array([[100.0 if d < SPLIT else 50.0, 40.0, 10.0 + i] for i, d in enumerate(rows)], dtype=np.float32)
        for f in ("open", "high", "low", "close"):
            np.save(folder / f"{f}.npy", close)
        np.save(folder / "volume.npy", np.full_like(close, 1000.0))
        np.save(folder / "transactions.npy", np.full_like(close, 10.0))
        blocks[year] = {"dir": f"blocks/{year}-r1", "rows": len(rows), "cols": 3}
    v = root / version
    v.mkdir(parents=True)
    np.save(v / "calendar.npy", np.array(cal, dtype="datetime64[D]"))
    pq.write_table(pa.table({"sid": pa.array([0, 1, 2], pa.int32()), "ticker": TICKERS, "label": TICKERS,
                             "kind": ["stock"] * 3, "first_date": [dt.date(2024, 12, 2)] * 3,
                             "last_date": [dt.date.fromisoformat(last)] * 3}), v / "securities.parquet")
    pq.write_table(pa.table({"ticker": TICKERS, "sid": pa.array([0, 1, 2], pa.int32()), "start": [cal[0]] * 3,
                             "end": [cal[-1]] * 3}), v / "tickers.parquet")
    pq.write_table(pa.table({
        "sid": pa.array([0, 1], pa.int32()), "ticker": ["AAA", "BBB"],
        "ex_date": [dt.date.fromisoformat(SPLIT), dt.date.fromisoformat(DIV)], "kind": ["split", "dividend"],
        "factor": [0.5, 1 - 1 / 40], "volume_factor": [2.0, 1.0], "applied": [True, True],
        "status": ["ok", "ok"]}), v / "events.parquet")
    pq.write_table(pa.table({"universe": ["top500", "top500"],
                             "effective_date": [dt.date(2024, 12, 2), dt.date(2025, 1, 2)],
                             "sids": pa.array([[0, 1], [1, 2]], pa.list_(pa.int32()))}), v / "universes.parquet")
    (v / "manifest.json").write_text(json.dumps({
        "format": "qfv-daily-panel/1", "first_date": cal[0], "last_date": cal[-1], "dates": len(cal),
        "securities": 3, "blocks": blocks, "universes": {"top500": {"size": 500}}, "built_at": "now"}))
    (root / "CURRENT").write_text(version + "\n")


@pytest.fixture
def store(tmp_path):
    write_store(tmp_path)
    return QFVPanelStore(tmp_path)


def test_prices_are_adjusted_to_the_window_end(store):
    d, syms, m = store.get_matrices(["close", "volume"], ["AAA"], "2024-12-20", "2025-01-10", "2025-01-10", "split")
    assert list(syms) == ["AAA"] and m["close"][0, 0] == 50.0 and m["volume"][0, 0] == 2000.0
    d, _, m = store.get_matrices(["close"], ["AAA"], "2024-12-20", "2025-01-10", "2024-12-31", "split")
    assert str(d[-1]) == "2024-12-31" and m["close"][0, 0] == 100.0         # as_of before the split
    _, _, m = store.get_matrices(["close"], ["BBB"], "2024-12-10", "2024-12-20", "2024-12-20", "total")
    assert m["close"][0, 0] == pytest.approx(39.0)
    frame = store.get_panel(["close"], ["AAA", "BBB"], "2025-01-02", "2025-01-03", "2025-01-03")
    assert frame.columns == ["date", "symbol", "close"] and frame.height == 4


def test_membership_is_point_in_time(store):
    assert store.get_universe("TOP500", "2024-12-20", "2024-12-20") == ["AAA", "BBB"]
    assert store.get_universe("TOP500", "2025-01-10", "2025-01-10") == ["BBB", "CCC"]
    assert store.universe_span("TOP500", "2024-12-02", "2025-01-24") == ["AAA", "BBB", "CCC"]
    dates = np.array(["2024-12-31", "2025-01-02"], dtype="datetime64[D]")
    m = store.get_membership("TOP500", dates, ["AAA", "BBB", "CCC"])
    assert m.tolist() == [[True, True, False], [False, True, True]]


def test_the_engine_scores_only_members_on_each_date(store):
    eng = FactorEngine.from_store(store, "TOP500", ("2024-12-02", "2025-01-24"), "2025-01-24")
    assert list(eng.symbols) == ["AAA", "BBB", "CCC"] and eng.snapshot == "qfv-daily-panel:v000001"
    v = eng.evaluate("ts_mean(close, 2)").values
    k = list(eng.dates.astype(str)).index(SPLIT)
    assert np.isfinite(v[k - 1, 0]) and np.isnan(v[k, 0])          # AAA leaves on the rebalance
    assert np.isnan(v[k - 1, 2]) and np.isfinite(v[k, 2])          # CCC joins, with its history in the window
    assert v[k, 2] == pytest.approx(eng.field_matrix("close")[k - 1:k + 1, 2].mean())
    [many] = eng.evaluate_many(["ts_mean(close, 2)"])
    assert np.array_equal(np.isnan(many.values), np.isnan(v))
    assert eng.panel_frame().height == 3 * len(eng.dates)


def test_a_new_version_is_picked_up(store, tmp_path):
    write_store(tmp_path, "v000002", last="2025-01-31")
    assert store.refresh(min_interval_s=0) and store.snapshot_id.endswith("v000002")
    assert store.price_span()["last_date"] == "2025-01-31"


def test_a_repeated_sid_is_adjusted_in_every_column(store):
    p = store.panel
    dates = p.calendar[(p.calendar >= np.datetime64("2024-12-20")) & (p.calendar <= np.datetime64("2025-01-10"))]
    price, volume = p._back_factors(dates, np.array([0, 1, 0], dtype=np.int32), ("split",))
    np.testing.assert_array_equal(price[:, 0], price[:, 2])
    assert price[0, 0] == 0.5 and volume[0, 2] == 2.0


def test_labels_map_back_to_their_own_security(tmp_path):
    # BBB's ticker was later used by sid 2 (now CCC): the label "BBB" still names sid 1,
    # while the ticker "BBB" on the last day means its latest user, sid 2
    write_store(tmp_path)
    v = tmp_path / "v000001"
    cal = days("2024-12-02", "2025-01-24")
    pq.write_table(pa.table({"ticker": ["AAA", "BBB", "BBB", "CCC"], "sid": pa.array([0, 1, 2, 2], pa.int32()),
                             "start": [cal[0], cal[0], "2024-12-23", "2025-01-06"],
                             "end": [cal[-1], "2024-12-20", "2025-01-03", cal[-1]]}), v / "tickers.parquet")
    p = QFVPanelStore(tmp_path).panel
    assert list(p.sids(["BBB"])) == [2]
    assert list(p.label_sids(["BBB", "CCC", "AAA", "ZZZ"])) == [1, 2, 0, -1]
