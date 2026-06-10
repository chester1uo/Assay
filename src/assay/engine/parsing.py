"""Two front-end parsers over one shared grammar.

Assay accepts factor expressions in two syntaxes (engineering-docs section 4.1),
both lowered to the unified AST in :mod:`assay.engine.ast`:

* **qlib** — ``$``-prefixed fields and CamelCase operators::

      Corr($close, $volume, 20) - Mean(Ref($close, 1), 10)

* **function-call** — Assay-native ``ts_*``/``cs_*`` names *and* the
  Alpha-101 / WorldQuant spellings (``delay``, ``correlation``, ``rank``,
  ``decay_linear``, ``SignedPower``, the ``adv{d}`` macro, the ``? :`` ternary)::

      ts_corr(close, volume, 20) - ts_mean(ts_delay(close, 1), 10)
      (returns < 0) ? stddev(returns, 20) : close

The two share a single tokenizer and recursive-descent grammar; only field
notation and the operator-name vocabulary differ. Name resolution is unified and
arity/type-aware, so the operator-compatibility table's three columns
(Alpha-101 · qlib · native) all reach the same canonical operators.
:class:`ExprParser` auto-detects the dialect; :func:`parse` is the convenience
entry point used everywhere else.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from assay.engine import operators
from assay.engine.ast import FieldNode, LitNode, OpNode

# Raw data fields the engine knows by bare name (no ``$`` needed).
KNOWN_FIELDS = {"open", "high", "low", "close", "volume", "vwap", "market_cap"}

# Operators whose final argument is an integer look-back window. Alpha-101 uses
# fractional windows (``decay_linear(x, 7.89)``); per the "101 Formulaic Alphas"
# paper a non-integer d is converted to floor(d).
_WINDOW_OPS = {
    "ts_delay", "ts_delta", "ts_returns", "ts_log_returns", "ts_mean", "ts_sum",
    "ts_product", "ts_std", "ts_min", "ts_max", "ts_argmin", "ts_argmax",
    "ts_rank", "ts_decay_linear", "ts_ema", "ts_dema", "ts_skew", "ts_kurt",
    "ts_corr", "ts_cov",
}
_GROUP_OPS = {"cs_neutralize", "cs_group_rank", "cs_group_mean"}

# Direct name -> canonical operator map (the unambiguous spellings). Ambiguous
# names (rank / min / max) are resolved by arity/type in `_resolve_call`.
_ALIASES = {
    # time-series
    "ts_delay": "ts_delay", "delay": "ts_delay", "Ref": "ts_delay",
    "ts_delta": "ts_delta", "delta": "ts_delta", "Delta": "ts_delta",
    "ts_returns": "ts_returns",
    "ts_log_returns": "ts_log_returns",
    "ts_mean": "ts_mean", "mean": "ts_mean", "Mean": "ts_mean",
    "ts_sum": "ts_sum", "sum": "ts_sum", "Sum": "ts_sum",
    "ts_product": "ts_product", "product": "ts_product", "Product": "ts_product",
    "ts_std": "ts_std", "stddev": "ts_std", "Std": "ts_std",
    "ts_corr": "ts_corr", "correlation": "ts_corr", "Corr": "ts_corr",
    "ts_cov": "ts_cov", "covariance": "ts_cov", "Cov": "ts_cov",
    "ts_decay_linear": "ts_decay_linear", "decay_linear": "ts_decay_linear", "WMA": "ts_decay_linear",
    "ts_ema": "ts_ema", "EMA": "ts_ema",
    "ts_dema": "ts_dema", "DEMA": "ts_dema",
    "ts_skew": "ts_skew", "ts_kurt": "ts_kurt",
    "ts_min": "ts_min", "ts_max": "ts_max",
    "ts_argmax": "ts_argmax", "Ts_ArgMax": "ts_argmax", "IdxMax": "ts_argmax",
    "ts_argmin": "ts_argmin", "Ts_ArgMin": "ts_argmin", "IdxMin": "ts_argmin",
    "ts_rank": "ts_rank", "Ts_Rank": "ts_rank", "TsRank": "ts_rank",
    # cross-sectional
    "cs_rank": "cs_rank", "CSRank": "cs_rank",
    "cs_zscore": "cs_zscore", "CSZScore": "cs_zscore",
    "cs_demean": "cs_demean", "CSDemean": "cs_demean",
    "cs_scale": "cs_scale", "scale": "cs_scale", "CSScale": "cs_scale",
    "cs_winsorize": "cs_winsorize",
    "cs_neutralize": "cs_neutralize", "IndNeutralize": "cs_neutralize",
    "indneutralize": "cs_neutralize",
    "cs_group_rank": "cs_group_rank", "cs_group_mean": "cs_group_mean",
    # math
    "abs": "abs", "Abs": "abs",
    "log": "log", "Log": "log",
    "sign": "sign", "Sign": "sign",
    "sqrt": "sqrt", "Sqrt": "sqrt",
    "signed_power": "signed_power", "SignedPower": "signed_power", "signedpower": "signed_power",
    "pow": "pow", "power": "pow", "Power": "pow",
    "clip": "clip", "Clip": "clip",
    "where": "where", "If": "where",
    "safe_div": "safe_div",
    "fillna": "fillna",
    "sigmoid": "sigmoid", "Sigmoid": "sigmoid",
    "elem_min": "elem_min", "elem_max": "elem_max",
}

# CamelCase operators that only exist in the qlib dialect (used for detection).
_QLIB_OPS = {"Ref", "Mean", "Std", "Corr", "Cov", "EMA", "DEMA", "WMA", "Delta",
             "Sum", "Product", "Rank", "IdxMax", "IdxMin", "Sign", "Abs", "Log",
             "Power", "Sqrt", "If", "Clip", "Sigmoid", "Less", "Greater"}


class ParseError(ValueError):
    """Raised on malformed factor expressions.

    Carries a structured ``code`` (a name in :data:`assay.engine.diagnostics.CATALOG`)
    and a ``span`` ``(start, end)`` char offset into the expression, so the
    diagnostics layer can render a located, coded :class:`Diagnostic`.
    """

    def __init__(self, message: str, *, code: str = "UNEXPECTED_TOKEN", span=None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.span = span


# ---------------------------------------------------------------------------
# tokenizer
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(
    r"""
      (?P<WS>\s+)
    | (?P<ADV>adv\d+(?:\.\d+)?)
    | (?P<NUMBER>\d+\.\d*|\.\d+|\d+)
    | (?P<DOLLAR>\$[A-Za-z_]\w*)
    | (?P<IDENT>[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)
    | (?P<STRING>'[^']*'|\"[^\"]*\")
    | (?P<OR>\|\|)
    | (?P<CMP><=|>=|==|!=|<|>)
    | (?P<CARET>\^)
    | (?P<OP>[+\-*/])
    | (?P<LPAREN>\()
    | (?P<RPAREN>\))
    | (?P<COMMA>,)
    | (?P<QMARK>\?)
    | (?P<COLON>:)
    """,
    re.VERBOSE,
)


@dataclass(frozen=True)
class _Tok:
    kind: str
    value: str
    pos: int


def _tokenize(expr: str) -> list[_Tok]:
    toks: list[_Tok] = []
    i = 0
    for m in _TOKEN_RE.finditer(expr):
        if m.start() != i:
            raise ParseError(f"unexpected character {expr[i]!r} at position {i}",
                             code="UNEXPECTED_CHARACTER", span=(i, i + 1))
        i = m.end()
        kind = m.lastgroup
        if kind == "WS":
            continue
        toks.append(_Tok(kind, m.group(), m.start()))
    if i != len(expr):
        raise ParseError(f"unexpected character {expr[i]!r} at position {i}",
                         code="UNEXPECTED_CHARACTER", span=(i, i + 1))
    return toks


_CMP_OPS = {"<": "lt", "<=": "le", ">": "gt", ">=": "ge", "==": "eq", "!=": "ne"}


# ---------------------------------------------------------------------------
# recursive-descent parser
# ---------------------------------------------------------------------------


class _Parser:
    """Precedence-climbing parser shared by both dialects."""

    # Cap on expression nesting depth. Well above any hand-written factor (~20)
    # and low enough that the depth guard fires before Python's recursion limit.
    _MAX_DEPTH = 64

    def __init__(self, toks: list[_Tok], expr: str):
        self.toks = toks
        self.expr = expr
        self.i = 0
        self.depth = 0

    # -- token cursor ----------------------------------------------------
    def _peek(self) -> _Tok | None:
        return self.toks[self.i] if self.i < len(self.toks) else None

    def _next(self) -> _Tok:
        tok = self._peek()
        if tok is None:
            raise ParseError(f"unexpected end of expression in {self.expr!r}",
                             code="UNEXPECTED_EOF", span=(len(self.expr), len(self.expr) + 1))
        self.i += 1
        return tok

    def _expect(self, kind: str) -> _Tok:
        tok = self._peek()
        if tok is None:
            raise ParseError(
                f"expected {kind} but the expression ended",
                code="UNEXPECTED_EOF", span=(len(self.expr), len(self.expr) + 1),
            )
        tok = self._next()
        if tok.kind != kind:
            raise ParseError(
                f"expected {kind} but found {tok.value!r} at position {tok.pos}",
                code="UNEXPECTED_TOKEN", span=(tok.pos, tok.pos + len(tok.value)),
            )
        return tok

    def _accept(self, kind: str) -> bool:
        tok = self._peek()
        if tok is not None and tok.kind == kind:
            self.i += 1
            return True
        return False

    # -- grammar (lowest precedence first) -------------------------------
    def parse(self):
        node = self._ternary()
        if self._peek() is not None:
            tok = self._peek()
            raise ParseError(f"unexpected trailing {tok.value!r} at position {tok.pos}",
                             code="TRAILING_TOKENS", span=(tok.pos, tok.pos + len(tok.value)))
        return node

    def _ternary(self):
        self.depth += 1
        if self.depth > self._MAX_DEPTH:
            raise ParseError(
                f"expression nesting exceeds {self._MAX_DEPTH} levels",
                code="EXPRESSION_TOO_DEEP", span=None,
            )
        try:
            cond = self._logical_or()
            if self._accept("QMARK"):
                a = self._ternary()
                self._expect("COLON")
                b = self._ternary()
                return OpNode("where", (cond, a, b))
            return cond
        finally:
            self.depth -= 1

    def _logical_or(self):
        left = self._comparison()
        while self._accept("OR"):
            right = self._comparison()
            left = OpNode("or", (left, right))
        return left

    def _comparison(self):
        left = self._additive()
        while (tok := self._peek()) is not None and tok.kind == "CMP":
            self._next()
            right = self._additive()
            left = OpNode(_CMP_OPS[tok.value], (left, right))
        return left

    def _additive(self):
        left = self._multiplicative()
        while (tok := self._peek()) is not None and tok.kind == "OP" and tok.value in "+-":
            self._next()
            right = self._multiplicative()
            left = OpNode("add" if tok.value == "+" else "sub", (left, right))
        return left

    def _multiplicative(self):
        left = self._unary()
        while (tok := self._peek()) is not None and tok.kind == "OP" and tok.value in "*/":
            self._next()
            right = self._unary()
            left = OpNode("mul" if tok.value == "*" else "div", (left, right))
        return left

    def _unary(self):
        tok = self._peek()
        if tok is not None and tok.kind == "OP" and tok.value == "-":
            self._next()
            operand = self._unary()
            if isinstance(operand, LitNode) and isinstance(operand.value, (int, float)):
                return LitNode(-operand.value)  # fold -literal so windows/params stay literal
            return OpNode("neg", (operand,))
        if tok is not None and tok.kind == "OP" and tok.value == "+":
            self._next()
            return self._unary()
        return self._power()

    def _power(self):
        base = self._primary()
        if self._accept("CARET"):  # right-associative; exponent may be an expression
            exponent = self._unary()
            return OpNode("pow", (base, exponent))
        return base

    def _primary(self):
        tok = self._next()
        if tok.kind == "ADV":  # adv20 / adv5.85 macro -> ts_mean(volume, floor(d))
            window = math.floor(float(tok.value[3:]))
            return OpNode("ts_mean", (FieldNode("volume"), LitNode(window)))
        if tok.kind == "NUMBER":
            return LitNode(float(tok.value) if "." in tok.value else int(tok.value))
        if tok.kind == "STRING":
            return LitNode(tok.value[1:-1])
        if tok.kind == "DOLLAR":
            return FieldNode(tok.value[1:].lower())
        if tok.kind == "LPAREN":
            node = self._ternary()
            self._expect("RPAREN")
            return node
        if tok.kind == "IDENT":
            if self._accept("LPAREN"):
                args = self._arg_list()
                return _resolve_call(tok.value, args, pos=tok.pos)
            return _resolve_atom(tok.value, pos=tok.pos)
        raise ParseError(f"unexpected token {tok.value!r} at position {tok.pos}",
                         code="UNEXPECTED_TOKEN", span=(tok.pos, tok.pos + len(tok.value)))

    def _arg_list(self):
        args = []
        if not self._accept("RPAREN"):
            args.append(self._ternary())
            while self._accept("COMMA"):
                args.append(self._ternary())
            self._expect("RPAREN")
        return args


# ---------------------------------------------------------------------------
# atom / call resolution (alias + macro + arity handling)
# ---------------------------------------------------------------------------

def _span(pos, text):
    return None if pos is None else (pos, pos + len(text))


def _resolve_atom(name: str, *, pos=None):
    if name.startswith("IndClass."):  # Alpha-101 IndClass.sector -> group literal
        return LitNode(name.split(".", 1)[1])
    if "." in name:
        raise ParseError(f"unexpected dotted identifier {name!r}",
                         code="INVALID_ARGUMENT", span=_span(pos, name))
    if name == "returns":  # Alpha-101 built-in field -> explicit 1-day return
        return OpNode("ts_returns", (FieldNode("close"), LitNode(1)))
    if name == "cap":
        return FieldNode("market_cap")
    # Known or custom bare field reference (the `adv{d}` macro is a separate token).
    return FieldNode(name)


def _coerce_window(node, *, name, pos):
    if isinstance(node, LitNode) and isinstance(node.value, (int, float)):
        return LitNode(int(math.floor(node.value)))  # paper: non-integer d -> floor(d)
    raise ParseError(
        f"the look-back window of {name!r} must be a numeric literal, got {node}",
        code="INVALID_WINDOW", span=_span(pos, name),
    )


def _coerce_group(node, *, name, pos):
    if isinstance(node, FieldNode):  # bare `sector` -> group label "sector"
        return LitNode(node.name)
    if isinstance(node, LitNode) and isinstance(node.value, str):
        return node
    raise ParseError(
        f"the group argument of {name!r} must be a name like 'sector' or IndClass.sector",
        code="INVALID_ARGUMENT", span=_span(pos, name),
    )


def _resolve_call(name: str, args: list, *, pos=None):
    n = len(args)
    sp = _span(pos, name)

    # Arity/type-ambiguous spellings shared across dialects:
    if name in {"rank", "Rank", "CSRank"}:
        if n == 1:
            canonical = "cs_rank"          # rank(x) / Rank($x) -> cross-sectional
        elif n == 2:
            canonical = "ts_rank"          # Rank(x, d) -> time-series
        else:
            raise ParseError(f"{name}(...) takes 1 or 2 arguments, got {n}",
                             code="OPERATOR_ARITY", span=sp)
    elif name in {"min", "max", "Min", "Max"}:
        if n != 2:
            raise ParseError(f"{name}(...) takes 2 arguments, got {n}",
                             code="OPERATOR_ARITY", span=sp)
        kind = name.lower()
        rolling = isinstance(args[1], LitNode) and isinstance(args[1].value, (int, float))
        canonical = f"ts_{kind}" if rolling else f"elem_{kind}"
    else:
        canonical = _ALIASES.get(name)
        if canonical is None and operators.is_registered(name):
            canonical = name  # canonical-native name or a user-registered custom operator
        if canonical is None:
            raise ParseError(
                f"unknown operator {name!r}; not a built-in or registered operator "
                "(see OPERATOR_SCHEMA, or register it with operators.register)",
                code="UNKNOWN_OPERATOR", span=sp,
            )

    try:
        operators.get(canonical).check_arity(n)
    except ValueError as ve:  # operator known but called with the wrong number of args
        raise ParseError(str(ve), code="OPERATOR_ARITY", span=sp) from ve

    if canonical in _WINDOW_OPS:
        args = [*args[:-1], _coerce_window(args[-1], name=name, pos=pos)]
    if canonical in _GROUP_OPS:
        args = [args[0], _coerce_group(args[1], name=name, pos=pos), *args[2:]]

    return OpNode(canonical, tuple(args))


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------


def detect_dialect(expr: str) -> str:
    """Return ``'qlib'`` or ``'func'`` for an expression (best-effort)."""
    if "$" in expr:
        return "qlib"
    for m in re.finditer(r"([A-Za-z_]\w*)\s*\(", expr):
        if m.group(1) in _QLIB_OPS:
            return "qlib"
    return "func"


class ExprParser:
    """Auto-detecting front-end parser (engineering-docs section 4.1).

    ``parse`` picks the dialect and produces a unified AST. ``QlibParser`` and
    ``FuncParser`` force a dialect; the grammar and operator backend are shared,
    so equivalent expressions in either syntax yield identical trees.
    """

    def parse(self, expr: str) -> OpNode | FieldNode | LitNode:
        self.dialect = detect_dialect(expr)
        return _parse(expr)


class QlibParser(ExprParser):
    def parse(self, expr: str):
        self.dialect = "qlib"
        return _parse(expr)


class FuncParser(ExprParser):
    def parse(self, expr: str):
        self.dialect = "func"
        return _parse(expr)


def _parse(expr: str):
    if not expr or not expr.strip():
        raise ParseError("empty factor expression", code="EMPTY_EXPRESSION", span=None)
    toks = _tokenize(expr)
    return _Parser(toks, expr).parse()


def parse(expr: str) -> OpNode | FieldNode | LitNode:
    """Parse a factor expression in either dialect into the unified AST."""
    return _parse(expr)
