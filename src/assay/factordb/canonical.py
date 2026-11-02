"""Canonical form, identity and static properties of factor expressions.

* :func:`canonicalize` — commutative operators sort their arguments and integral
  float literals become ints, so ``close/open + 1`` and ``add(1, div(close, open))``
  are the same tree; :func:`factor_id` is the structural hash of that tree.
* :func:`separable` — whether a subtree is *column-separable*: built only from
  time-series and element-wise operators, so the value for one security depends on
  that security's own history and never on the universe.
* :func:`frontier` — the maximal column-separable subtrees of a factor: what the
  node store keeps on the global grid (the rest is cross-sectional work done per
  request).
* :func:`lookback` — how many past rows a node needs (``None``: unbounded, e.g.
  ``ts_ema``), for appending new dates.
* :func:`degree` — the node's degree ``(a, b)`` in the panel's price and volume
  adjustment factors, so a value stored on the store's last-day basis can be put on an
  earlier day's basis by dividing by ``P**a * V**b`` (``P``, ``V``: the price and volume
  factors of the corporate actions in between); ``None`` when it is not homogeneous.
"""

from __future__ import annotations

from assay.engine import operators
from assay.engine.ast import FieldNode, LitNode, OpNode, hash_tree

__all__ = ["canonicalize", "factor_id", "separable", "frontier", "lookback", "degree",
           "FIELD_DEGREE", "COMMUTATIVE"]

COMMUTATIVE = {"add", "mul", "elem_min", "elem_max", "eq", "ne", "and", "or"}

# operator categories whose value at (t, s) depends only on security s; anything else
# (cross-sectional, group, custom operators) is treated as coupling securities
_SEPARABLE_CATEGORIES = {"time-series", "arithmetic", "comparison", "logical", "math"}

# operators whose window is not the last argument (position of the window)
_WINDOW_POS = {"ts_quantile": 1}
# recurrences: the value depends on the whole history, not a finite window
_UNBOUNDED = {"ts_ema", "ts_dema"}

# (price-factor degree, volume-factor degree) of each panel field: adjusted prices are
# raw * P, adjusted volume is raw * V; dvol and ret are basis-free
FIELD_DEGREE: dict[str, tuple[int, int]] = {
    "open": (1, 0), "high": (1, 0), "low": (1, 0), "close": (1, 0), "vwap": (1, 0),
    "volume": (0, 1), "transactions": (0, 0), "dvol": (0, 0), "ret": (0, 0),
}
_KEEP = {"neg", "abs", "ts_delay", "ts_mean", "ts_sum", "ts_std", "ts_min", "ts_max", "ts_delta",
         "ts_med", "ts_mad", "ts_decay_linear", "ts_ema", "ts_dema", "ts_quantile", "fillna",
         "ts_slope", "ts_resi"}
_ZERO = {"ts_corr", "ts_rank", "ts_returns", "ts_log_returns", "ts_argmin", "ts_argmax", "ts_skew",
         "ts_kurt", "ts_count", "ts_rsquare", "sign", "sigmoid", "lt", "le", "gt", "ge", "eq", "ne",
         "and", "or", "not", "cs_rank", "cs_zscore", "cs_group_rank", "cs_winsorize"}
_SAME = {"add", "sub", "elem_min", "elem_max"}


def _lit(value):
    if isinstance(value, float) and value.is_integer() and abs(value) < 2 ** 53:
        return int(value)
    return value


def canonicalize(node):
    """The canonical tree of ``node`` (a new tree; ``node`` is not changed)."""
    if isinstance(node, LitNode):
        v = _lit(node.value)
        return node if v is node.value else LitNode(v)
    if isinstance(node, FieldNode):
        return node
    kids = tuple(canonicalize(c) for c in node.children)
    if node.op in COMMUTATIVE:
        hashes = hash_tree(OpNode(node.op, kids))
        kids = tuple(sorted(kids, key=lambda k: hashes[id(k)]))
    return OpNode(node.op, kids)


