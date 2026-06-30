"""Tests for the library analytics helpers (Alpha-space MDS, lineage DAG, buckets).

Pure-function tests for :mod:`assay.library.analysis` — no engine or data — plus a
small lineage round-trip through the real expression parser.

    PYTHONPATH=src python -m pytest tests/library/test_analysis.py -q
"""

from __future__ import annotations

import numpy as np

from assay.library.analysis import bucket_periods, classical_mds, lineage_graph


# ---------------------------------------------------------------------------
# classical MDS
# ---------------------------------------------------------------------------
def test_classical_mds_recovers_2d_geometry():
    # four points on a square; MDS from their pairwise distances should preserve
    # the relative geometry (distances), up to rotation/reflection/translation.
    pts = np.array([[0.0, 0.0], [1.0, 0.0], [0.0, 1.0], [1.0, 1.0]])
    D = np.sqrt(((pts[:, None, :] - pts[None, :, :]) ** 2).sum(-1))
    emb = classical_mds(D, 2)
    assert emb.shape == (4, 2)
    De = np.sqrt(((emb[:, None, :] - emb[None, :, :]) ** 2).sum(-1))
    assert np.allclose(De, D, atol=1e-6)  # distances preserved


def test_classical_mds_degenerate_inputs():
    assert classical_mds(np.zeros((0, 0)), 2).shape == (0, 2)
    assert classical_mds(np.zeros((1, 1)), 2).shape == (1, 2)
    out = classical_mds(np.zeros((3, 3)), 2)  # all-zero distances -> finite layout
    assert out.shape == (3, 2) and np.all(np.isfinite(out))


# ---------------------------------------------------------------------------
# bucketing
# ---------------------------------------------------------------------------
def test_bucket_periods_monthly_and_quarterly():
    import datetime as dt

    dates = [dt.date(2021, 1, 5), dt.date(2021, 1, 20), dt.date(2021, 2, 3), dt.date(2021, 4, 1)]
    labels, groups = bucket_periods(dates, "month")
    assert labels == ["2021-01", "2021-02", "2021-04"]
    assert [g.tolist() for g in groups] == [[0, 1], [2], [3]]
    qlabels, _ = bucket_periods(dates, "quarter")
    assert qlabels == ["2021-Q1", "2021-Q2"]


# ---------------------------------------------------------------------------
# lineage DAG
# ---------------------------------------------------------------------------
def test_lineage_edge_on_subexpression_containment():
    # 'ts_mean(close, 5)' is a sub-expression of 'rank(ts_mean(close, 5))' -> an edge;
    # 'delta(close, 10)' shares nothing structural -> isolated.
    g = lineage_graph({
        "A": "ts_mean(close, 5)",
        "B": "rank(ts_mean(close, 5))",
        "C": "delta(close, 10)",
    })
    ids = {n["id"] for n in g["nodes"]}
    assert ids == {"A", "B", "C"}
    edges = {(e["from"], e["to"]) for e in g["edges"]}
    assert ("A", "B") in edges            # A is a component of B
    assert not any(e[0] == "C" or e[1] == "C" for e in edges)  # C isolated


def test_lineage_transitive_reduction():
    # close ⊂ ts_mean(close,5) ⊂ rank(ts_mean(close,5)): keep only the closest edges
    # (A->B, B->C), not the transitive A->C.
    g = lineage_graph({
        "A": "close",
        "B": "ts_mean(close, 5)",
        "C": "rank(ts_mean(close, 5))",
    })
    edges = {(e["from"], e["to"]) for e in g["edges"]}
    assert ("A", "B") in edges and ("B", "C") in edges
    assert ("A", "C") not in edges        # transitive edge reduced away


def test_lineage_unparseable_is_isolated_node():
    g = lineage_graph({"A": "this is not <valid", "B": "rank(close)"})
    assert {n["id"] for n in g["nodes"]} == {"A", "B"}
    assert g["edges"] == [] or all("A" not in (e["from"], e["to"]) for e in g["edges"])
