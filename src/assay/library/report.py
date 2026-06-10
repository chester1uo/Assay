"""The :class:`FactorReport` contract — engineering-docs section 7.2.

A ``FactorReport`` is **not** a dashboard artifact: it is the machine-readable
protocol the agent loop consumes after every evaluation (engineering-docs section
2.3, "Agent feedback as a first-class output"). Every field is chosen to give the
next generation step actionable signal — predictive quality (IC/RankIC/ICIR),
horizon profile, decay, turnover, redundancy against the existing library, and a
diagnostics-derived failure mode + suggestion when something is wrong.

This module owns the *shape* of that protocol only — the evaluator (Phase-2
EVALUATOR agent) populates it, and :class:`assay.library.FactorLibrary` (Phase-2
LIBRARY agent) persists :class:`FactorSummary` rows derived from it. Keeping the
schema here, dependency-free over numpy/polars, lets every downstream module code
against one stable interface.

Serialisation rules (``to_dict``): JSON-safe — ``NaN``/``inf`` floats become
``None``, tuples become lists, nested :class:`Lineage`/diagnostics flatten to
dicts. ``from_dict`` is the exact inverse for the scalar fields, tolerant of
missing optional keys so older persisted rows still load.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from typing import Any

import polars as pl


# ---------------------------------------------------------------------------
# JSON-safety helpers
# ---------------------------------------------------------------------------
def _clean_float(x: Any) -> Any:
    """Map non-finite floats (NaN/inf) to ``None``; pass everything else through."""
    if isinstance(x, float) and not math.isfinite(x):
        return None
    return x


def _jsonify(x: Any) -> Any:
    """Recursively coerce a value into a JSON-serialisable form.

    Tuples -> lists, dict keys -> str (JSON object keys must be strings), NaN/inf
    floats -> None, nested containers handled element-wise. Objects exposing a
    ``to_dict`` (e.g. :class:`Lineage`, ``FactorDiagnostics``) are flattened.
    """
    if x is None:
        return None
    if isinstance(x, float):
        return _clean_float(x)
    if isinstance(x, (str, int, bool)):
        return x
    if hasattr(x, "to_dict") and callable(x.to_dict):
        return _jsonify(x.to_dict())
    if isinstance(x, dict):
        return {str(k): _jsonify(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_jsonify(v) for v in x]
    return x


# ---------------------------------------------------------------------------
# Lineage
# ---------------------------------------------------------------------------
@dataclass
class Lineage:
    """Reproducibility provenance for one evaluation (engineering-docs 7.2).

    Captures *what produced and what evaluated* a factor so any historical report
    can be traced and re-run: the LLM prompt hash, the immutable ``DataStore``
    snapshot id, the wall-clock eval time, the adjustment-factor version in force,
    and the source channel (agent loop, human SDK call, seed catalog, ...).
    """

    prompt_hash: str | None = None
    data_snapshot_id: str | None = None
    eval_timestamp: str | None = None  # ISO-8601
    adj_version: str | None = None
    source: str = "AGENT"

    def to_dict(self) -> dict[str, Any]:
        return {
            "prompt_hash": self.prompt_hash,
            "data_snapshot_id": self.data_snapshot_id,
            "eval_timestamp": self.eval_timestamp,
            "adj_version": self.adj_version,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "Lineage":
        if not d:
            return cls()
        return cls(
            prompt_hash=d.get("prompt_hash"),
            data_snapshot_id=d.get("data_snapshot_id"),
            eval_timestamp=d.get("eval_timestamp"),
            adj_version=d.get("adj_version"),
            source=d.get("source", "AGENT"),
        )


# ---------------------------------------------------------------------------
# FactorReport
# ---------------------------------------------------------------------------
@dataclass
class FactorReport:
    """Structured result of evaluating one factor — the agent-facing protocol.

    Scalar metrics (top of the field list) are always present. The trailing
    optional fields carry the *detail* a UI or deeper analysis wants — the per-date
    IC/RankIC series, the date axis, quintile spread returns, timing, and the raw
    :class:`~assay.engine.diagnostics.FactorDiagnostics` — and default to ``None``
    so a minimal report is cheap to build.
    """

    # --- identity ---------------------------------------------------------
    factor_id: str
    expr: str
    expr_canonical: str

    # --- predictive quality ----------------------------------------------
    ic: float
    icir: float
    rank_ic: float
    rank_icir: float
    ic_by_horizon: dict[int, float] = field(default_factory=dict)

    # --- dynamics ---------------------------------------------------------
    decay_halflife_days: int | None = None
    turnover_1d: float | None = None

    # --- redundancy against the library ----------------------------------
    redundancy_score: float = 0.0
    most_similar_factor: str | None = None

    # --- correctness / agent feedback ------------------------------------
    lookahead_detected: bool = False
    failure_mode: str | None = None  # SYNTAX_ERROR|LOOKAHEAD|CONSTANT|ALL_NAN|RUNTIME_ERROR
    suggestion: str | None = None

    # --- evaluation context ----------------------------------------------
    eval_period: tuple[str, str] = ("", "")
    universe_id: str = ""
    n_dates: int = 0
    n_symbols: int = 0
    execution: str = "next_open"
    neutralize: list[str] | None = None

    # --- provenance -------------------------------------------------------
    lineage: Lineage = field(default_factory=Lineage)

    # --- optional detail (None by default) -------------------------------
    ic_series: list[float] | None = None
    rank_ic_series: list[float] | None = None
    dates: list[str] | None = None
    quintile_returns: list[float] | None = None
    duration_ms: float | None = None
    diagnostics: Any = None  # FactorDiagnostics | dict | None

    # -- identity helper ---------------------------------------------------
    @staticmethod
    def compute_factor_id(expr_canonical: str) -> str:
        """SHA-256[:16] hex digest of the canonical expression (engineering-docs 7.2)."""
        return hashlib.sha256(expr_canonical.encode("utf-8")).hexdigest()[:16]

    # -- serialisation -----------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        """JSON-safe dict: NaN/inf -> None, tuples -> lists, nested objects flattened."""
        return {
            "factor_id": self.factor_id,
            "expr": self.expr,
            "expr_canonical": self.expr_canonical,
            "ic": _clean_float(self.ic),
            "icir": _clean_float(self.icir),
            "rank_ic": _clean_float(self.rank_ic),
            "rank_icir": _clean_float(self.rank_icir),
            "ic_by_horizon": {int(k): _clean_float(v) for k, v in self.ic_by_horizon.items()},
            "decay_halflife_days": self.decay_halflife_days,
            "turnover_1d": _clean_float(self.turnover_1d),
            "redundancy_score": _clean_float(self.redundancy_score),
            "most_similar_factor": self.most_similar_factor,
            "lookahead_detected": bool(self.lookahead_detected),
            "failure_mode": self.failure_mode,
            "suggestion": self.suggestion,
            "eval_period": list(self.eval_period),
            "universe_id": self.universe_id,
            "n_dates": self.n_dates,
            "n_symbols": self.n_symbols,
            "execution": self.execution,
            "neutralize": list(self.neutralize) if self.neutralize is not None else None,
            "lineage": self.lineage.to_dict(),
            "ic_series": _jsonify(self.ic_series),
            "rank_ic_series": _jsonify(self.rank_ic_series),
            "dates": _jsonify(self.dates),
            "quintile_returns": _jsonify(self.quintile_returns),
            "duration_ms": _clean_float(self.duration_ms),
            "diagnostics": _jsonify(self.diagnostics),
        }

    def to_json(self, **kwargs) -> str:
        return json.dumps(self.to_dict(), **kwargs)

    def to_dataframe(self) -> pl.DataFrame:
        """Per-date IC series as a long ``(date, ic, rank_ic)`` polars frame.

        Falls back to integer indices when ``dates`` is unset, and pads the shorter
        of the two series with nulls so the frame is always rectangular. Returns an
        empty (correctly-typed) frame when no series were recorded.
        """
        ic = self.ic_series or []
        ric = self.rank_ic_series or []
        n = max(len(ic), len(ric))
        if n == 0:
            return pl.DataFrame(
                schema={"date": pl.Utf8, "ic": pl.Float64, "rank_ic": pl.Float64}
            )
        if self.dates is not None and len(self.dates) == n:
            date_col = [str(d) for d in self.dates]
        else:
            date_col = [str(i) for i in range(n)]
        return pl.DataFrame(
            {
                "date": date_col,
                "ic": list(ic) + [None] * (n - len(ic)),
                "rank_ic": list(ric) + [None] * (n - len(ric)),
            }
        )

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "FactorReport":
        """Rebuild a report from a :meth:`to_dict` payload (tolerant of missing keys)."""
        ic_by_horizon = {int(k): v for k, v in (d.get("ic_by_horizon") or {}).items()}
        eval_period = d.get("eval_period") or ("", "")
        neutralize = d.get("neutralize")
        return cls(
            factor_id=d.get("factor_id", ""),
            expr=d.get("expr", ""),
            expr_canonical=d.get("expr_canonical", ""),
            ic=d.get("ic"),
            icir=d.get("icir"),
            rank_ic=d.get("rank_ic"),
            rank_icir=d.get("rank_icir"),
            ic_by_horizon=ic_by_horizon,
            decay_halflife_days=d.get("decay_halflife_days"),
            turnover_1d=d.get("turnover_1d"),
            redundancy_score=d.get("redundancy_score", 0.0) or 0.0,
            most_similar_factor=d.get("most_similar_factor"),
            lookahead_detected=bool(d.get("lookahead_detected", False)),
            failure_mode=d.get("failure_mode"),
            suggestion=d.get("suggestion"),
            eval_period=tuple(eval_period),
            universe_id=d.get("universe_id", ""),
            n_dates=d.get("n_dates", 0),
            n_symbols=d.get("n_symbols", 0),
            execution=d.get("execution", "next_open"),
            neutralize=list(neutralize) if neutralize is not None else None,
            lineage=Lineage.from_dict(d.get("lineage")),
            ic_series=d.get("ic_series"),
            rank_ic_series=d.get("rank_ic_series"),
            dates=d.get("dates"),
            quintile_returns=d.get("quintile_returns"),
            duration_ms=d.get("duration_ms"),
            diagnostics=d.get("diagnostics"),
        )


# ---------------------------------------------------------------------------
# FactorSummary
# ---------------------------------------------------------------------------
@dataclass
class FactorSummary:
    """Compact, list-view projection of a :class:`FactorReport`.

    The library persists one summary row per factor (the full report is heavier);
    these are what ``FactorLibrary.list``/leaderboard endpoints page over. Holds
    only the columns a ranking/triage UI needs — identity, headline quality, decay,
    redundancy, turnover, failure mode and provenance.
    """

    factor_id: str
    expr: str
    rank_ic: float
    rank_icir: float
    ic: float
    decay_halflife_days: int | None = None
    redundancy_score: float = 0.0
    turnover_1d: float | None = None
    failure_mode: str | None = None
    source: str = "AGENT"
    universe_id: str = ""

    @classmethod
    def from_report(cls, r: FactorReport) -> "FactorSummary":
        return cls(
            factor_id=r.factor_id,
            expr=r.expr,
            rank_ic=r.rank_ic,
            rank_icir=r.rank_icir,
            ic=r.ic,
            decay_halflife_days=r.decay_halflife_days,
            redundancy_score=r.redundancy_score,
            turnover_1d=r.turnover_1d,
            failure_mode=r.failure_mode,
            source=r.lineage.source if r.lineage else "AGENT",
            universe_id=r.universe_id,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "factor_id": self.factor_id,
            "expr": self.expr,
            "rank_ic": _clean_float(self.rank_ic),
            "rank_icir": _clean_float(self.rank_icir),
            "ic": _clean_float(self.ic),
            "decay_halflife_days": self.decay_halflife_days,
            "redundancy_score": _clean_float(self.redundancy_score),
            "turnover_1d": _clean_float(self.turnover_1d),
            "failure_mode": self.failure_mode,
            "source": self.source,
            "universe_id": self.universe_id,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "FactorSummary":
        return cls(
            factor_id=d.get("factor_id", ""),
            expr=d.get("expr", ""),
            rank_ic=d.get("rank_ic"),
            rank_icir=d.get("rank_icir"),
            ic=d.get("ic"),
            decay_halflife_days=d.get("decay_halflife_days"),
            redundancy_score=d.get("redundancy_score", 0.0) or 0.0,
            turnover_1d=d.get("turnover_1d"),
            failure_mode=d.get("failure_mode"),
            source=d.get("source", "AGENT"),
            universe_id=d.get("universe_id", ""),
        )