def factor_id(node) -> str:
    """Structural hash of the canonical tree: the factor's identity."""
    canon = canonicalize(node)
    return hash_tree(canon)[id(canon)]


def _row_coupled(op: str) -> bool:
    spec = operators.get(op)
    return spec.schema.get("category") not in _SEPARABLE_CATEGORIES or spec.needs_ctx


def separable(node) -> bool:
    """True if no operator in ``node`` couples securities (no cross-sectional/group op)."""
    if not isinstance(node, OpNode):
        return True
    return not _row_coupled(node.op) and all(separable(c) for c in node.children)


def frontier(node) -> list:
    """Maximal column-separable subtrees that contain an operator (leaves are read directly)."""
    if not isinstance(node, OpNode):
        return []
    if separable(node):
        return [node]
    out = []
    for c in node.children:
        out.extend(frontier(c))
    return out


def lookback(node) -> int | None:
    """Past rows (beyond the current one) the node needs; ``None`` if unbounded."""
    if not isinstance(node, OpNode):
        return 0
    deps = [lookback(c) for c in node.children if isinstance(c, OpNode | FieldNode)]
    if any(d is None for d in deps) or node.op in _UNBOUNDED:
        return None
    own = 0
    if node.op.startswith("ts_"):
        pos = _WINDOW_POS.get(node.op, len(node.children) - 1)
        w = node.children[pos] if pos < len(node.children) else None
        if isinstance(w, LitNode) and isinstance(w.value, int | float):
            own = max(int(w.value), 1)
            if node.op in ("ts_delay", "ts_delta", "ts_returns", "ts_log_returns"):
                own = int(w.value) + 1
        else:
            return None
    return own + max(deps, default=0)


def degree(node) -> tuple[int, int] | None:
    """(price, volume) degree of ``node``; ``None`` when it is not homogeneous."""
    if isinstance(node, LitNode):
        return (0, 0)
    if isinstance(node, FieldNode):
        return FIELD_DEGREE.get(node.name)
    op = node.op
    arrays = [c for c in node.children if not isinstance(c, LitNode)]
    degs = [degree(c) for c in arrays]
    if any(d is None for d in degs):
        return None
    if not degs:                      # literals only, e.g. a constant folded into an op
        return (0, 0)
    if op in _ZERO:
        return (0, 0)
    if op in _KEEP:
        return degs[0] if degs else (0, 0)
    if op in _SAME:
        return degs[0] if len(set(degs)) <= 1 else None
    if op == "where":
        branch = [degree(c) for c in node.children[1:]]
        return branch[0] if len(set(branch)) == 1 and branch[0] is not None else None
    if op == "mul":
        return (sum(d[0] for d in degs), sum(d[1] for d in degs))
    if op in ("div", "safe_div"):
        a, b = degree(node.children[0]), degree(node.children[1])
        if a is None or b is None:
            return None
        return (a[0] - b[0], a[1] - b[1])
    if op in ("ts_cov", "ts_var"):
        d = degs[0]
        if op == "ts_var":
            return (2 * d[0], 2 * d[1])
        e = degs[1] if len(degs) > 1 else d
        return (d[0] + e[0], d[1] + e[1])
    if op == "ts_product":
        w = node.children[-1]
        if isinstance(w, LitNode):
            return (degs[0][0] * int(w.value), degs[0][1] * int(w.value))
        return None
    if op in ("pow", "signed_power"):
        e = node.children[1]
        if isinstance(e, LitNode) and float(e.value).is_integer():
            return (degs[0][0] * int(e.value), degs[0][1] * int(e.value))
        return (0, 0) if degs[0] == (0, 0) else None
    if op in ("log", "sqrt", "clip"):
        return (0, 0) if degs[0] == (0, 0) else None
    if op == "cs_scale" or op == "cs_demean":
        return degs[0]
    return (0, 0) if all(d == (0, 0) for d in degs) else None
