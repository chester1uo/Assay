"""Assay factor execution engine.

Parse a factor expression (qlib or function-call/Alpha-101 syntax) into a unified
AST, then evaluate it over a point-in-time price panel to a ``(T, N)`` factor
matrix. See engineering-docs section 4 and the operator-compatibility table.

Typical use::

    from assay.engine import FactorEngine, parse

    eng = FactorEngine(panel)            # panel: long (date, symbol, *fields) frame
    result = eng.evaluate("cs_rank(ts_corr(close, volume, 20))")
    result.values                        # (T, N) numpy matrix
    result.to_frame()                    # long (date, symbol, factor) DataFrame

Equivalent expressions in either dialect parse to the same AST::

    parse("Corr($close, $volume, 20)").struct_hash() \\
        == parse("ts_corr(close, volume, 20)").struct_hash()
"""

from assay.engine import diagnostics, operators
from assay.engine.ast import FieldNode, LitNode, OpNode, iter_fields, iter_ops
from assay.engine.diagnostics import (
    CATALOG,
    Diagnostic,
    FactorDiagnostics,
    Severity,
    Stage,
    lint,
)
from assay.engine.cse import CommonSubexpr, common_subexpressions
from assay.engine.engine import EvalContext, EvaluationError, FactorEngine, FactorResult
from assay.engine.precompute import BoundPrecompute, PrecomputeStore
from assay.engine.operators import (
    OPERATOR_SCHEMA,
    OpSpec,
    all_specs,
    get,
    is_registered,
    op,
    operator_schema,
    register,
)
from assay.engine.parsing import (
    ExprParser,
    FuncParser,
    ParseError,
    QlibParser,
    detect_dialect,
    parse,
)

__all__ = [
    "FieldNode",
    "LitNode",
    "OpNode",
    "iter_fields",
    "iter_ops",
    "FactorEngine",
    "FactorResult",
    "EvalContext",
    "EvaluationError",
    "common_subexpressions",
    "CommonSubexpr",
    "PrecomputeStore",
    "BoundPrecompute",
    "diagnostics",
    "FactorDiagnostics",
    "Diagnostic",
    "Severity",
    "Stage",
    "CATALOG",
    "lint",
    "operators",
    "OPERATOR_SCHEMA",
    "OpSpec",
    "all_specs",
    "get",
    "is_registered",
    "register",
    "op",
    "operator_schema",
    "ExprParser",
    "QlibParser",
    "FuncParser",
    "ParseError",
    "detect_dialect",
    "parse",
]
