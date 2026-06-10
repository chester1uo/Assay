"""Factor library package — persistence and the report contract.

This package owns two things:

* the :class:`FactorReport` / :class:`FactorSummary` / :class:`Lineage` schema
  (engineering-docs section 7.2) — the machine-readable protocol the agent loop and
  WebUI consume after every evaluation; defined here in :mod:`assay.library.report`;
* (Phase-2, added by the LIBRARY agent) :class:`FactorLibrary` plus the
  correlation/redundancy helpers — the append-only store of evaluated factors and
  the Spearman-based redundancy lookup that powers ``redundancy_score`` and
  ``assay library prune``.

The re-exports below are the stable public surface every downstream module imports
from ``assay.library``.
"""

from __future__ import annotations

from assay.library.correlation import (
    correlation_matrix,
    factor_similarity,
    prune,
    redundancy_score,
)
from assay.library.report import FactorReport, FactorSummary, Lineage
from assay.library.store import FactorLibrary

__all__ = [
    "FactorReport",
    "FactorSummary",
    "Lineage",
    "FactorLibrary",
    "factor_similarity",
    "correlation_matrix",
    "redundancy_score",
    "prune",
]
