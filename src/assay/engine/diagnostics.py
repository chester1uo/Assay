"""Structured diagnostics for factor parsing, execution and output quality.

Designed for an LLM agent alpha-mining loop: every problem is a :class:`Diagnostic`
with a **stable error code**, severity, the pipeline **stage**, a **detailed
message**, the **location** in the expression (character span + a caret snippet),
machine-readable **context**, and an **actionable suggestion**.
:class:`FactorDiagnostics` aggregates them across the three stages and serialises
to JSON via :meth:`FactorDiagnostics.to_dict`.

Stages and code prefixes:

* ``ASSAY-P###`` — **parse**: syntax, unknown operator/variable, wrong arity, ...
* ``ASSAY-E###`` — **execute**: a kernel raised, missing field/group data, ...
* ``ASSAY-O###`` — **output**: the factor ran but the *series* is suspect
  (all-NaN, too many NaNs, no cross-sectional variance, infinities, ...).

Use :func:`lint` for panel-free syntax checks, or
:meth:`assay.engine.FactorEngine.diagnose` for the full pipeline.
"""

from __future__ import annotations

import enum
import json
import re
import warnings
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from assay.engine import operators
from assay.engine.ast import FieldNode, iter_fields, iter_ops
from assay.engine.parsing import ParseError, parse


