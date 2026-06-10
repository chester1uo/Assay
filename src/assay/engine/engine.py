"""Factor execution engine: evaluate a parsed expression over a price panel.

The engine takes a long, point-in-time panel (the output of
:meth:`assay.data.store.DataStore.get_panel` — columns ``date``, ``symbol`` and
one column per field) and pivots it into aligned ``(T, N)`` float matrices, one
per referenced field. A factor expression is then evaluated by walking its AST
(:mod:`assay.engine.ast`) and applying the registered numpy kernels
(:mod:`assay.engine.operators`), producing a ``(T, N)`` factor matrix on the
same ``date``/``symbol`` axes — the array model of engineering-docs sections 4-6.

This is the cold path of a single-factor evaluation. Batch DAG/CSE execution and
the two-level cache (engineering-docs sections 4.3-5) are a separate performance
layer built on top of this; the kernels here are the correctness foundation.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import polars as pl

from assay.engine import operators
from assay.engine.ast import FieldNode, LitNode, OpNode, iter_fields, iter_ops
from assay.engine.parsing import ParseError, parse


class EvaluationError(ValueError):
    """A kernel raised while evaluating an operator.

    Subclasses ``ValueError`` (message preserved) and additionally records the
    operator name ``op`` so diagnostics can point at it in the expression.
    """

    def __init__(self, message: str, *, op: str | None = None, cause: str | None = None):
        super().__init__(message)
        self.message = message
        self.op = op
        self.cause = cause  # original exception type name (e.g. 'ValueError')


@dataclass
class EvalContext:
    """Aligned matrices + axes + optional group labels for one evaluation."""

    dates: list
    symbols: list[str]
    matrices: dict[str, np.ndarray]
    groups: dict[str, np.ndarray]  # group key -> (N,) per-symbol label array

    def require_groups(self, key: str) -> np.ndarray:
        try:
            return self.groups[key]
        except KeyError:
            have = sorted(self.groups) or "none"
            raise ValueError(
                f"operator needs group data {key!r} but the engine has no such "
                f"grouping (configured: {have}). Pass group_data= to FactorEngine "
                f"(e.g. sector labels per symbol)."
            ) from None


@dataclass
class FactorResult:
    """A computed factor matrix on aligned ``date`` (rows) / ``symbol`` (cols) axes."""

    expr: str
    values: np.ndarray  # (T, N) float64
    dates: list
    symbols: list[str]

    @property
    def shape(self) -> tuple[int, int]:
        return self.values.shape

    def __array__(self, dtype=None):
        return self.values if dtype is None else self.values.astype(dtype)

    def to_frame(self) -> pl.DataFrame:
        """Return the factor as a long ``(date, symbol, factor)`` DataFrame.

        The ``date`` column is a proper ``pl.Date`` (so the frame can be sorted,
        joined and pivoted), not a numpy ``object`` column.
        """
        t, n = self.values.shape
        dates = pl.Series("date", [d for d in self.dates for _ in range(n)]).cast(pl.Date)
        return pl.DataFrame(
            {
                "date": dates,
                "symbol": list(self.symbols) * t,
                "factor": self.values.reshape(-1),
            }
        )


class FactorEngine:
    """Evaluate factor expressions over a point-in-time price panel."""

    def __init__(
        self,
        panel: pl.DataFrame,
        group_data: dict[str, dict[str, object]] | None = None,
    ):
        if panel.is_empty():
            raise ValueError("cannot build a FactorEngine on an empty panel")
        for required in ("date", "symbol"):
            if required not in panel.columns:
                raise ValueError(f"panel is missing the required {required!r} column")

        d_all = panel["date"].to_numpy()
        s_all = panel["symbol"].to_numpy()
        self.dates = np.unique(d_all)  # sorted ascending (time axis)
        self.symbols = np.unique(s_all)  # sorted (cross-section axis)
        self._di = np.searchsorted(self.dates, d_all)
        self._sj = np.searchsorted(self.symbols, s_all)
        self._shape = (self.dates.shape[0], self.symbols.shape[0])
        self._field_cols = [c for c in panel.columns if c not in ("date", "symbol")]
        self._panel = panel
        self._matrix_cache: dict[str, np.ndarray] = {}
        self._groups = self._build_groups(group_data or {})

    # -- field pivot ----------------------------------------------------------
    def _matrix(self, field: str) -> np.ndarray:
        """Pivot one field into an aligned ``(T, N)`` matrix (NaN where absent)."""
        if field in self._matrix_cache:
            return self._matrix_cache[field]
        if field not in self._field_cols:
            raise ValueError(
                f"field {field!r} is not in the panel (have: {sorted(self._field_cols)})"
            )
        arr = np.full(self._shape, np.nan, dtype=np.float64)
        arr[self._di, self._sj] = self._panel[field].to_numpy().astype(np.float64)
        self._matrix_cache[field] = arr
        return arr

    def field_matrix(self, name: str) -> np.ndarray:
        """Public accessor for a field's aligned ``(T, N)`` float64 matrix.

        Thin wrapper over the internal :meth:`_matrix` pivot (NaN where a symbol is
        absent on a date). Used by the evaluator layer to fetch raw price fields
        (e.g. ``open``/``close`` for forward returns) on the engine's own axes.
        """
        return self._matrix(name)

    def _build_groups(self, group_data) -> dict[str, np.ndarray]:
        groups: dict[str, np.ndarray] = {}
        symbols = self.symbols.tolist()
        for key, mapping in group_data.items():
            labels = [mapping.get(s) for s in symbols]
            missing = [s for s, lab in zip(symbols, labels) if lab is None]
            if missing:
                raise ValueError(
                    f"group_data[{key!r}] is missing labels for symbols {missing}; "
                    "every panel symbol must have a group label"
                )
            groups[key] = np.array(labels, dtype=object)
        return groups

    # -- evaluation -----------------------------------------------------------
    def evaluate(self, expr) -> FactorResult:
        """Parse (if needed) and evaluate ``expr`` into a :class:`FactorResult`."""
        node = parse(expr) if isinstance(expr, str) else expr
        expr_str = expr if isinstance(expr, str) else str(node)

        unknown_ops = {op for op in iter_ops(node) if not operators.is_registered(op)}
        if unknown_ops:
            raise ValueError(f"expression uses unregistered operators: {sorted(unknown_ops)}")

        matrices = {f: self._matrix(f) for f in iter_fields(node)}
        ctx = EvalContext(self.dates.tolist(), self.symbols.tolist(), matrices, self._groups)

        values = self._eval(node, ctx)
        if np.isscalar(values) or np.ndim(values) == 0:  # a constant factor
            values = np.full(self._shape, float(values), dtype=np.float64)
        return FactorResult(expr_str, np.asarray(values, dtype=np.float64), ctx.dates, ctx.symbols)

    def _eval(self, node, ctx: EvalContext):
        if isinstance(node, FieldNode):
            return ctx.matrices[node.name]
        if isinstance(node, LitNode):
            return node.value
        if isinstance(node, OpNode):
            spec = operators.get(node.op)
            args = [self._eval(child, ctx) for child in node.children]
            try:
                return spec.fn(*args, ctx=ctx) if spec.needs_ctx else spec.fn(*args)
            except EvaluationError:
                raise  # already attributed to the inner operator
            except Exception as exc:  # attribute the failure to this operator
                raise EvaluationError(str(exc), op=node.op, cause=type(exc).__name__) from exc
        raise TypeError(f"unexpected AST node: {node!r}")

    # -- diagnostics ----------------------------------------------------------
    def diagnose(
        self,
        expr,
        *,
        max_nan_fraction: float = 0.5,
        min_coverage: float = 0.02,
        extreme: float = 1e12,
        warmup_frac: float = 0.5,
    ):
        """Run the full pipeline and return structured :class:`FactorDiagnostics`.

        Never raises: parse, operator/field validation, evaluation and output-
        quality are each captured as coded, located diagnostics (designed for an
        LLM agent loop — ``.to_dict()`` is JSON-serialisable). On success the
        computed :class:`FactorResult` is attached as ``.result``.
        """
        from assay.engine import diagnostics as dg

        text = expr if isinstance(expr, str) else str(expr)
        fd = dg.FactorDiagnostics(expr=text, stage_reached="parse")

        # 1. parse
        try:
            node = parse(expr) if isinstance(expr, str) else expr
        except ParseError as exc:
            fd.add(dg.from_parse_error(exc, text))
            return fd
        except Exception as exc:  # defensive: diagnose() must never raise
            fd.add(dg.diag(dg.INTERNAL_ERROR, f"unexpected parser error: {exc}",
                           error_type=type(exc).__name__))
            return fd

        # 2. static validation against this panel (operators + fields)
        fd.stage_reached = "execute"
        for opname in sorted(iter_ops(node)):
            if not operators.is_registered(opname):
                fd.add(dg.diag(dg.UNREGISTERED_OPERATOR,
                               f"operator {opname!r} is not registered",
                               span=dg.locate(text, opname), operator=opname))
        for fname in sorted(iter_fields(node)):
            if fname not in self._field_cols:
                fd.add(dg.diag(dg.UNKNOWN_FIELD,
                               f"field {fname!r} is not in the panel "
                               f"(available: {sorted(self._field_cols)})",
                               span=dg.locate(text, fname),
                               field=fname, available=sorted(self._field_cols)))
        if not fd.ok:
            return fd

        # 3. evaluate
        try:
            result = self.evaluate(node)
        except EvaluationError as exc:
            fd.add(dg.from_runtime_error(exc, text, op=exc.op))
            return fd
        except ValueError as exc:
            fd.add(dg.from_runtime_error(exc, text))
            return fd
        except Exception as exc:  # defensive: diagnose() must never raise
            fd.add(dg.diag(dg.INTERNAL_ERROR, f"unexpected evaluation error: {exc}",
                           error_type=type(exc).__name__))
            return fd
        fd.result = result

        # 4. output-series quality
        fd.stage_reached = "output"
        out_diags, stats = dg.output_diagnostics(
            result.values,
            max_nan_fraction=max_nan_fraction,
            min_coverage=min_coverage,
            extreme=extreme,
            warmup_frac=warmup_frac,
        )
        for d in out_diags:
            fd.add(d)
        fd.stats = stats
        return fd

    # -- convenience constructor ---------------------------------------------
    @classmethod
    def from_store(
        cls,
        store,
        universe: str,
        period: tuple[str, str],
        as_of: str,
        fields: list[str] | None = None,
        adj: str = "split",
        group_data: dict[str, dict[str, object]] | None = None,
    ) -> "FactorEngine":
        """Build an engine from a :class:`DataStore`, fetching a PIT panel.

        ``fields`` defaults to the OHLCV fields the store provides; the panel is
        read point-in-time as-of ``as_of`` (look-ahead bias is impossible by
        construction — see engineering-docs section 3.4).
        """
        fields = fields or ["open", "high", "low", "close", "volume"]
        symbols = store.get_universe(universe, period[1], as_of)
        panel = store.get_panel(
            fields=fields,
            symbols=symbols,
            start_date=period[0],
            end_date=period[1],
            as_of_date=as_of,
            adj=adj,
        )
        return cls(panel, group_data=group_data)
