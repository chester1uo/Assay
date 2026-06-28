"""The :class:`PortfolioReport` contract — design-doc section 5.

The structured, machine-readable output of a completed portfolio backtest. It
mirrors :class:`assay.library.report.FactorReport`: a flat dataclass of
diagnostic scalars (returns, risk-adjusted ratios, drawdown, turnover/cost) plus
heavier optional detail (NAV series, monthly returns, trade/position logs,
attribution, A-share metrics) and a reproducibility ``lineage`` block.

This module owns the *shape* of that protocol only — the simulator (Phase-5
ENGINE agent) populates it. Keeping the schema here, dependency-free over the
stdlib, lets every downstream consumer (WebUI, agent loop, CLI) code against one
stable interface.

Serialisation rules (:meth:`to_dict`): JSON-safe — non-finite floats (``NaN`` /
``inf``) become ``None``, tuples/arrays become lists, nested :class:`Trade` /
:class:`PositionSnapshot` / :class:`PortfolioLineage` flatten to dicts.
:meth:`from_dict` is the inverse, tolerant of missing optional keys so older
persisted reports still load. ``run_id`` = SHA-256[:12] of
``factor_id + config_hash`` (:meth:`compute_run_id`).
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# JSON-safety helpers (same contract as assay.library.report)
# ---------------------------------------------------------------------------
def _clean_float(x: Any) -> Any:
    """Map non-finite floats (NaN/inf) to ``None``; pass everything else through."""
    if isinstance(x, float) and not math.isfinite(x):
        return None
    return x


def _jsonify(x: Any) -> Any:
    """Recursively coerce a value into a JSON-serialisable form.

    Tuples -> lists, dict keys -> str, NaN/inf floats -> None, nested containers
    element-wise. Objects exposing a callable ``to_dict`` (e.g. :class:`Trade`,
    :class:`PositionSnapshot`, :class:`PortfolioLineage`) are flattened.
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
    # numpy scalars / arrays expose .tolist(); fall back to that.
    if hasattr(x, "tolist") and callable(x.tolist):
        return _jsonify(x.tolist())
    return x


# ---------------------------------------------------------------------------
# Trade
# ---------------------------------------------------------------------------
@dataclass
class Trade:
    """One executed (or blocked) order — design-doc 2.9 ``save_trade_log``.

    Records both intent (``target_w``) and reality (``exec_w``, ``price``,
    ``qty_frac``, ``cost``); ``blocked_reason`` is set (e.g. ``'limit_up'``,
    ``'limit_down'``, ``'t_plus_1'``, ``'adv_cap'``, ``'suspended'``) when the
    A-share/execution layer prevented or trimmed the fill, else ``None``.
    """

    date: str
    symbol: str
    side: str            # 'buy' | 'sell'
    target_w: float      # intended post-trade weight
    exec_w: float        # achieved post-trade weight (after constraints/blocks)
    price: float         # execution price (per execution_price benchmark)
    qty_frac: float      # signed weight change actually traded (fraction of NAV)
    cost: float          # total transaction cost for this trade (fraction of NAV)
    blocked_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "date": self.date,
            "symbol": self.symbol,
            "side": self.side,
            "target_w": _clean_float(self.target_w),
            "exec_w": _clean_float(self.exec_w),
            "price": _clean_float(self.price),
            "qty_frac": _clean_float(self.qty_frac),
            "cost": _clean_float(self.cost),
            "blocked_reason": self.blocked_reason,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Trade":
        return cls(
            date=d.get("date", ""),
            symbol=d.get("symbol", ""),
            side=d.get("side", ""),
            target_w=d.get("target_w"),
            exec_w=d.get("exec_w"),
            price=d.get("price"),
            qty_frac=d.get("qty_frac"),
            cost=d.get("cost"),
            blocked_reason=d.get("blocked_reason"),
        )


# ---------------------------------------------------------------------------
# PositionSnapshot
# ---------------------------------------------------------------------------
@dataclass
class PositionSnapshot:
    """Portfolio state on one date — design-doc 2.9 ``save_position_log``.

    ``weights`` maps symbol -> portfolio weight (fraction of NAV); ``nav`` is the
    mark-to-market net asset value (indexed from 1.0 at period start); ``cash`` is
    the residual cash weight (``1 - sum(weights)`` for long-only, signed otherwise).
    """

    date: str
    weights: dict[str, float]
    nav: float
    cash: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "date": self.date,
            "weights": {str(k): _clean_float(v) for k, v in self.weights.items()},
            "nav": _clean_float(self.nav),
            "cash": _clean_float(self.cash),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "PositionSnapshot":
        return cls(
            date=d.get("date", ""),
            weights=dict(d.get("weights") or {}),
            nav=d.get("nav"),
            cash=d.get("cash"),
        )