class Severity(str, enum.Enum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


class Stage(str, enum.Enum):
    PARSE = "parse"
    EXECUTE = "execute"
    OUTPUT = "output"


@dataclass(frozen=True)
class Code:
    """A stable diagnostic code: id, symbolic name, stage, severity and a default hint."""

    id: str
    name: str
    stage: Stage
    severity: Severity
    title: str
    hint: str


CATALOG: dict[str, Code] = {}


def _c(id, name, stage, severity, title, hint) -> Code:
    code = Code(id, name, stage, severity, title, hint)
    CATALOG[name] = code
    return code


# --- parse stage --------------------------------------------------------------
EMPTY_EXPRESSION = _c("ASSAY-P001", "EMPTY_EXPRESSION", Stage.PARSE, Severity.ERROR,
    "Empty expression", "Provide a non-empty factor expression.")
UNEXPECTED_CHARACTER = _c("ASSAY-P002", "UNEXPECTED_CHARACTER", Stage.PARSE, Severity.ERROR,
    "Unexpected character",
    "Remove or fix the highlighted character; only names, numbers, operators, parentheses, commas and '? :' are allowed.")
UNEXPECTED_TOKEN = _c("ASSAY-P003", "UNEXPECTED_TOKEN", Stage.PARSE, Severity.ERROR,
    "Unexpected token", "Check the syntax around the highlighted token (a missing operator, comma or operand?).")
UNEXPECTED_EOF = _c("ASSAY-P004", "UNEXPECTED_EOF", Stage.PARSE, Severity.ERROR,
    "Unexpected end of expression", "The expression ends early — an operand or a closing parenthesis is missing.")
TRAILING_TOKENS = _c("ASSAY-P005", "TRAILING_TOKENS", Stage.PARSE, Severity.ERROR,
    "Trailing tokens", "Remove the extra tokens after the expression (often an unbalanced parenthesis).")
UNKNOWN_OPERATOR = _c("ASSAY-P006", "UNKNOWN_OPERATOR", Stage.PARSE, Severity.ERROR,
    "Unknown operator", "Use a registered operator (see OPERATOR_SCHEMA) or register one with operators.register().")
OPERATOR_ARITY = _c("ASSAY-P007", "OPERATOR_ARITY", Stage.PARSE, Severity.ERROR,
    "Wrong number of arguments", "Pass exactly the arguments the operator's signature expects.")
INVALID_ARGUMENT = _c("ASSAY-P008", "INVALID_ARGUMENT", Stage.PARSE, Severity.ERROR,
    "Invalid argument", "Fix the highlighted argument; e.g. group operators take a label like 'sector'.")
INVALID_WINDOW = _c("ASSAY-P009", "INVALID_WINDOW", Stage.PARSE, Severity.ERROR,
    "Invalid look-back window", "The window must be a numeric literal, e.g. ts_mean(close, 20).")
EXPRESSION_TOO_DEEP = _c("ASSAY-P010", "EXPRESSION_TOO_DEEP", Stage.PARSE, Severity.ERROR,
    "Expression nested too deeply", "Reduce the nesting depth of the expression (split it into sub-factors).")
CONSTANT_EXPRESSION = _c("ASSAY-P011", "CONSTANT_EXPRESSION", Stage.PARSE, Severity.ERROR,
    "Constant expression (no data field)",
    "A factor must reference at least one data field (e.g. close, volume). A pure constant like '1' or '0' is not a valid factor.")
BARE_FIELD = _c("ASSAY-P012", "BARE_FIELD", Stage.PARSE, Severity.ERROR,
    "Bare data field is not a factor",
    "A raw field on its own carries no signal. Apply a transformation, e.g. cs_rank(close), ts_returns(close, 5) or ts_corr(close, volume, 20).")

# --- execute stage ------------------------------------------------------------
UNKNOWN_FIELD = _c("ASSAY-E001", "UNKNOWN_FIELD", Stage.EXECUTE, Severity.ERROR,
    "Unknown data field", "Reference a field present in the panel (open/high/low/close/volume/vwap/...) or load it.")
UNREGISTERED_OPERATOR = _c("ASSAY-E002", "UNREGISTERED_OPERATOR", Stage.EXECUTE, Severity.ERROR,
    "Unregistered operator", "Register the operator before evaluating.")
OPERATOR_RUNTIME_ERROR = _c("ASSAY-E003", "OPERATOR_RUNTIME_ERROR", Stage.EXECUTE, Severity.ERROR,
    "Operator failed at runtime", "Inspect the operator's argument values and shapes.")
INVALID_OPERATOR_PARAM = _c("ASSAY-E004", "INVALID_OPERATOR_PARAM", Stage.EXECUTE, Severity.ERROR,
    "Invalid operator parameter",
    "Fix the parameter: ts_std/ts_rank/ts_cov need d>=2, cs_winsorize needs 0<p<0.5, fillna method in {zero,median,ffill}, ts_delay needs d>=0.")
MISSING_GROUP_DATA = _c("ASSAY-E005", "MISSING_GROUP_DATA", Stage.EXECUTE, Severity.ERROR,
    "Missing group data", "Pass group_data=... to the engine for the referenced grouping (e.g. sector labels).")
NO_DATA = _c("ASSAY-E006", "NO_DATA", Stage.EXECUTE, Severity.ERROR,
    "No panel data", "Provide a non-empty price panel.")
LOOKAHEAD_SHIFT = _c("ASSAY-E007", "LOOKAHEAD_SHIFT", Stage.EXECUTE, Severity.ERROR,
    "Look-ahead shift",
    "A negative look-back peeks into the future. Use a non-negative window (ts_delay(x, d>=0)).")
INTERNAL_ERROR = _c("ASSAY-E099", "INTERNAL_ERROR", Stage.EXECUTE, Severity.ERROR,
    "Internal error", "This is an engine bug — please report the expression that triggered it.")

# --- output stage -------------------------------------------------------------
ALL_NAN = _c("ASSAY-O001", "ALL_NAN", Stage.OUTPUT, Severity.ERROR,
    "All-NaN output",
    "The factor produced no finite values — usually a look-back longer than the available history, or a domain error (log/sqrt of non-positive). Shorten windows or check inputs.")
HIGH_NAN_FRACTION = _c("ASSAY-O002", "HIGH_NAN_FRACTION", Stage.OUTPUT, Severity.WARNING,
    "High NaN fraction", "A large share of cells are NaN; shorten windows or check for domain errors / sparse data.")
CONSTANT_OUTPUT = _c("ASSAY-O003", "CONSTANT_OUTPUT", Stage.OUTPUT, Severity.WARNING,
    "No cross-sectional variance", "The factor has ~no cross-sectional dispersion, so it carries no rank signal — rethink the formula.")
NON_FINITE_VALUES = _c("ASSAY-O004", "NON_FINITE_VALUES", Stage.OUTPUT, Severity.WARNING,
    "Infinite values present", "Division by ~zero produced +/-inf; use safe_div or guard denominators.")
EXTREME_VALUES = _c("ASSAY-O005", "EXTREME_VALUES", Stage.OUTPUT, Severity.WARNING,
    "Extreme magnitudes", "Very large magnitudes may indicate overflow or an unnormalised factor; wrap in cs_rank/cs_zscore/scale.")
EXCESSIVE_WARMUP = _c("ASSAY-O006", "EXCESSIVE_WARMUP", Stage.OUTPUT, Severity.WARNING,
    "Excessive warm-up", "The factor only becomes valid late in the window; ensure enough history for the chosen look-backs.")
LOW_COVERAGE = _c("ASSAY-O007", "LOW_COVERAGE", Stage.OUTPUT, Severity.WARNING,
    "Low coverage", "Few valid cells; IC estimates will be noisy. Check windows and data availability.")
DEGENERATE_CROSS_SECTION = _c("ASSAY-O008", "DEGENERATE_CROSS_SECTION", Stage.OUTPUT, Severity.WARNING,
    "Degenerate cross-section", "Most dates have <2 valid symbols, so cross-sectional ranking/IC is undefined.")
NEAR_CONSTANT_OUTPUT = _c("ASSAY-O009", "NEAR_CONSTANT_OUTPUT", Stage.OUTPUT, Severity.WARNING,
    "Near-constant output",
    "The factor varies but its dynamic range is negligible relative to its level — effectively no rank signal. Rescale or rethink the formula.")

# failure_mode maps a code to the FactorReport-level mode (engineering-docs 7.2).
_FAILURE_MODE: dict[str, str] = {
    "ALL_NAN": "ALL_NAN",
    "CONSTANT_OUTPUT": "CONSTANT",
    "NEAR_CONSTANT_OUTPUT": "CONSTANT",
    "LOOKAHEAD_SHIFT": "LOOKAHEAD",
    "UNKNOWN_FIELD": "RUNTIME_ERROR",
    "UNREGISTERED_OPERATOR": "RUNTIME_ERROR",
    "OPERATOR_RUNTIME_ERROR": "RUNTIME_ERROR",
    "INVALID_OPERATOR_PARAM": "RUNTIME_ERROR",
    "MISSING_GROUP_DATA": "RUNTIME_ERROR",
    "NO_DATA": "RUNTIME_ERROR",
    "INTERNAL_ERROR": "RUNTIME_ERROR",
}


def caret_snippet(expr: str, span: tuple[int, int] | None) -> str | None:
    """Render ``expr`` with a caret ``^`` underline beneath ``span``."""
    if not expr or span is None:
        return None
    start, end = span
    start = max(0, min(start, len(expr)))
    end = max(start + 1, min(end, len(expr)))
    return f"{expr}\n{' ' * start}{'^' * (end - start)}"


def locate(expr: str, name: str) -> tuple[int, int] | None:
    """First whole-token occurrence of ``name`` (optionally ``$``-prefixed) in ``expr``."""
    if not expr or not name:
        return None
    m = re.search(r"(?<![\w.$])\$?" + re.escape(name) + r"(?![\w])", expr)
    return (m.start(), m.end()) if m else None


@dataclass
class Diagnostic:
    """One structured problem at a pipeline stage."""

    code: Code
    message: str
    span: tuple[int, int] | None = None
    context: dict = field(default_factory=dict)
    suggestion: str | None = None
    expr: str | None = None

    @property
    def severity(self) -> Severity:
        return self.code.severity

    @property
    def stage(self) -> Stage:
        return self.code.stage

    def snippet(self) -> str | None:
        return caret_snippet(self.expr, self.span)

    def to_dict(self) -> dict:
        loc = None
        if self.span is not None:
            loc = {"start": self.span[0], "end": self.span[1], "snippet": self.snippet()}
        return {
            "code": self.code.id,
            "name": self.code.name,
            "severity": self.code.severity.value,
            "stage": self.code.stage.value,
            "title": self.code.title,
            "message": self.message,
            "location": loc,
            "context": self.context,
            "suggestion": self.suggestion or self.code.hint,
        }

    def __str__(self) -> str:
        loc = f"  [{self.span[0]}:{self.span[1]}]" if self.span else ""
        out = f"[{self.code.id} {self.code.name}/{self.code.severity.value}] {self.message}{loc}"
        snip = self.snippet()
        if snip:
            out += "\n" + "\n".join("  " + ln for ln in snip.splitlines())
        out += f"\n  -> {self.suggestion or self.code.hint}"
        return out


def diag(code: Code, message: str, *, span=None, expr=None, suggestion=None, **context) -> Diagnostic:
    return Diagnostic(code, message, span=span, context=context, suggestion=suggestion, expr=expr)


@dataclass
class FactorDiagnostics:
    """Aggregated diagnostics for one factor across all three stages."""

    expr: str
    diagnostics: list[Diagnostic] = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    stage_reached: str = "parse"
    result: Any = None  # FactorResult | None

    @property
    def errors(self) -> list[Diagnostic]:
        return [d for d in self.diagnostics if d.severity is Severity.ERROR]

    @property
    def warnings(self) -> list[Diagnostic]:
        return [d for d in self.diagnostics if d.severity is Severity.WARNING]

    @property
    def ok(self) -> bool:
        """True when there are no ERROR-severity diagnostics (the factor is usable)."""
        return not self.errors

    @property
    def status(self) -> str:
        if self.errors:
            return "error"
        if self.warnings:
            return "warning"
        return "ok"

    @property
    def failure_mode(self) -> str | None:
        for d in self.errors:
            return _FAILURE_MODE.get(d.code.name, "RUNTIME_ERROR") if d.stage is not Stage.PARSE else "SYNTAX_ERROR"
        for d in self.warnings:  # a usable-but-suspect factor (e.g. CONSTANT)
            if d.code.name in _FAILURE_MODE:
                return _FAILURE_MODE[d.code.name]
        return None

    def add(self, d: Diagnostic) -> None:
        d.expr = self.expr
        self.diagnostics.append(d)

    def to_dict(self) -> dict:
        return {
            "expr": self.expr,
            "status": self.status,
            "ok": self.ok,
            "stage_reached": self.stage_reached,
            "failure_mode": self.failure_mode,
            "errors": [d.to_dict() for d in self.errors],
            "warnings": [d.to_dict() for d in self.warnings],
            "stats": self.stats,
        }

    def to_json(self, **kwargs) -> str:
        return json.dumps(self.to_dict(), **kwargs)

    def __str__(self) -> str:
        head = f"factor: {self.expr}\nstatus: {self.status} (stage: {self.stage_reached}, failure_mode: {self.failure_mode})"
        body = "\n".join(str(d) for d in self.diagnostics)
        return head + ("\n" + body if body else "")


# ---------------------------------------------------------------------------
# mapping raised exceptions -> diagnostics
# ---------------------------------------------------------------------------


def from_parse_error(err: ParseError, expr: str) -> Diagnostic:
    code = CATALOG.get(getattr(err, "code", None) or "", UNEXPECTED_TOKEN)
    return Diagnostic(code, getattr(err, "message", str(err)), span=getattr(err, "span", None), expr=expr)


def _classify_runtime(message: str) -> Code:
    msg = message.lower()
    if "look ahead" in msg or "look-ahead" in msg:  # negative shift peeks into the future
        return LOOKAHEAD_SHIFT
    if "group data" in msg:
        return MISSING_GROUP_DATA
    if "not in the panel" in msg:
        return UNKNOWN_FIELD
    if "empty panel" in msg:
        return NO_DATA
    # parameter-domain violations (specific phrasings to avoid false positives)
    if any(s in msg for s in ("needs d >=", "needs 0 <", "method must", "window length must")):
        return INVALID_OPERATOR_PARAM
    return OPERATOR_RUNTIME_ERROR


def from_runtime_error(err: Exception, expr: str, *, op: str | None = None) -> Diagnostic:
    code = _classify_runtime(str(err))
    span = locate(expr, op) if op else None
    ctx = {"operator": op} if op else {}
    # report the ORIGINAL exception type (EvaluationError records its cause)
    ctx["error_type"] = getattr(err, "cause", None) or type(err).__name__
    return Diagnostic(code, str(err), span=span, context=ctx, expr=expr)


# ---------------------------------------------------------------------------
# output-quality analysis
# ---------------------------------------------------------------------------


def _round(x, n=6):
    if x is None or (isinstance(x, float) and not np.isfinite(x)):
        return None
    return round(float(x), n)


def output_diagnostics(
    values: np.ndarray,
    *,
    max_nan_fraction: float = 0.5,
    min_coverage: float = 0.02,
    extreme: float = 1e12,
    warmup_frac: float = 0.5,
    constant_tol: float = 1e-12,
) -> tuple[list[Diagnostic], dict]:
    """Inspect a ``(T, N)`` factor matrix; return (diagnostics, stats)."""
    v = np.asarray(values, dtype=np.float64)
    T, N = (v.shape if v.ndim == 2 else (v.shape[0], 1))
    n_total = v.size
    isnan = np.isnan(v)
    isinf = np.isinf(v)
    finite = np.isfinite(v)
    n_finite = int(finite.sum())
    n_inf = int(isinf.sum())
    coverage = n_finite / n_total if n_total else 0.0
    nan_fraction = int(isnan.sum()) / n_total if n_total else 0.0
    valid_per_date = finite.sum(axis=1) if v.ndim == 2 else finite.astype(int)
    warm = next((int(t) for t in range(T) if valid_per_date[t] > 0), None)
    dates_signal = int((valid_per_date > 0).sum())
    dates_ge2 = int((valid_per_date >= 2).sum())

    fin_vals = v[finite]
    abs_max = float(np.max(np.abs(fin_vals))) if n_finite else None
    n_unique = int(np.unique(np.round(fin_vals, 12)).size) if n_finite else 0

    # cross-sectional dispersion per date (only where >=2 valid symbols)
    constant_date_frac = None
    if v.ndim == 2 and dates_ge2 > 0:
        masked = np.where(finite, v, np.nan)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)  # ddof<=0 on sparse dates
            cs_std = np.nanstd(masked, axis=1)
        ge2 = valid_per_date >= 2
        constant_date_frac = float(np.mean(cs_std[ge2] <= constant_tol))

    stats = {
        "n_dates": T, "n_symbols": N, "n_cells": n_total,
        "n_finite": n_finite, "coverage": _round(coverage), "nan_fraction": _round(nan_fraction),
        "inf_count": n_inf, "warmup_rows": warm,
        "dates_with_signal": dates_signal, "dates_ge2_symbols": dates_ge2,
        "n_unique_values": n_unique, "abs_max": _round(abs_max),
        "value_mean": _round(float(np.mean(fin_vals))) if n_finite else None,
        "value_std": _round(float(np.std(fin_vals))) if n_finite else None,
        "constant_date_fraction": _round(constant_date_frac),
    }

    diags: list[Diagnostic] = []
    if n_finite == 0:
        n_nan = int(isnan.sum())
        sug = None
        if n_inf > 0 and n_inf >= n_nan:  # dominated by infinities, not warm-up/domain NaNs
            sug = ("Every value is non-finite — usually division by ~zero. "
                   "Use safe_div(a, b) or guard the denominator.")
        diags.append(diag(
            ALL_NAN,
            f"the factor produced 0 finite values out of {n_total} cells "
            f"({T} dates x {N} symbols): {n_nan} NaN, {n_inf} +/-inf",
            suggestion=sug, n_cells=n_total, nan_count=n_nan, inf_count=n_inf,
            n_dates=T, n_symbols=N,
        ))
        return diags, stats

    if nan_fraction > max_nan_fraction:
        diags.append(diag(HIGH_NAN_FRACTION,
                          f"{nan_fraction:.1%} of cells are NaN (> {max_nan_fraction:.0%} threshold)"
                          + (f"; the factor warms up only at row {warm}/{T}" if warm else ""),
                          nan_fraction=_round(nan_fraction), warmup_rows=warm))
    if n_inf > 0:
        diags.append(diag(NON_FINITE_VALUES, f"{n_inf} infinite value(s) in the output", inf_count=n_inf))
    constant_flagged = n_unique <= 1 or (constant_date_frac is not None and constant_date_frac >= 0.99)
    if constant_flagged:
        diags.append(diag(CONSTANT_OUTPUT,
                          "the factor has no cross-sectional variance"
                          + (f" on {constant_date_frac:.0%} of evaluated dates" if constant_date_frac is not None else "")
                          + f" (only {n_unique} distinct value(s))",
                          n_unique_values=n_unique, constant_date_fraction=_round(constant_date_frac)))
    elif n_finite > 1:  # varies, but is the dynamic range negligible vs the level?
        span = float(fin_vals.max() - fin_vals.min())
        center = abs(float(np.mean(fin_vals))) + 1e-12
        if span > 0 and span / center < 1e-6:
            diags.append(diag(NEAR_CONSTANT_OUTPUT,
                              f"dynamic range {span:.3g} is negligible vs level {center:.3g} "
                              f"(relative span {span / center:.1e}) — effectively no rank signal",
                              span_abs=_round(span), relative_span=_round(span / center, 12)))
    if abs_max is not None and abs_max > extreme:
        diags.append(diag(EXTREME_VALUES, f"maximum magnitude is {abs_max:.3g} (> {extreme:.0g})", abs_max=_round(abs_max)))
    if warm is not None and T > 1 and warm > warmup_frac * T:
        diags.append(diag(EXCESSIVE_WARMUP,
                          f"first finite values appear at row {warm} of {T} ({warm / T:.0%} of the window is warm-up)",
                          warmup_rows=warm, n_dates=T))
    if 0 < coverage < min_coverage:
        diags.append(diag(LOW_COVERAGE, f"coverage is only {coverage:.2%}", coverage=_round(coverage)))
    if v.ndim == 2 and N > 1 and dates_signal > 0 and dates_ge2 < 0.5 * dates_signal:
        diags.append(diag(DEGENERATE_CROSS_SECTION,
                          f"only {dates_ge2} of {dates_signal} non-empty dates have >=2 valid symbols",
                          dates_ge2_symbols=dates_ge2, dates_with_signal=dates_signal))
    return diags, stats


