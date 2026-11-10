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

from assay import _native
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
        self.membership: np.ndarray | None = None   # (T, N) bool: point-in-time universe membership
        self.snapshot: str | None = None            # data version the panel was read from
        self.value_cache = None  # optional, bounded research execution cache

    @classmethod
    def from_matrices(
        cls,
        dates,
        symbols,
        matrices: dict[str, np.ndarray],
        group_data: dict[str, dict[str, object]] | None = None,
    ) -> "FactorEngine":
        """Build an engine from ``(T, N)`` field matrices (no long frame to pivot).

        ``symbols`` must be sorted (the engine's cross-section axis order)."""
        self = cls.__new__(cls)
        self.dates = np.asarray(dates).astype("datetime64[D]")
        self.symbols = np.asarray(symbols)
        if not len(self.dates) or not len(self.symbols):
            raise ValueError("cannot build a FactorEngine on an empty panel")
        if np.any(self.symbols[1:] < self.symbols[:-1]):
            raise ValueError("symbols must be sorted")
        self._shape = (self.dates.shape[0], self.symbols.shape[0])
        self._field_cols = list(matrices)
        self._panel = None
        self._matrix_cache = {f: np.asarray(m, dtype=np.float64) for f, m in matrices.items()}
        for f, m in self._matrix_cache.items():
            if m.shape != self._shape:
                raise ValueError(f"field {f!r} has shape {m.shape}, expected {self._shape}")
        self._groups = self._build_groups(group_data or {})
        self.membership = None
        self.snapshot = None
        return self

    def panel_frame(self):
        """The panel as a long ``date, symbol, *fields`` frame (built on demand for matrix engines)."""
        if self._panel is None:
            import polars as pl

            t, n = self._shape
            cols = {"date": pl.Series(np.repeat(self.dates, n)).cast(pl.Date),
                    "symbol": pl.Series(np.tile(self.symbols.astype(str), t))}
            for f in self._field_cols:
                cols[f] = pl.Series(self._matrix(f).reshape(-1))
            frame = pl.DataFrame(cols)
            self._panel = frame.filter(pl.any_horizontal([pl.col(f).is_not_nan() for f in self._field_cols]))
        return self._panel

    def _members_only(self, values: np.ndarray) -> np.ndarray:
        """NaN where the security was not in the universe on that date (row orders
        remembered for ``values`` carry over, filtered to the members)."""
        if self.membership is None:
            return values
        out = np.where(self.membership, values, np.nan)
        orders = _native.known_orders(values)
        if orders is not None and _native.kernels is not None:
            keep = np.ascontiguousarray(self.membership, dtype=bool)
            _native.remember_orders(out, _native.kernels.orders_where(*orders, keep))
        return out

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

        if not iter_fields(node):  # constant expression -> not a factor
            raise ParseError(
                "the expression references no data field — a constant is not a valid factor",
                code="CONSTANT_EXPRESSION")

        unknown_ops = {op for op in iter_ops(node) if not operators.is_registered(op)}
        if unknown_ops:
            raise ValueError(f"expression uses unregistered operators: {sorted(unknown_ops)}")

        matrices = {f: self._matrix(f) for f in iter_fields(node)}
        ctx = EvalContext(self.dates.tolist(), self.symbols.tolist(), matrices, self._groups)

        cache = getattr(self, 'value_cache', None)
        values = cache.evaluate(self, node, ctx) if cache is not None else self._eval(node, ctx)
        if np.isscalar(values) or np.ndim(values) == 0:  # a constant factor
            values = np.full(self._shape, float(values), dtype=np.float64)
        values = self._members_only(np.asarray(values, dtype=np.float64))
        return FactorResult(expr_str, values, ctx.dates, ctx.symbols)

    def _eval(self, node, ctx: EvalContext):
        if isinstance(node, FieldNode):
            return ctx.matrices[node.name]
        if isinstance(node, LitNode):
            return node.value
        if isinstance(node, OpNode):
            spec = operators.get(node.op)
            args = [self._eval(child, ctx) for child in node.children]
            args = self._cross_section_args(spec, args)
            try:
                return spec.fn(*args, ctx=ctx) if spec.needs_ctx else spec.fn(*args)
            except EvaluationError:
                raise  # already attributed to the inner operator
            except Exception as exc:  # attribute the failure to this operator
                raise EvaluationError(str(exc), op=node.op, cause=type(exc).__name__) from exc
        raise TypeError(f"unexpected AST node: {node!r}")

    # -- batch evaluation with common-sub-expression elimination (CSE) --------
    def _eval_cse(self, node, ctx: EvalContext, memo: dict, hashes: dict, refs: dict | None = None):
        """Evaluate ``node`` reusing a structural-hash memo (shared across a batch).

        Every :class:`OpNode` result is cached in ``memo`` keyed by its structural
        hash (looked up from the precomputed ``hashes`` map — O(1), never re-walking
        the subtree), so a sub-expression shared across the batch or repeated within
        one factor is computed exactly once. ``memo`` may be pre-seeded from a
        precompute store before the walk; a seeded hash short-circuits the whole
        subtree. Field/literal leaves are returned directly — they cost nothing.

        With ``refs`` (remaining uses per hash, see :meth:`_use_counts`) a child's entry
        is dropped from ``memo`` once its last consumer has been computed, so only the
        live frontier of the batch DAG stays in memory.
        """
        if isinstance(node, FieldNode):
            return ctx.matrices[node.name]
        if isinstance(node, LitNode):
            return node.value
        if not isinstance(node, OpNode):
            raise TypeError(f"unexpected AST node: {node!r}")

        h = hashes[id(node)]
        cached = memo.get(h)
        if cached is not None:
            return cached

        spec = operators.get(node.op)
        args = [self._eval_cse(child, ctx, memo, hashes, refs) for child in node.children]
        args = self._cross_section_args(spec, args)
        try:
            val = spec.fn(*args, ctx=ctx) if spec.needs_ctx else spec.fn(*args)
        except EvaluationError:
            raise
        except Exception as exc:
            raise EvaluationError(str(exc), op=node.op, cause=type(exc).__name__) from exc
        if np.isscalar(val) or np.ndim(val) == 0:  # a constant subtree
            val = np.full(self._shape, float(val), dtype=np.float64)
        else:
            val = np.asarray(val, dtype=np.float64)
        memo[h] = val
        if refs is not None:
            del args
            for child in node.children:
                if isinstance(child, OpNode):
                    self._release(hashes[id(child)], memo, refs)
        return val

    def _cross_section_args(self, spec, args):
        # Historical rolling inputs remain available before index admission;
        # cross-sectional statistics only include that day's actual members.
        if self.membership is None or spec.schema.get('category') != 'cross-sectional':
            return args
        return [np.where(self.membership, arg, np.nan) if isinstance(arg, np.ndarray) and arg.shape == self._shape
                else arg for arg in args]

    @staticmethod
    def _release(h: str, memo: dict, refs: dict) -> None:
        refs[h] -= 1
        if refs[h] <= 0:
            memo.pop(h, None)

    @staticmethod
    def _use_counts(nodes, hashes: dict) -> dict[str, int]:
        """Uses of each distinct subtree: one per (distinct parent, child slot), one per output."""
        refs: dict[str, int] = {}
        seen: set[str] = set()

        def _walk(n):
            h = hashes[id(n)]
            if h in seen:
                return
            seen.add(h)
            for c in n.children:
                if isinstance(c, OpNode):
                    ch = hashes[id(c)]
                    refs[ch] = refs.get(ch, 0) + 1
                    _walk(c)

        for nd in nodes:
            if isinstance(nd, OpNode):
                refs[hashes[id(nd)]] = refs.get(hashes[id(nd)], 0) + 1
                _walk(nd)
        return refs

    def evaluate_many(self, exprs, *, precompute=None, precompute_min_nodes: int = 3,
                      mask_output: bool = True) -> list[FactorResult]:
        """All results of :meth:`evaluate_iter` as a list (see there)."""
        return list(self.evaluate_iter(exprs, precompute=precompute, precompute_min_nodes=precompute_min_nodes,
                                       mask_output=mask_output))

    def evaluate_iter(self, exprs, *, precompute=None, precompute_min_nodes: int = 3, release: bool = True,
                      errors: str = "raise", mask_output: bool = True):
        """Evaluate many expressions over one shared panel with CSE (engineering §4.3).

        Builds the field matrices once and threads a single structural-hash memo
        across **all** expressions, so a sub-expression that recurs across the batch
        (``sub(high, low)``, ``ts_mean(volume, 20)``, …) is computed only once.
        Structural hashes are computed bottom-up in O(n) (not the O(n²) of calling
        ``struct_hash`` per node). With a bound ``precompute`` store the memo is
        **pre-seeded once** from disk — the batch's distinct subtrees of at least
        ``precompute_min_nodes`` nodes are looked up up front, so there is no per-node
        disk overhead during the walk (tiny subtrees are cheaper to recompute than to
        load, so they are not fetched).

        Yields one :class:`FactorResult` per input, in ``exprs`` order, as each is done.
        With ``release`` (the default; ignored with ``precompute``) a subtree's matrix
        is freed after its last use in the batch, so peak memory follows the width of
        the batch DAG rather than its number of distinct subtrees; a caller that
        consumes results one at a time never holds the whole batch. An expression that
        fails to parse / evaluate raises, exactly as :meth:`evaluate` would; with
        ``errors="yield"`` its exception is yielded in its place and the batch goes on.
        """
        from assay.engine.ast import hash_tree

        if errors not in ("raise", "yield"):
            raise ValueError("errors is 'raise' or 'yield'")
        nodes = [parse(e) if isinstance(e, str) else e for e in exprs]
        fields: set[str] = set()
        hashes: dict[int, str] = {}
        for nd in nodes:
            fields |= iter_fields(nd)
            hash_tree(nd, hashes)
        matrices = {f: self._matrix(f) for f in fields}
        ctx = EvalContext(self.dates.tolist(), self.symbols.tolist(), matrices, self._groups)

        memo: dict[str, np.ndarray] = {}
        refs = None
        if precompute is not None:
            self._seed_from_precompute(nodes, hashes, memo, precompute, precompute_min_nodes)
        elif release:
            refs = self._use_counts(nodes, hashes)

        for src, nd in zip(exprs, nodes):
            expr_str = src if isinstance(src, str) else str(nd)
            try:
                values = self._eval_cse(nd, ctx, memo, hashes, refs)
            except Exception as exc:
                if errors == "raise":
                    raise
                yield exc
                continue
            if refs is not None and isinstance(nd, OpNode):
                self._release(hashes[id(nd)], memo, refs)
            if np.isscalar(values) or np.ndim(values) == 0:
                values = np.full(self._shape, float(values), dtype=np.float64)
            values = np.asarray(values, dtype=np.float64)
            yield FactorResult(expr_str, self._members_only(values) if mask_output else values,
                               ctx.dates, ctx.symbols)
            del values

    @staticmethod
    def _seed_from_precompute(nodes, hashes: dict, memo: dict, precompute, min_nodes: int) -> None:
        """Pre-load precomputed matrices for the batch's distinct subtrees (>= ``min_nodes``).

        One disk lookup per distinct qualifying subtree hash (not per node), so the
        per-node walk stays pure in-memory. Misses are silently skipped.
        """
        sizes: dict[str, int] = {}

        def _walk(n):  # subtree node-count per hash, distinct over the batch
            if not isinstance(n, OpNode):
                return 1
            h = hashes[id(n)]
            if h in sizes:
                return sizes[h]
            sz = 1 + sum(_walk(c) for c in n.children)
            sizes[h] = sz
            return sz

        for nd in nodes:
            _walk(nd)
        for h, sz in sizes.items():
            if sz >= min_nodes and h not in memo:
                mat = precompute.get(h)
                if mat is not None:
                    memo[h] = mat

    def panel_fingerprint(self) -> str:
        """Cache identity of the observed values, axes, membership and group labels.

        Restatements and changes in research context invalidate stored subtrees,
        even when the date and symbol axes remain identical.
        """
        import hashlib

        d = self.dates
        first = str(d[0]) if d.size else ""
        last = str(d[-1]) if d.size else ""
        sym_digest = hashlib.blake2b(
            "\x1f".join(map(str, self.symbols.tolist())).encode(), digest_size=8
        ).hexdigest()
        fld_digest = hashlib.blake2b(
            "\x1f".join(sorted(self._field_cols)).encode(), digest_size=4
        ).hexdigest()
        preimage = f"{first}|{last}|{int(d.size)}|{int(self._shape[1])}|{sym_digest}|{fld_digest}"
        if self.snapshot:  # a new data version (restated history) must not reuse cached matrices
            preimage += f"|{self.snapshot}"
        preimage += '|' + self.content_fingerprint()
        return hashlib.blake2b(preimage.encode(), digest_size=16).hexdigest()

    def content_fingerprint(self) -> str:
        """Hash the observed panel, including values and historical membership."""
        import hashlib
        digest = hashlib.sha256()
        digest.update(str(self.dates.astype('datetime64[D]').tolist()).encode())
        digest.update(str(self.symbols.tolist()).encode())
        for field in sorted(self._field_cols):
            values = np.array(self.field_matrix(field), dtype='<f8', order='C', copy=True)
            values[np.isnan(values)] = np.nan
            digest.update(field.encode())
            digest.update(values.tobytes())
        if self.membership is not None:
            digest.update(np.asarray(self.membership, dtype=bool).tobytes())
        import json
        for key, values in sorted(self._groups.items()):
            digest.update(json.dumps([key, values.tolist()], sort_keys=True, default=str).encode())
        return digest.hexdigest()

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

        # degenerate expressions (data-free reject): a constant, or a bare data field
        if not iter_fields(node):
            fd.add(dg.diag(dg.CONSTANT_EXPRESSION,
                           "the expression references no data field — a constant is not a valid factor"))
            return fd
        if isinstance(node, FieldNode):
            fd.add(dg.diag(dg.BARE_FIELD,
                           f"{node.name!r} is a bare data field, not a factor — apply a transformation"))
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
        if hasattr(store, "universe_span"):
            # every security that was a member during the period, masked to its member dates below
            symbols = store.universe_span(universe, period[0], period[1], as_of)
        else:
            symbols = store.get_universe(universe, period[1], as_of)
        if hasattr(store, "get_matrices"):
            dates, syms, mats = store.get_matrices(fields, symbols, period[0], period[1], as_of, adj)
            engine = cls.from_matrices(dates, syms, mats, group_data=group_data)
        else:
            panel = store.get_panel(
                fields=fields,
                symbols=symbols,
                start_date=period[0],
                end_date=period[1],
                as_of_date=as_of,
                adj=adj,
            )
            engine = cls(panel, group_data=group_data)
        if hasattr(store, "get_membership"):
            engine.membership = store.get_membership(universe, engine.dates, engine.symbols, as_of)
        engine.snapshot = getattr(store, "snapshot_id", None)
        return engine
