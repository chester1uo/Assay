"""Factor-library analytics — the compute behind the WebUI's three advanced views.

Pure, dependency-light helpers (numpy + the engine parser) that power three
library visualisations beyond the correlation matrix:

* :func:`classical_mds` — project a distance matrix to 2-D for the **Alpha space
  map** (a similarity scatter of every factor; clusters of redundant alphas fall
  together). Classical (Torgerson) MDS via an eigendecomposition — deterministic
  and dependency-free, so it always works; the service may swap in t-SNE / UMAP
  when those libraries are installed.
* :func:`lineage_graph` — build the **lineage DAG** from the factors' expression
  ASTs: an edge ``a -> b`` means factor ``a``'s whole expression appears as a
  sub-expression of ``b`` (so ``b`` is *derived from* ``a``). Transitively reduced
  to a clean Hasse diagram.

The IC-heatmap bucketing lives in the service (it needs the engine + forward
returns); only the two engine-free pieces live here so they stay unit-testable in
isolation.
"""

from __future__ import annotations

import datetime as _dt

import numpy as np

__all__ = ["classical_mds", "lineage_graph", "bucket_periods"]


# ---------------------------------------------------------------------------
# IC heatmap — calendar bucketing of the date axis
# ---------------------------------------------------------------------------
def bucket_periods(dates, bucket: str = "month") -> tuple[list[str], list[np.ndarray]]:
    """Group an ascending date axis into calendar buckets for the IC heatmap.

    Returns ``(labels, groups)`` where ``labels[k]`` is the period label
    (``'YYYY-MM'`` monthly, ``'YYYY-Qn'`` quarterly, ``'YYYY-Www'`` ISO-weekly) and
    ``groups[k]`` the integer indices of the dates falling in it. Assumes ``dates``
    is sorted ascending (the engine's date axis is). Tolerant of numpy
    ``datetime64`` / ``datetime.date`` / ISO strings.
    """
    b = (bucket or "month").lower()

    def _label(d) -> str:
        s = str(d)[:10]  # 'YYYY-MM-DD'
        y, m = s[:4], s[5:7]
        if b == "quarter":
            q = (int(m) - 1) // 3 + 1 if m.isdigit() else 1
            return f"{y}-Q{q}"
        if b == "week":
            try:
                iso = _dt.date.fromisoformat(s).isocalendar()
                return f"{iso[0]}-W{iso[1]:02d}"
            except ValueError:
                return f"{y}-{m}"
        return f"{y}-{m}"  # month (default)

    labels: list[str] = []
    groups: list[list[int]] = []
    cur: str | None = None
    for i, d in enumerate(dates):
        lab = _label(d)
        if lab != cur:
            labels.append(lab)
            groups.append([])
            cur = lab
        groups[-1].append(i)
    return labels, [np.asarray(g, dtype=np.int64) for g in groups]


# ---------------------------------------------------------------------------
# Alpha space map — classical MDS
# ---------------------------------------------------------------------------
def classical_mds(dist: np.ndarray, dims: int = 2) -> np.ndarray:
    """Classical multidimensional scaling of an ``(n, n)`` distance matrix → ``(n, dims)``.

    Torgerson MDS: double-centre the squared-distance matrix into the Gram matrix
    ``B = -½ J D² J`` and take the top-``dims`` eigenvectors scaled by ``√eigenvalue``.
    Deterministic and dependency-free. Degenerate inputs (``n <= dims``, all-zero or
    non-finite distances) fall back to a spread-out line so the caller always gets a
    finite ``(n, dims)`` layout.
    """
    d = np.asarray(dist, dtype=np.float64)
    n = d.shape[0]
    if n == 0:
        return np.zeros((0, dims), dtype=np.float64)
    if n <= dims:
        out = np.zeros((n, dims), dtype=np.float64)
        out[:, 0] = np.arange(n, dtype=np.float64)
        return out
    d = np.where(np.isfinite(d), d, 0.0)
    d = 0.5 * (d + d.T)  # symmetrise
    d2 = d ** 2
    j = np.eye(n) - np.ones((n, n)) / n
    b = -0.5 * j @ d2 @ j
    b = 0.5 * (b + b.T)
    try:
        w, v = np.linalg.eigh(b)
    except np.linalg.LinAlgError:  # pragma: no cover - defensive
        out = np.zeros((n, dims), dtype=np.float64)
        out[:, 0] = np.arange(n, dtype=np.float64)
        return out
    order = np.argsort(w)[::-1][:dims]
    lam = np.clip(w[order], 0.0, None)
    x = v[:, order] * np.sqrt(lam)
    if x.shape[1] < dims:  # too few positive eigenvalues — pad with zeros
        x = np.hstack([x, np.zeros((n, dims - x.shape[1]))])
    return np.asarray(x, dtype=np.float64)