# ---------------------------------------------------------------------------
# panel-free syntax linting
# ---------------------------------------------------------------------------


def lint(expr) -> FactorDiagnostics:
    """Parse-only diagnostics (no panel needed): syntax, unknown operator, arity.

    Accepts a string or a pre-built AST node, and never raises — any unexpected
    parser failure is captured as an INTERNAL_ERROR diagnostic. Field validity is
    NOT checked here (no panel); use :meth:`FactorEngine.diagnose` for that.
    """
    text = expr if isinstance(expr, str) else str(expr)
    fd = FactorDiagnostics(expr=text, stage_reached="parse")
    if not isinstance(expr, str):
        return fd  # already an AST node — nothing to lint syntactically
    try:
        node = parse(expr)
        if not iter_fields(node):  # references no data field -> a constant, not a factor
            fd.add(diag(CONSTANT_EXPRESSION,
                        "the expression references no data field — a constant is not a valid factor"))
        elif isinstance(node, FieldNode):  # a bare field is raw data, not a factor
            fd.add(diag(BARE_FIELD,
                        f"{node.name!r} is a bare data field, not a factor — apply a transformation"))
    except ParseError as e:
        fd.add(from_parse_error(e, text))
    except Exception as e:  # pragma: no cover - defensive: lint must never raise
        fd.add(diag(INTERNAL_ERROR, f"unexpected parser error: {e}", error_type=type(e).__name__))
    return fd
