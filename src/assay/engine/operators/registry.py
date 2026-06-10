"""The operator registry: ``OpSpec`` and registration / lookup API.

Every operator — built-in or user-defined — lives in a single process-wide
registry keyed by its canonical name. Built-in kernels register themselves at
import time (see the category modules); users register their own with
:func:`register` or the :func:`op` decorator (see the package docstring for an
example). The parser resolves any registered name, so a custom operator becomes
usable in factor expressions immediately.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable


@dataclass(frozen=True)
class OpSpec:
    """A registered operator: its kernel, arity bounds and agent-facing schema.

    The kernel receives already-evaluated arguments (``(T, N)`` matrices for
    array operands, python scalars for literal parameters) and returns a
    ``(T, N)`` matrix. If ``needs_ctx`` is true it additionally receives the
    evaluation context as a keyword ``ctx`` (used by group operators to resolve
    industry labels).
    """

    name: str
    fn: Callable
    min_args: int
    max_args: int
    schema: dict = field(default_factory=dict)
    needs_ctx: bool = False

    def check_arity(self, n: int) -> None:
        if not (self.min_args <= n <= self.max_args):
            want = (
                f"{self.min_args}"
                if self.min_args == self.max_args
                else f"{self.min_args}-{self.max_args}"
            )
            raise ValueError(f"operator {self.name!r} takes {want} argument(s), got {n}")


_REGISTRY: dict[str, OpSpec] = {}


def register(name, fn, min_args, max_args, *, needs_ctx=False, **schema) -> Callable:
    """Register operator ``name`` with kernel ``fn`` and arity ``[min, max]``.

    ``schema`` keyword args become the machine-readable operator schema
    (``signature``, ``category``, ``output_range``, ...). Returns ``fn`` so it
    can be used as a plain function decorator if preferred. Re-registering an
    existing name overrides it.
    """
    _REGISTRY[name] = OpSpec(name, fn, min_args, max_args, schema=schema, needs_ctx=needs_ctx)
    return fn


def op(name, min_args, max_args, *, needs_ctx=False, **schema):
    """Decorator form of :func:`register`.

    ::

        @op("ts_zscore", 2, 2, category="custom", output_range="(-inf, inf)")
        def ts_zscore(x, d):
            return (x - ts_mean(x, d)) / ts_std(x, d)
    """

    def decorator(fn):
        register(name, fn, min_args, max_args, needs_ctx=needs_ctx, **schema)
        return fn

    return decorator


def get(name: str) -> OpSpec:
    try:
        return _REGISTRY[name]
    except KeyError:
        raise KeyError(f"unknown operator {name!r}; not in the operator registry") from None


def is_registered(name: str) -> bool:
    return name in _REGISTRY


def unregister(name: str) -> None:
    """Remove a (typically custom) operator from the registry, if present."""
    _REGISTRY.pop(name, None)


def all_specs() -> dict[str, OpSpec]:
    """A snapshot ``{name: OpSpec}`` of every registered operator (incl. custom)."""
    return dict(_REGISTRY)


def operator_schema() -> dict[str, dict]:
    """A live ``{name: schema}`` view for agent prompt injection (incl. custom ops)."""
    return {
        name: {"signature": spec.schema.get("signature", name), **spec.schema}
        for name, spec in _REGISTRY.items()
    }
