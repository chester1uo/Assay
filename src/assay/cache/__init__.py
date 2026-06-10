"""Assay cache layer (engineering-docs section 5).

Two correctness-first cache levels for :class:`AssayService` sessions:

* :class:`SessionCache` / :class:`SessionRegistry` — in-memory, per-session store
  of the aligned ``(T, N)`` field matrices and precomputed forward returns, plus a
  generic ``get_or_compute`` memo. Amortises panel load / pivot across every factor
  evaluated in a session (engineering-docs §5, §6.1).
* :class:`L2FactorCache` — cross-session, content-addressed on-disk cache of
  complete factor-result matrices, keyed by canonical expression + universe +
  period + adjustment + market (engineering-docs §5.4).

This is the simple, correct foundation; the L1 operator Arena / incremental
maintenance layer (engineering-docs §5.2-5.3) is a separate performance concern
built on top.
"""

from __future__ import annotations

from assay.cache.l2 import L2FactorCache
from assay.cache.session import SessionCache, SessionRegistry

__all__ = [
    "SessionCache",
    "SessionRegistry",
    "L2FactorCache",
]
