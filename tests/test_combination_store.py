"""Unit tests for the saved-combination store (reloadable combination jobs)."""

from __future__ import annotations

import pytest

from assay.library.combination_store import LAST_RUN_ID, CombinationStore

pytestmark = pytest.mark.library


def _result(method="ridge", icir=0.21):
    return {
        "method": method, "standardize": "zscore", "horizon": 5,
        "factor_names": ["a", "b"], "weights": {"a": 0.4, "b": 0.6},
        "weight_kind": "weight", "orientation": {"a": 1, "b": 1},
        "per_factor_train_ic": {"a": 0.03, "b": 0.05},
        "train": {"icir": 0.5}, "val": {"icir": 0.3}, "test": {"icir": icir, "rank_icir": icir + 0.01},
        "universe": "NASDAQ100", "splits": {"train": ["2018-01-01", "2018-12-31"]},
        "resolved_factors": [{"name": "a", "expr": "rank(close)"}],
    }


def test_save_list_get_delete(tmp_path):
    store = CombinationStore(tmp_path)
    summ = store.save(_result(), name="my blend")
    assert summ["id"].startswith("cmb_") and summ["name"] == "my blend"
    assert summ["test_icir"] == pytest.approx(0.21) and summ["n_factors"] == 2

    rows = store.list()
    assert [r["id"] for r in rows] == [summ["id"]]

    rec = store.get(summ["id"])
    assert rec["result"]["weights"] == {"a": 0.4, "b": 0.6}   # the fitted model round-trips

    assert store.delete(summ["id"]) == 1
    assert store.get(summ["id"]) is None and store.list() == []


def test_last_run_is_pinned_first_and_overwrites(tmp_path):
    store = CombinationStore(tmp_path)
    store.save(_result(icir=0.1), name="(last run)", record_id=LAST_RUN_ID)
    store.save(_result(icir=0.2), name="(last run)", record_id=LAST_RUN_ID)  # overwrite
    store.save(_result(), name="perm")

    rows = store.list()
    assert rows[0]["is_last"] is True                    # last run pinned first
    assert sum(1 for r in rows if r["is_last"]) == 1     # single rolling record
    assert store.get(LAST_RUN_ID)["result"]["test"]["icir"] == pytest.approx(0.2)

    # excluding the last run leaves only the permanent save
    assert [r["name"] for r in store.list(include_last=False)] == ["perm"]


def test_reindex_from_disk(tmp_path):
    CombinationStore(tmp_path).save(_result(), name="persisted")
    reopened = CombinationStore(tmp_path)   # fresh instance rebuilds index from files
    assert [r["name"] for r in reopened.list()] == ["persisted"]


def test_summary_survives_missing_metrics(tmp_path):
    store = CombinationStore(tmp_path)
    bad = _result()
    bad["test"] = {"icir": float("nan")}    # NaN must not crash the summary
    summ = store.save(bad, name="nan test")
    assert summ["test_icir"] is None