# ---------------------------------------------------------------------------
# PortfolioLineage
# ---------------------------------------------------------------------------
@dataclass
class PortfolioLineage:
    """Reproducibility provenance for one backtest — design-doc section 5 ``lineage.*``.

    Captures the immutable ``DataStore`` snapshot, the wall-clock run time, and the
    adjustment-factor version in force, so any historical report can be re-run.
    """

    data_snapshot: str | None = None
    eval_timestamp: str | None = None  # ISO-8601
    adj_version: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "data_snapshot": self.data_snapshot,
            "eval_timestamp": self.eval_timestamp,
            "adj_version": self.adj_version,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "PortfolioLineage":
        if not d:
            return cls()
        return cls(
            data_snapshot=d.get("data_snapshot"),
            eval_timestamp=d.get("eval_timestamp"),
            adj_version=d.get("adj_version"),
        )


# ---------------------------------------------------------------------------
# PortfolioReport
# ---------------------------------------------------------------------------
@dataclass
class PortfolioReport:
    """Structured result of one portfolio backtest — design-doc section 5.

    Scalar metrics (identity through cost/turnover) are always present. The
    trailing optional fields carry the detail a UI or deeper analysis wants — NAV
    and benchmark series, monthly returns, trade/position logs, attribution and
    A-share metrics — and default to empty/``None`` so a minimal report is cheap.
    """

    # --- identity ---------------------------------------------------------
    run_id: str  # SHA-256[:12] of (factor_id + config_hash)
    factor_id: str  # source factor (from the FactorReport)
    config: dict[str, Any] = field(default_factory=dict)  # full PortfolioBacktestConfig.to_dict()

    # --- period -----------------------------------------------------------
    period_start: str = ""  # actual start (after warmup)
    period_end: str = ""
    n_trading_days: int = 0
    n_rebalances: int = 0

    # --- return metrics (design-doc 4.1) ---------------------------------
    total_return: float = float("nan")   # net total return over period
    annual_return: float = float("nan")  # annualised net return
    gross_return: float = float("nan")   # before all transaction costs
    excess_return: float = float("nan")  # return minus benchmark

    # --- risk-adjusted metrics (design-doc 4.2) --------------------------
    sharpe: float = float("nan")
    sortino: float = float("nan")
    calmar: float = float("nan")
    information_ratio: float = float("nan")
    max_drawdown: float = float("nan")
    max_drawdown_start: str | None = None  # peak date before the max drawdown
    max_drawdown_end: str | None = None    # trough date
    drawdown_recovery_days: int | None = None  # trading days to recover; null if unrecovered
    beta: float = float("nan")
    alpha_capm: float = float("nan")       # annualised CAPM alpha
    tracking_error: float = float("nan")   # annualised active-return std

    # --- turnover and cost metrics (design-doc 4.3) ----------------------
    annual_turnover: float = float("nan")  # one-way annual turnover
    cost_drag: float = float("nan")        # gross - net return
    avg_holding_days: float = float("nan")  # implied avg holding period (trading days)

    # --- optional detail (cheap to omit) ---------------------------------
    nav_series: list[float] = field(default_factory=list)       # daily NAV from 1.0
    nav_dates: list[str] = field(default_factory=list)          # dates aligned to nav_series
    benchmark_series: list[float] = field(default_factory=list)  # benchmark NAV (same dates)
    monthly_returns: dict[str, float] = field(default_factory=dict)  # 'YYYY-MM' -> return
    trade_log: list[Trade] = field(default_factory=list)        # present if save_trade_log
    position_log: list[PositionSnapshot] = field(default_factory=list)  # if save_position_log
    attribution: dict[str, Any] | None = None  # present if compute_attribution
    a_share_metrics: dict[str, Any] | None = None  # present if market == 'A'

    # --- provenance -------------------------------------------------------
    lineage: PortfolioLineage = field(default_factory=PortfolioLineage)

    # -- identity helper ---------------------------------------------------
    @staticmethod
    def compute_run_id(factor_id: str, config_hash: str) -> str:
        """SHA-256[:12] of ``factor_id + config_hash`` (design-doc section 5)."""
        return hashlib.sha256(f"{factor_id}{config_hash}".encode("utf-8")).hexdigest()[:12]

    # -- serialisation -----------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        """JSON-safe dict: NaN/inf -> None, arrays/tuples -> lists, nested objects flattened."""
        return {
            "run_id": self.run_id,
            "factor_id": self.factor_id,
            "config": _jsonify(self.config),
            "period_start": self.period_start,
            "period_end": self.period_end,
            "n_trading_days": self.n_trading_days,
            "n_rebalances": self.n_rebalances,
            "total_return": _clean_float(self.total_return),
            "annual_return": _clean_float(self.annual_return),
            "gross_return": _clean_float(self.gross_return),
            "excess_return": _clean_float(self.excess_return),
            "sharpe": _clean_float(self.sharpe),
            "sortino": _clean_float(self.sortino),
            "calmar": _clean_float(self.calmar),
            "information_ratio": _clean_float(self.information_ratio),
            "max_drawdown": _clean_float(self.max_drawdown),
            "max_drawdown_start": self.max_drawdown_start,
            "max_drawdown_end": self.max_drawdown_end,
            "drawdown_recovery_days": self.drawdown_recovery_days,
            "beta": _clean_float(self.beta),
            "alpha_capm": _clean_float(self.alpha_capm),
            "tracking_error": _clean_float(self.tracking_error),
            "annual_turnover": _clean_float(self.annual_turnover),
            "cost_drag": _clean_float(self.cost_drag),
            "avg_holding_days": _clean_float(self.avg_holding_days),
            "nav_series": _jsonify(self.nav_series),
            "nav_dates": _jsonify(self.nav_dates),
            "benchmark_series": _jsonify(self.benchmark_series),
            "monthly_returns": {str(k): _clean_float(v) for k, v in self.monthly_returns.items()},
            "trade_log": [t.to_dict() for t in self.trade_log],
            "position_log": [p.to_dict() for p in self.position_log],
            "attribution": _jsonify(self.attribution),
            "a_share_metrics": _jsonify(self.a_share_metrics),
            "lineage": self.lineage.to_dict(),
        }

    def to_json(self, **kwargs: Any) -> str:
        return json.dumps(self.to_dict(), **kwargs)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "PortfolioReport":
        """Rebuild a report from a :meth:`to_dict` payload (tolerant of missing keys)."""
        return cls(
            run_id=d.get("run_id", ""),
            factor_id=d.get("factor_id", ""),
            config=dict(d.get("config") or {}),
            period_start=d.get("period_start", ""),
            period_end=d.get("period_end", ""),
            n_trading_days=d.get("n_trading_days", 0),
            n_rebalances=d.get("n_rebalances", 0),
            total_return=d.get("total_return", float("nan")),
            annual_return=d.get("annual_return", float("nan")),
            gross_return=d.get("gross_return", float("nan")),
            excess_return=d.get("excess_return", float("nan")),
            sharpe=d.get("sharpe", float("nan")),
            sortino=d.get("sortino", float("nan")),
            calmar=d.get("calmar", float("nan")),
            information_ratio=d.get("information_ratio", float("nan")),
            max_drawdown=d.get("max_drawdown", float("nan")),
            max_drawdown_start=d.get("max_drawdown_start"),
            max_drawdown_end=d.get("max_drawdown_end"),
            drawdown_recovery_days=d.get("drawdown_recovery_days"),
            beta=d.get("beta", float("nan")),
            alpha_capm=d.get("alpha_capm", float("nan")),
            tracking_error=d.get("tracking_error", float("nan")),
            annual_turnover=d.get("annual_turnover", float("nan")),
            cost_drag=d.get("cost_drag", float("nan")),
            avg_holding_days=d.get("avg_holding_days", float("nan")),
            nav_series=list(d.get("nav_series") or []),
            nav_dates=list(d.get("nav_dates") or []),
            benchmark_series=list(d.get("benchmark_series") or []),
            monthly_returns=dict(d.get("monthly_returns") or {}),
            trade_log=[Trade.from_dict(t) for t in (d.get("trade_log") or [])],
            position_log=[
                PositionSnapshot.from_dict(p) for p in (d.get("position_log") or [])
            ],
            attribution=d.get("attribution"),
            a_share_metrics=d.get("a_share_metrics"),
            lineage=PortfolioLineage.from_dict(d.get("lineage")),
        )

    # -- display -----------------------------------------------------------
    def __str__(self) -> str:
        """Compact one-block human summary (headline metrics only)."""

        def _f(x: Any, pct: bool = False) -> str:
            if x is None or (isinstance(x, float) and not math.isfinite(x)):
                return "n/a"
            return f"{x:+.2%}" if pct else f"{x:.2f}"

        return (
            f"PortfolioReport[{self.run_id}] factor={self.factor_id} "
            f"{self.period_start}..{self.period_end} "
            f"({self.n_trading_days}d, {self.n_rebalances} rebal)\n"
            f"  total={_f(self.total_return, True)} annual={_f(self.annual_return, True)} "
            f"excess={_f(self.excess_return, True)} cost_drag={_f(self.cost_drag, True)}\n"
            f"  sharpe={_f(self.sharpe)} sortino={_f(self.sortino)} "
            f"calmar={_f(self.calmar)} IR={_f(self.information_ratio)}\n"
            f"  maxDD={_f(self.max_drawdown, True)} beta={_f(self.beta)} "
            f"alpha={_f(self.alpha_capm, True)} turnover={_f(self.annual_turnover, True)}"
        )
