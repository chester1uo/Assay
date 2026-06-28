"""Portfolio backtest package — design-doc "Portfolio Backtest" (engineering Phase 5).

Given a factor signal, real trading constraints, and market costs, the portfolio
backtest answers the question IC analysis cannot: *what is the achievable net
return?* It re-evaluates a factor expression to an aligned ``(T, N)`` matrix and
runs a full simulation — universe filtering, signal processing, rebalance
scheduling, weight construction, constraint application, execution (slippage,
partial fill, T+1 settlement) and daily mark-to-market — producing a
:class:`PortfolioReport`.

The module is built market-agnostic and grounded to the only data Assay actually
has — US equities (NASDAQ-100 OHLCV + transactions). A-share regulatory rules
(T+1, price limits, the stamp-duty/commission/transfer-fee cost model) are encoded
as configuration that activates when its inputs are present; the data-dependent
A-share filters (ST, suspension, index reconstitution, northbound, sector-neutral)
are scaffolded as optional inputs that no-op gracefully when their data is absent.

The package owns the two schemas everything else codes against:
:class:`PortfolioBacktestConfig` (the full section-2 parameter set, with
``__post_init__`` validation, :meth:`~PortfolioBacktestConfig.preset` market
defaults, and JSON round-trip) and :class:`PortfolioReport` (the section-5
machine-readable result, mirroring :class:`assay.library.report.FactorReport`),
plus the :class:`Trade` and :class:`PositionSnapshot` records the report carries.

The assembled pipeline is :class:`PortfolioBacktester` (and the one-shot
:func:`run_portfolio_backtest` convenience): re-evaluate a factor expression to a
``(T, N)`` matrix, then run UniverseFilter -> SignalProcessor -> RebalanceScheduler
-> WeightConstructor -> ConstraintApplicator -> ExecutionSimulator ->
PortfolioAccountant -> :class:`PortfolioReport` (design-doc section 1.1).
"""

from __future__ import annotations

from assay.portfolio.backtester import PortfolioBacktester, run_portfolio_backtest
from assay.portfolio.config import PortfolioBacktestConfig
from assay.portfolio.report import (
    PortfolioLineage,
    PortfolioReport,
    PositionSnapshot,
    Trade,
)

__all__ = [
    "PortfolioBacktestConfig",
    "PortfolioReport",
    "PortfolioLineage",
    "Trade",
    "PositionSnapshot",
    "PortfolioBacktester",
    "run_portfolio_backtest",
]
