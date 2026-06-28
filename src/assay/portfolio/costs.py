"""Transaction cost model — design-doc section 2.6.3 (A-share cost breakdown).

A factor backtest's *net* return is the only number that matters (design-doc 4.1),
and the gap between gross and net is the transaction cost. This module turns the
config's cost fields (:mod:`assay.portfolio.config` section 2.6.3 + 2.7) into a
single fractional cost per trade.

A trade's cost is expressed as a **fraction of the traded notional** (itself a
fraction of NAV), so the accountant deducts ``cost * |notional_fraction|`` from
NAV. The model sums four market-agnostic components, each driven entirely by
config so the same code serves every market (the section-6 table picks the rates):

* **commission** (佣金) — ``commission_rate`` on both sides, with a per-trade
  minimum ``commission_min`` re-expressed as a fraction of notional;
* **stamp duty** (印花税) — ``stamp_duty_rate`` on the *sell* side only (A/HK
  differ in side coverage but the config already encodes the rate, so the side
  rule here is the A-share convention the doc specifies);
* **transfer fee** (过户费) — ``transfer_fee_rate`` on both sides;
* **slippage / market impact** — one of ``{'sqrt','linear','zero','almgren_chriss'}``
  scaled by ``slippage_k`` and the order's ADV participation (2.7).

The commission minimum is the one place a *fraction-of-notional* cost needs an
absolute floor (CNY 5). Because the simulator works in NAV-fraction space with no
absolute capital, the floor is applied as ``commission_min / (notional_fraction *
nav_capital_proxy)``; with no capital scale available it degrades to the rate-only
commission (documented in :meth:`trade_cost`). This keeps the core market-agnostic
and never fabricates a portfolio size.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from assay.portfolio.config import PortfolioBacktestConfig


@dataclass
class TransactionCostModel:
    """Fractional transaction cost from the config's section-2.6.3/2.7 rates.

    Stateless apart from the ``config`` it wraps. All methods return costs as a
    fraction of the *traded notional* (a buy of 2% of NAV with a 0.05% cost
    returns ``0.0005``; the caller multiplies by ``0.02`` to get the NAV hit).
    """

    config: PortfolioBacktestConfig
    # Optional NAV capital scale (in market currency) used only to apply the
    # absolute ``commission_min`` floor. ``None`` (the default in the
    # NAV-fraction simulator) => the floor is skipped and commission is rate-only.
    nav_capital: float | None = None

    # -- slippage / market impact -----------------------------------------
    def slippage(self, adv_fraction: float | None) -> float:
        """Market-impact cost fraction for an order of ``adv_fraction`` of ADV.

        ``adv_fraction`` is ``|order_notional| / average_daily_volume`` (2.7). The
        four families (config ``slippage_model``):

        * ``'sqrt'`` / ``'almgren_chriss'`` — ``k * sqrt(order/adv)`` (the
          Almgren-Chriss temporary-impact square-root law; treated as the sqrt
          variant here per the task spec);
        * ``'linear'`` — ``k * (order/adv)``;
        * ``'zero'`` — ``0``.

        When ``adv_fraction`` is ``None`` or non-finite (no volume data — the
        common US case where ADV is unavailable) impact degrades to ``0`` rather
        than fabricating a participation rate.
        """
        model = self.config.slippage_model
        if model == "zero":
            return 0.0
        if adv_fraction is None or not math.isfinite(adv_fraction) or adv_fraction <= 0.0:
            return 0.0
        k = self.config.slippage_k
        if model == "linear":
            return k * adv_fraction
        # 'sqrt' and 'almgren_chriss' both use the square-root impact law.
        return k * math.sqrt(adv_fraction)

    # -- per-trade cost ----------------------------------------------------
    def trade_cost(
        self,
        side: str,
        notional_fraction: float,
        adv_fraction: float | None = None,
    ) -> float:
        """Total cost fraction for one ``side`` trade of ``notional_fraction`` of NAV.

        Components (all as a fraction of the traded notional):

        * commission ``commission_rate`` (both sides) with the per-trade
          ``commission_min`` floor applied *iff* ``nav_capital`` is set — the
          floor is ``commission_min / |notional|`` where ``|notional|`` is in
          market currency (``|notional_fraction| * nav_capital``); with no capital
          scale the floor is skipped (rate-only commission, documented);
        * stamp duty ``stamp_duty_rate`` on ``side == 'sell'`` only;
        * transfer fee ``transfer_fee_rate`` both sides;
        * slippage from :meth:`slippage`.

        Returns ``0.0`` for a zero-notional trade. ``notional_fraction`` is taken
        by magnitude (sign carries no cost meaning).
        """
        nf = abs(float(notional_fraction))
        if nf == 0.0 or not math.isfinite(nf):
            return 0.0
        cfg = self.config

        # Commission (both sides), as a fraction of notional, with optional floor.
        commission = cfg.commission_rate
        if cfg.commission_min > 0.0 and self.nav_capital is not None and self.nav_capital > 0.0:
            notional_ccy = nf * self.nav_capital
            if notional_ccy > 0.0:
                commission = max(commission, cfg.commission_min / notional_ccy)

        # Stamp duty: sell side only (印花税).
        stamp = cfg.stamp_duty_rate if side == "sell" else 0.0

        # Transfer fee: both sides (过户费).
        transfer = cfg.transfer_fee_rate

        impact = self.slippage(adv_fraction)
        return commission + stamp + transfer + impact

    # -- diagnostics -------------------------------------------------------
    def round_trip_estimate(self, adv_fraction: float | None = None) -> float:
        """One round-trip (buy + sell) cost fraction for diagnostics.

        Sum of a buy and a sell leg at the same ``adv_fraction`` — the section-2.6.3
        "Total (round-trip)" figure (e.g. ~0.11% for A-share with the post-2023-08
        0.05% stamp duty). Used for the break-even-IC diagnostic, not the hot path.
        """
        buy = self.trade_cost("buy", 1.0, adv_fraction)
        sell = self.trade_cost("sell", 1.0, adv_fraction)
        return buy + sell
