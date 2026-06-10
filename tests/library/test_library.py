"""Tests for the factor library store + correlation/redundancy helpers.

Covers engineering-docs section 7.2 (FactorReport store) and section 6
(CorrelationAnalyzer / redundancy management):

* :class:`FactorLibrary` on a ``tmp_path`` directory — empty store lists nothing,
  save/get round-trips, ``list`` filters + descending sort, ``delete``, and
  persistence across re-instantiation (the directory is the source of truth).
* the signed-Spearman helpers — ``factor_similarity(a, a)≈1`` / ``(a, -a)≈-1``,
  ``correlation_matrix`` diagonal=1 and symmetry, ``redundancy_score`` returns the
  ``abs``-argmax match, and ``prune`` drops the lower ``rank_icir`` factor of an
  over-threshold pair.

Offline only — no network or ingested data required. Run with::

    PYTHONPATH=src python -m pytest tests/library/test_library.py -q
"""

from __future__ import annotations

import numpy as np
import pytest

from assay.library import (
    FactorLibrary,
    FactorReport,
    Lineage,
    correlation_matrix,
    factor_similarity,
    prune,
    redundancy_score,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def make_report(
    expr: str,
    *,
    rank_icir: float = 1.0,
    rank_ic: float = 0.05,
    ic: float = 0.03,
    redundancy: float = 0.0,
    universe: str = "SP500",
    source: str = "AGENT",
) -> FactorReport:
    """A minimal-but-valid report; factor_id derived from the canonical expr hash."""
    return FactorReport(
        factor_id=FactorReport.compute_factor_id(expr),
        expr=expr,
        expr_canonical=expr,
        ic=ic,
        icir=0.5,
        rank_ic=rank_ic,
        rank_icir=rank_icir,
        redundancy_score=redundancy,
        universe_id=universe,
        lineage=Lineage(source=source),
    )


# ---------------------------------------------------------------------------
# FactorLibrary: empty store
# ---------------------------------------------------------------------------
def test_empty_library_lists_nothing(tmp_path):
    """A fresh directory yields an empty list and None for any get."""
    lib = FactorLibrary(tmp_path / "lib")
    assert lib.list() == []
    assert lib.all_reports() == []
    assert lib.get("nope") is None


def test_library_creates_directory(tmp_path):
    """The store mkdirs its path (with parents) on construction."""
    target = tmp_path / "a" / "b" / "lib"
    FactorLibrary(target)
    assert target.is_dir()


# ---------------------------------------------------------------------------
# save / get round-trip
# ---------------------------------------------------------------------------
def test_save_then_get_returns_equal_report(tmp_path):
    """get() loads back a report equal (by to_dict) to the saved one."""
    lib = FactorLibrary(tmp_path)
    r = make_report("ts_mean(close,5)")
    fid = lib.save(r)
    assert fid == r.factor_id
    got = lib.get(fid)
    assert got is not None
    assert got.to_dict() == r.to_dict()


def test_save_overwrites_in_place_by_id(tmp_path):
    """Re-saving the same canonical expr overwrites rather than duplicating."""
    lib = FactorLibrary(tmp_path)
    lib.save(make_report("close", rank_icir=1.0))
    lib.save(make_report("close", rank_icir=2.0))  # same id, new metric
    assert len(lib.list()) == 1
    fid = FactorReport.compute_factor_id("close")
    assert lib.get(fid).rank_icir == pytest.approx(2.0)


def test_save_without_factor_id_uses_canonical_hash(tmp_path):
    """A report with no factor_id is addressed by its canonical-expr hash."""
    lib = FactorLibrary(tmp_path)
    r = make_report("ts_std(close,10)")
    r.factor_id = ""  # force the fallback path
    fid = lib.save(r)
    assert fid == FactorReport.compute_factor_id("ts_std(close,10)")
    assert lib.get(fid) is not None


# ---------------------------------------------------------------------------
# list: filters + sort
# ---------------------------------------------------------------------------
def test_list_sorted_descending_by_rank_icir(tmp_path):
    """Default sort is descending by rank_icir."""
    lib = FactorLibrary(tmp_path)
    lib.save(make_report("a", rank_icir=0.5))
    lib.save(make_report("b", rank_icir=2.0))
    lib.save(make_report("c", rank_icir=1.0))
    rows = lib.list()
    assert [s.rank_icir for s in rows] == [2.0, 1.0, 0.5]
    assert [s.expr for s in rows] == ["b", "c", "a"]


def test_list_min_rank_icir_filter(tmp_path):
    """min_rank_icir keeps only rows at/above the floor."""
    lib = FactorLibrary(tmp_path)
    lib.save(make_report("a", rank_icir=0.2))
    lib.save(make_report("b", rank_icir=1.5))
    rows = lib.list(min_rank_icir=1.0)
    assert [s.expr for s in rows] == ["b"]


def test_list_max_redundancy_filter(tmp_path):
    """max_redundancy drops rows whose redundancy_score exceeds the ceiling."""
    lib = FactorLibrary(tmp_path)
    lib.save(make_report("a", redundancy=0.9))
    lib.save(make_report("b", redundancy=0.1))
    rows = lib.list(max_redundancy=0.5)
    assert [s.expr for s in rows] == ["b"]


def test_list_universe_and_source_filters(tmp_path):
    """universe / source are exact-match conjunctive filters."""
    lib = FactorLibrary(tmp_path)
    lib.save(make_report("a", universe="SP500", source="AGENT"))
    lib.save(make_report("b", universe="R3000", source="AGENT"))
    lib.save(make_report("c", universe="SP500", source="HUMAN"))
    assert {s.expr for s in lib.list(universe="SP500")} == {"a", "c"}
    assert {s.expr for s in lib.list(source="HUMAN")} == {"c"}
    assert {s.expr for s in lib.list(universe="SP500", source="AGENT")} == {"a"}


def test_list_limit_and_offset_pagination(tmp_path):
    """limit/offset page the descending-sorted result; limit<0 lifts the cap."""
    lib = FactorLibrary(tmp_path)
    for i in range(5):
        lib.save(make_report(f"f{i}", rank_icir=float(i)))
    # descending: f4,f3,f2,f1,f0
    assert [s.expr for s in lib.list(limit=2)] == ["f4", "f3"]
    assert [s.expr for s in lib.list(limit=2, offset=2)] == ["f2", "f1"]
    assert len(lib.list(limit=-1)) == 5


def test_list_sort_by_alternate_metric(tmp_path):
    """sort_by can target any numeric summary attribute (descending)."""
    lib = FactorLibrary(tmp_path)
    lib.save(make_report("a", rank_icir=1.0, rank_ic=0.9))
    lib.save(make_report("b", rank_icir=2.0, rank_ic=0.1))
    rows = lib.list(sort_by="rank_ic")
    assert [s.expr for s in rows] == ["a", "b"]


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------
def test_delete_removes_from_disk_and_index(tmp_path):
    """delete() unlinks the file, drops the index row, and returns the count."""
    lib = FactorLibrary(tmp_path)
    fid_a = lib.save(make_report("a"))
    lib.save(make_report("b"))
    assert lib.delete(fid_a) == 1
    assert lib.get(fid_a) is None
    assert {s.expr for s in lib.list()} == {"b"}
    assert not (tmp_path / f"{fid_a}.json").exists()


def test_delete_missing_returns_zero(tmp_path):
    """Deleting an unknown id is a no-op returning 0."""
    lib = FactorLibrary(tmp_path)
    assert lib.delete("ghost") == 0


def test_delete_accepts_list(tmp_path):
    """A list of ids deletes each present one and counts only those removed."""
    lib = FactorLibrary(tmp_path)
    fa = lib.save(make_report("a"))
    fb = lib.save(make_report("b"))
    assert lib.delete([fa, fb, "ghost"]) == 2
    assert lib.list() == []


# ---------------------------------------------------------------------------
# persistence across re-instantiation
# ---------------------------------------------------------------------------
def test_persistence_across_reinstantiation(tmp_path):
    """A new FactorLibrary on the same dir re-scans and recovers the saved rows."""
    lib = FactorLibrary(tmp_path)
    r = make_report("ts_rank(close,20)", rank_icir=1.7)
    fid = lib.save(r)

    lib2 = FactorLibrary(tmp_path)  # rebuild index by scanning *.json
    assert [s.factor_id for s in lib2.list()] == [fid]
    got = lib2.get(fid)
    assert got is not None
    assert got.to_dict() == r.to_dict()
    assert lib2.list()[0].rank_icir == pytest.approx(1.7)


# ===========================================================================
# correlation / redundancy helpers
# ===========================================================================

# A non-trivial (T, N) factor with varied within-date orderings.
_A = np.array(
    [
        [1.0, 2.0, 3.0, 4.0],
        [4.0, 3.0, 2.0, 1.0],
        [1.0, 3.0, 2.0, 4.0],
        [2.0, 1.0, 4.0, 3.0],
    ]
)


def test_factor_similarity_self_is_one():
    """A factor correlates with itself at +1 (rank ordering identical each date)."""
    assert factor_similarity(_A, _A) == pytest.approx(1.0)


def test_factor_similarity_negation_is_minus_one():
    """A factor vs its negation is the exactly-reversed ordering -> -1 (signed)."""
    assert factor_similarity(_A, -_A) == pytest.approx(-1.0)


def test_factor_similarity_monotone_rescale_is_one():
    """Spearman is invariant to a positive monotone rescaling (3x + 1 -> still +1)."""
    assert factor_similarity(_A, 3.0 * _A + 1.0) == pytest.approx(1.0)


def test_factor_similarity_nan_aware_no_poison():
    """A missing symbol on one date does not poison that date or the overall score."""
    b = _A.copy()
    b[0, 0] = np.nan  # drop one symbol on date 0; rest of ordering preserved
    # b is still a monotone copy of _A on every jointly-present cell -> +1
    assert factor_similarity(_A, b) == pytest.approx(1.0)


def test_factor_similarity_shape_mismatch_raises():
    """Mismatched shapes are a programming error, not silently coerced."""
    with pytest.raises(ValueError):
        factor_similarity(_A, _A[:, :2])


def test_correlation_matrix_diag_one_and_symmetric():
    """Diagonal pinned to 1.0; off-diagonal symmetric; ids in insertion order."""
    cm = correlation_matrix({"x": _A, "y": 2.0 * _A + 1.0, "z": -_A})
    assert cm["factor_ids"] == ["x", "y", "z"]
    m = cm["matrix"]
    n = len(m)
    for i in range(n):
        assert m[i][i] == pytest.approx(1.0)  # diagonal
        for j in range(n):
            assert m[i][j] == pytest.approx(m[j][i])  # symmetric
    assert m[0][1] == pytest.approx(1.0)   # x vs monotone-rescaled x
    assert m[0][2] == pytest.approx(-1.0)  # x vs negation


def test_correlation_matrix_empty():
    """Empty input yields empty axes."""
    cm = correlation_matrix({})
    assert cm["factor_ids"] == []
    assert cm["matrix"] == []


def test_redundancy_score_is_abs_argmax():
    """redundancy_score = (max |similarity|, argmax id); negation counts as redundant."""
    others = {
        "dup": 5.0 * _A,          # |sim| = 1.0  (positive duplicate)
        "neg": -_A,               # |sim| = 1.0  (negated duplicate, also redundant)
        "noise": np.zeros_like(_A),  # constant -> undefined -> 0.0 contribution
    }
    score, fid = redundancy_score(_A, others)
    assert score == pytest.approx(1.0)
    assert fid in {"dup", "neg"}  # both are abs-similarity 1.0; first-seen wins ties


def test_redundancy_score_empty_others():
    """No library factors -> (0.0, None)."""
    assert redundancy_score(_A, {}) == (0.0, None)


def test_redundancy_score_picks_strongest():
    """When matches differ, the strongest absolute similarity is returned."""
    weak = _A.copy()
    weak[:, [0, 1]] = weak[:, [1, 0]]  # swap two columns -> weaker correlation
    score, fid = redundancy_score(_A, {"weak": weak, "strong": 2.0 * _A})
    assert fid == "strong"
    assert score == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# prune
# ---------------------------------------------------------------------------
def test_prune_drops_lower_rank_icir_of_over_threshold_pair():
    """Of an over-threshold pair, the lower-quality (rank_icir) factor is dropped."""
    matrix = [
        [1.0, 0.95, 0.10],
        [0.95, 1.0, 0.20],
        [0.10, 0.20, 1.0],
    ]
    ids = ["x", "y", "z"]
    scores = {"x": 2.0, "y": 1.0, "z": 0.5}  # x beats y on the correlated pair
    res = prune(matrix, ids, scores, threshold=0.7)
    assert res["pairs_over_threshold"] == 1
    assert res["would_delete"] == ["y"]
    assert res["kept"] == ["x", "z"]


def test_prune_uses_absolute_correlation():
    """A strongly *anti*-correlated pair (|corr| >= thr) is still pruned."""
    matrix = [[1.0, -0.9], [-0.9, 1.0]]
    res = prune(matrix, ["a", "b"], {"a": 1.0, "b": 0.0}, threshold=0.7)
    assert res["pairs_over_threshold"] == 1
    assert res["would_delete"] == ["b"]
    assert res["kept"] == ["a"]


def test_prune_tie_breaks_on_factor_id():
    """Equal scores -> the lexicographically larger id is dropped (deterministic)."""
    matrix = [[1.0, 0.99], [0.99, 1.0]]
    res = prune(matrix, ["a", "b"], {"a": 1.0, "b": 1.0}, threshold=0.7)
    assert res["would_delete"] == ["b"]
    assert res["kept"] == ["a"]


def test_prune_nothing_over_threshold():
    """No pair over threshold -> nothing deleted, full set kept."""
    matrix = [[1.0, 0.1], [0.1, 1.0]]
    res = prune(matrix, ["a", "b"], {"a": 1.0, "b": 2.0}, threshold=0.7)
    assert res["pairs_over_threshold"] == 0
    assert res["would_delete"] == []
    assert res["kept"] == ["a", "b"]


def test_prune_partition_preserves_order():
    """would_delete and kept partition the inputs, preserving the input ordering."""
    matrix = [
        [1.0, 0.9, 0.9],
        [0.9, 1.0, 0.9],
        [0.9, 0.9, 1.0],
    ]
    ids = ["a", "b", "c"]
    scores = {"a": 3.0, "b": 2.0, "c": 1.0}  # a dominates -> b, c dropped
    res = prune(matrix, ids, scores, threshold=0.7)
    assert res["kept"] == ["a"]
    assert res["would_delete"] == ["b", "c"]
    # partition: every id appears exactly once across the two lists
    assert sorted(res["kept"] + res["would_delete"]) == sorted(ids)
