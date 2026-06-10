"""Unified abstract syntax tree for factor expressions.

Both front-end syntaxes (qlib expression strings and the function-call dialect
that covers Assay-native ``ts_*``/``cs_*`` names *and* Alpha-101/WorldQuant
aliases) are parsed into the *same* node tree defined here, exactly as described
in engineering-docs section 4.1. The evaluator and operator registry have no
knowledge of which syntax produced a tree — equivalent expressions in different
dialects produce structurally identical trees (and identical :meth:`struct_hash`).

Three node kinds:

* :class:`FieldNode` — a leaf referencing a raw data field (``close``, ``volume``)
* :class:`LitNode`   — a leaf literal (a window length ``20``, a power ``0.5``,
  a group name ``"sector"``)
* :class:`OpNode`    — an operator application; its children are the argument
  nodes (array-valued operands *and* scalar parameters alike), so the tree is
  uniform and the canonical operator name is the single source of truth.

The nodes are frozen dataclasses so they are hashable and cheap to intern.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass


@dataclass(frozen=True)
class FieldNode:
    """Leaf node: a raw data field, e.g. ``close``, ``volume``, ``vwap``."""

    name: str

    def struct_hash(self) -> str:
        return _digest(f"field:{self.name}")

    def __str__(self) -> str:  # human-readable, round-trippable into func syntax
        return self.name


@dataclass(frozen=True)
class LitNode:
    """Leaf node: a literal value (window length, power, group name, ...)."""

    value: float | int | str

    def struct_hash(self) -> str:
        # Distinguish 20 (int) from 20.0 (float) from "20" (str) so trees that
        # differ only in literal type never collide.
        return _digest(f"lit:{type(self.value).__name__}:{self.value!r}")

    def __str__(self) -> str:
        return repr(self.value) if isinstance(self.value, str) else str(self.value)


@dataclass(frozen=True)
class OpNode:
    """Internal node: application of a canonical operator to child nodes.

    ``children`` holds the argument nodes in call order. Array-valued operands
    and scalar parameters (windows, powers, group names) are *all* children;
    the operator's registered kernel knows which positions are scalars.
    """

    op: str
    children: tuple

    def struct_hash(self) -> str:
        child_hashes = ",".join(c.struct_hash() for c in self.children)
        return _digest(f"op:{self.op}({child_hashes})")

    def __str__(self) -> str:
        args = ", ".join(str(c) for c in self.children)
        return f"{self.op}({args})"


def _digest(raw: str) -> str:
    """Stable 12-hex-char structural digest (matches engineering-docs 4.1)."""
    return hashlib.sha256(raw.encode()).hexdigest()[:12]


def iter_ops(node) -> set[str]:
    """Return the set of operator names used anywhere in ``node``.

    Used to attach only the relevant operator schemas to an agent-facing
    response (engineering-docs section 7.3) and to validate an expression
    against the registry before evaluation.
    """
    if isinstance(node, OpNode):
        out = {node.op}
        for child in node.children:
            out |= iter_ops(child)
        return out
    return set()


def iter_fields(node) -> set[str]:
    """Return the set of raw data fields referenced anywhere in ``node``."""
    if isinstance(node, FieldNode):
        return {node.name}
    if isinstance(node, OpNode):
        out: set[str] = set()
        for child in node.children:
            out |= iter_fields(child)
        return out
    return set()
