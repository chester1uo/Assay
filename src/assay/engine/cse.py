"""Common sub-expression analysis over a corpus of factor expressions (§4.3).

When many factors are evaluated together (a sweep, a library re-score, a batch
backtest) they share **sub-expressions** — ``sub(high, low)``, ``ts_mean(volume,
20)``, ``ts_delay(close, 1)`` recur in thousands of distinct factors. Computing
each one once and reusing the result (common sub-expression elimination, CSE) is
the single biggest lever on batch throughput.

This module is the *analysis* half: parse a corpus, walk every AST, and rank the
sub-expressions by how much recomputation reusing them would save. Every AST node
already carries a stable structural digest (:meth:`assay.engine.ast.OpNode.struct_hash`),
so two structurally identical subtrees — regardless of the surface syntax that
produced them — share a hash and are counted together.

The *execution* half (memoised batch evaluation + the on-disk precompute store
that materialises the winners for every asset) lives in
:mod:`assay.engine.precompute` and :meth:`assay.engine.FactorEngine.evaluate_many`.

Pure: parses expressions and walks trees; never touches data or the engine's
numerics.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Iterable, Iterator

from assay.engine.ast import OpNode
from assay.engine.parsing import parse

__all__ = ["CommonSubexpr", "iter_subtrees", "node_size", "common_subexpressions"]


@dataclass(frozen=True)
class CommonSubexpr:
    """One recurring sub-expression and the recompute it would save.

    ``score`` is the savings proxy ``count * (n_nodes - 1)`` — how many operator
    evaluations are avoided across the corpus by computing this subtree once and
    reusing it everywhere it occurs (a 1-node leaf saves nothing, so larger,
    more-frequent subtrees rank highest).
    """

    struct_hash: str
    expr: str          # canonical re-parseable form
    count: int         # total occurrences across the corpus (incl. repeats in one factor)
    n_factors: int     # number of distinct factors that contain it
    n_nodes: int       # operator/leaf count of the subtree
    score: int         # count * (n_nodes - 1) — operator-evals saved by reuse

    def to_dict(self) -> dict:
        return {
            "struct_hash": self.struct_hash, "expr": self.expr, "count": self.count,
            "n_factors": self.n_factors, "n_nodes": self.n_nodes, "score": self.score,
        }


def iter_subtrees(node) -> Iterator[OpNode]:
    """Yield every :class:`OpNode` subtree of ``node`` (itself included; leaves skipped).

    Leaf nodes (fields / literals) are not yielded — they cost nothing to "compute",
    so they are never worth caching.
    """
    if isinstance(node, OpNode):
        yield node
        for child in node.children:
            yield from iter_subtrees(child)


def node_size(node) -> int:
    """Total node count of a subtree (operators + leaves) — a cheap cost proxy."""
    kids = getattr(node, "children", ()) or ()
    return 1 + sum(node_size(c) for c in kids)


def common_subexpressions(
    exprs: Iterable[str],
    *,
    min_count: int = 2,
    min_nodes: int = 2,
    top_k: int | None = None,
) -> list[CommonSubexpr]:
    """Rank the reusable sub-expressions of a factor corpus by recompute saved.

    Parses each expression (silently skipping any that fail to parse), walks its
    AST, and aggregates every operator subtree by :meth:`OpNode.struct_hash`. A
    subtree qualifies when it appears at least ``min_count`` times across the corpus
    and has at least ``min_nodes`` nodes (a single operator over leaves is the
    smallest worth caching). The result is sorted by ``score`` (recompute saved)
    descending, ``n_nodes`` then ``struct_hash`` breaking ties for determinism;
    ``top_k`` truncates to the strongest winners.

    Returns a list of :class:`CommonSubexpr`. ``count`` totals every occurrence
    (including repeats inside one factor); ``n_factors`` counts distinct factors —
    so a subtree used once each in 500 factors and one used 500 times in a single
    factor are distinguishable.
    """
    occ: Counter[str] = Counter()        # total occurrences
    fac: Counter[str] = Counter()        # distinct factors containing it
    rep: dict[str, str] = {}             # representative canonical string
    size: dict[str, int] = {}            # node count

    for raw in exprs:
        text = raw if isinstance(raw, str) else str(raw)
        try:
            root = parse(text)
        except Exception:  # noqa: BLE001 — a malformed corpus line is simply skipped
            continue
        seen_here: set[str] = set()
        for st in iter_subtrees(root):
            h = st.struct_hash()
            occ[h] += 1
            if h not in rep:
                rep[h] = str(st)
                size[h] = node_size(st)
            seen_here.add(h)
        for h in seen_here:
            fac[h] += 1

    out: list[CommonSubexpr] = []
    for h, c in occ.items():
        n_nodes = size[h]
        if c < min_count or n_nodes < min_nodes:
            continue
        out.append(CommonSubexpr(
            struct_hash=h, expr=rep[h], count=c, n_factors=fac[h],
            n_nodes=n_nodes, score=c * (n_nodes - 1),
        ))

    out.sort(key=lambda s: (-s.score, -s.n_nodes, s.struct_hash))
    return out[:top_k] if top_k is not None else out