# ---------------------------------------------------------------------------
# Lineage DAG — expression-AST containment
# ---------------------------------------------------------------------------
def _all_subtree_strs(node) -> set[str]:
    """Canonical strings of every node in an AST subtree (root included)."""
    out = {str(node)}
    for child in getattr(node, "children", ()) or ():
        out |= _all_subtree_strs(child)
    return out


def _ast_depth(node) -> int:
    """Height of the AST (a leaf is depth 1)."""
    kids = getattr(node, "children", ()) or ()
    return 1 + max((_ast_depth(c) for c in kids), default=0)


def lineage_graph(id_to_expr: dict[str, str]) -> dict:
    """Build the derivation DAG of a set of factors from their expression ASTs.

    ``id_to_expr`` maps each factor id to its expression. An edge ``a -> b`` is
    emitted when factor ``a``'s *entire* canonical expression occurs as a proper
    sub-expression of ``b`` (``b`` is derived from / contains ``a``). The relation is
    a partial order; the returned edges are its **transitive reduction** (the Hasse
    diagram), so only the closest derivations are drawn.

    Returns ``{"nodes": [{id, expr, depth, n_ops, ops, fields}], "edges": [{from, to}]}``.
    Unparseable expressions become isolated nodes (no edges) rather than failing.
    """
    from assay.engine import parse
    from assay.engine.ast import iter_fields, iter_ops

    canon: dict[str, str] = {}
    subtrees: dict[str, set[str]] = {}
    nodes: list[dict] = []
    for fid, expr in id_to_expr.items():
        try:
            root = parse(expr)
            c = str(root)
            subs = _all_subtree_strs(root)
            depth = _ast_depth(root)
            ops = sorted(iter_ops(root))
            fields = sorted(iter_fields(root))
        except Exception:  # noqa: BLE001 — a bad expression is just an isolated node
            c, subs, depth, ops, fields = (str(expr), {str(expr)}, 0, [], [])
        canon[fid] = c
        subtrees[fid] = subs
        nodes.append({
            "id": fid, "expr": c, "depth": int(depth),
            "n_ops": len(ops), "ops": ops, "fields": fields,
        })

    ids = list(id_to_expr)

    def _contains(parent: str, child: str) -> bool:
        # child's whole expression is a sub-expression of parent (and they differ)
        return canon[child] != canon[parent] and canon[child] in subtrees[parent]

    # Raw containment edges child -> parent (child is a component of parent).
    raw: set[tuple[str, str]] = set()
    for b in ids:
        for a in ids:
            if _contains(b, a):
                raw.add((a, b))

    # Transitive reduction: drop a->b if some c gives a->c and c->b.
    edges: list[dict] = []
    for (a, b) in raw:
        redundant = any(
            (a, c) in raw and (c, b) in raw and c not in (a, b) for c in ids
        )
        if not redundant:
            edges.append({"from": a, "to": b})

    return {"nodes": nodes, "edges": edges}
