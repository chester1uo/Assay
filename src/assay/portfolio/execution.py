"""Execution simulator — design-doc 1.1 (ExecutionSimulator) + 2.6.1/2.6.2/2.7.

The execution stage turns *intent* (target weights) into *reality* (executable
weight changes + costs), applying the microstructure constraints a naive backtest
ignores. Given a single rebalance step it produces the achievable post-trade
weights, a list of :class:`~assay.portfolio.report.Trade` records, the total cost
(a fraction of NAV), the residual targets still wanted but not yet filled, and a
diagnostics dict feeding the section-4.4 A-share metrics.

Constraints, in the order they bind (each can trim or block a name's trade):

1. **Tradability mask** (2.6 ST/suspended, optional) — a ``tradable_mask`` of
   ``False`` makes a name untradable; its weight is held (``blocked_reason``
   ``'suspended'``). ``None`` => everything tradable (the US default — Assay has
   no ST/suspension data, so the mask is always ``None`` there).
2. **Price limits** (2.6.2, A-share) — with ``enforce_limit_price`` a buy into a
   limit-up bar (``price >= ceiling``) and a sell into a limit-down bar
   (``price <= floor``) get zero fill (``'limit_up'`` / ``'limit_down'``). The
   ceiling/floor come from explicit per-name ``limit_up``/``limit_down`` bands
   (real 涨停价/跌停价, honouring 10/20/30%/ST boards) when supplied, else from a
   flat ``prev_close * (1 ± price_limit_pct)``. Neither input (US) => no limit logic.
3. **T+1 settlement** (2.6.1, A-share) — with ``t_plus_1`` and ``market == 'A'`` a
   name whose lot was *acquired today* (this step) cannot be reduced; the sell is
   deferred (``'t_plus_1'``, a forced hold). The acquired-step is tracked at the
   weight level by the caller-supplied ``acquired_step`` map (symbol-index ->
   step) and updated in place on fills.
4. **ADV capacity** (2.7) — ``|Δweight| <= max_adv_fraction`` of ADV. The excess
   is handled per ``partial_fill_handling``: ``'defer'`` returns the unfilled
   residual, ``'cancel'`` drops it, ``'force'`` ignores the cap. No ADV data
   (``adv_fraction is None``) => no capacity cap.

All vectors are 1-D float64 over the *same symbol axis* as the weights; missing
entries (NaN price / NaN weight) are treated as untradable for that name so one
bad symbol never poisons the step.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from assay.portfolio.config import PortfolioBacktestConfig
from assay.portfolio.costs import TransactionCostModel
from assay.portfolio.report import Trade

# Tolerances mirroring the design-doc apply_price_limit (2.6.2): a bar is "at the
# limit" within these factors of the theoretical limit price.
_LIMIT_UP_TOL = 0.9999
_LIMIT_DOWN_TOL = 1.0001
# Weight changes below this magnitude are treated as no-trade (dust / float noise).
_TRADE_EPS = 1e-12


@dataclass
class ExecutionResult:
    """One rebalance step's outcome (returned by :meth:`ExecutionSimulator.execute`)."""

    executed_weights: np.ndarray   # (N,) achievable post-trade weights
    trades: list[Trade]            # one Trade per name that traded or was blocked
    total_cost: float              # summed transaction cost (fraction of NAV)
    residual_targets: np.ndarray   # (N,) targets still wanted but unfilled ('defer')
    diag: dict[str, float]         # limit_hit_count / forced_hold_count / blocked counts


@dataclass
class ExecutionSimulator:
    """Apply 2.6/2.7 execution constraints to one rebalance step.

    Wraps the ``config`` and a :class:`TransactionCostModel`. The simulator is
    otherwise stateless — T+1 inventory lives in the caller-owned ``acquired_step``
    map passed to :meth:`execute`, so the accountant controls the date axis.
    """

    config: PortfolioBacktestConfig
    cost_model: TransactionCostModel = field(default=None)  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.cost_model is None:
            self.cost_model = TransactionCostModel(self.config)

    # -- main step ---------------------------------------------------------
    def execute(
        self,
        target_weights: np.ndarray,
        current_weights: np.ndarray,
        *,
        exec_price: np.ndarray,
        prev_close: np.ndarray | None = None,
        limit_up: np.ndarray | None = None,
        limit_down: np.ndarray | None = None,
        adv_fraction: np.ndarray | None = None,
        tradable_mask: np.ndarray | None = None,
        acquired_step: dict[int, int] | None = None,
        step: int = 0,
        symbols: list[str] | None = None,
        date: str = "",
    ) -> ExecutionResult:
        """Execute a move from ``current_weights`` toward ``target_weights``.

        Parameters
        ----------
        target_weights, current_weights:
            ``(N,)`` desired and held weights (fraction of NAV). NaN target is
            read as "no opinion" -> hold current.
        exec_price:
            ``(N,)`` execution price per :data:`config.execution_price`. NaN price
            makes a name untradable (held).
        prev_close:
            ``(N,)`` previous close, for *flat-percentage* price-limit detection
            (``price_limit_pct``). ``None`` (US) disables it. Ignored when explicit
            ``limit_up``/``limit_down`` bands are supplied.
        limit_up, limit_down:
            ``(N,)`` explicit per-name ceiling / floor prices in the **same basis
            as ``exec_price``** (A-share 涨停价/跌停价, rebased to the panel's
            adjustment). When given they override the ``prev_close * (1 ± pct)``
            bands, so real per-board limits (10/20/30%, ST 5%) are honoured instead
            of one flat percentage. NaN for a name => no limit constraint there.
            A missing side is treated as no bound (``+inf`` ceiling / ``-inf`` floor).
        adv_fraction:
            ``(N,)`` participation = ``|Δnotional| / ADV`` *at full target trade*,
            i.e. the order's fraction of ADV. ``None`` disables the capacity cap
            and slippage. Values are the realised participation the cost model also
            consumes for impact.
        tradable_mask:
            ``(N,)`` bool; ``False`` => untradable (ST/suspended). ``None`` => all
            tradable.
        acquired_step:
            caller-owned ``{symbol_index: step_acquired}`` for T+1. Updated in place
            when a name is newly bought. ``None`` => T+1 disabled regardless of
            config (no inventory to consult).
        step:
            monotonically increasing rebalance-step counter; a name with
            ``acquired_step[i] == step`` was bought *this* step and is T+1-locked.
        symbols, date:
            labels for the emitted :class:`Trade` records (cosmetic).

        Returns
        -------
        ExecutionResult
            ``executed_weights`` (achievable), ``trades``, ``total_cost``,
            ``residual_targets`` (unfilled-but-still-wanted, for ``'defer'``), and
            ``diag`` with ``limit_hit_count`` / ``forced_hold_count`` and per-reason
            blocked counts for the A-share metrics.
        """
        cur = np.asarray(current_weights, dtype=np.float64).copy()
        tgt = np.asarray(target_weights, dtype=np.float64)
        price = np.asarray(exec_price, dtype=np.float64)
        n = cur.shape[0]

        # NaN target => hold current (no opinion); NaN price => untradable.
        tgt = np.where(np.isfinite(tgt), tgt, cur)
        desired_delta = tgt - cur  # signed weight change we want this step

        executed = cur.copy()
        residual = cur.copy()  # default residual target = no further change wanted
        trades: list[Trade] = []
        total_cost = 0.0
        diag = {
            "limit_hit_count": 0.0,
            "forced_hold_count": 0.0,
            "blocked_suspended": 0.0,
            "blocked_adv": 0.0,
            "n_trades": 0.0,
        }

        cfg = self.config
        t_plus_1_on = bool(cfg.t_plus_1) and cfg.market == "A" and acquired_step is not None
        explicit_limits = limit_up is not None or limit_down is not None
        limits_on = bool(cfg.enforce_limit_price) and (
            explicit_limits or (cfg.price_limit_pct is not None and prev_close is not None)
        )
        if limits_on:
            if explicit_limits:
                # Real per-name bands (already in exec_price basis). A missing side
                # is an open bound so only the supplied side constrains.
                lim_up = (
                    np.asarray(limit_up, dtype=np.float64)
                    if limit_up is not None
                    else np.full(n, np.inf)
                )
                lim_down = (
                    np.asarray(limit_down, dtype=np.float64)
                    if limit_down is not None
                    else np.full(n, -np.inf)
                )
            else:
                prev = np.asarray(prev_close, dtype=np.float64)
                lim = float(cfg.price_limit_pct)
                lim_up = prev * (1.0 + lim)
                lim_down = prev * (1.0 - lim)
        cap_on = adv_fraction is not None

        for i in range(n):
            delta = desired_delta[i]
            if not np.isfinite(delta) or abs(delta) < _TRADE_EPS:
                continue  # nothing to do

            px = price[i]
            sym = symbols[i] if symbols is not None else str(i)

            # (1) Tradability mask (ST/suspended) — also NaN price is untradable.
            untradable = (not np.isfinite(px)) or (
                tradable_mask is not None and not bool(tradable_mask[i])
            )
            if untradable:
                diag["blocked_suspended"] += 1.0
                trades.append(
                    self._blocked_trade(date, sym, cur[i], tgt[i], px, "suspended")
                )
                residual[i] = tgt[i]  # still want it; defer
                continue

            side = "buy" if delta > 0 else "sell"

            # (2) Price limits: block buys at limit-up, sells at limit-down.
            if limits_on:
                if side == "buy" and np.isfinite(lim_up[i]) and px >= lim_up[i] * _LIMIT_UP_TOL:
                    diag["limit_hit_count"] += 1.0
                    trades.append(self._blocked_trade(date, sym, cur[i], tgt[i], px, "limit_up"))
                    residual[i] = tgt[i]
                    continue
                if (
                    side == "sell"
                    and np.isfinite(lim_down[i])
                    and px <= lim_down[i] * _LIMIT_DOWN_TOL
                ):
                    diag["limit_hit_count"] += 1.0
                    trades.append(self._blocked_trade(date, sym, cur[i], tgt[i], px, "limit_down"))
                    residual[i] = tgt[i]
                    continue

            # (3) T+1: a name acquired *this* step cannot be reduced now.
            if t_plus_1_on and side == "sell" and acquired_step.get(i) == step:
                diag["forced_hold_count"] += 1.0
                trades.append(self._blocked_trade(date, sym, cur[i], tgt[i], px, "t_plus_1"))
                residual[i] = tgt[i]
                continue

            # (4) ADV capacity cap on |Δweight|.
            fill = delta
            capped = False
            if cap_on:
                advf = adv_fraction[i]
                max_adv = cfg.max_adv_fraction
                if cfg.partial_fill_handling != "force" and np.isfinite(advf) and advf > 0.0:
                    # participation scales linearly with traded fraction; cap the
                    # trade so participation <= max_adv_fraction.
                    max_frac_of_desired = max_adv / advf
                    if max_frac_of_desired < 1.0:
                        fill = delta * max_frac_of_desired
                        capped = True

            if abs(fill) < _TRADE_EPS:
                # capacity cancelled the whole trade
                diag["blocked_adv"] += 1.0
                trades.append(self._blocked_trade(date, sym, cur[i], tgt[i], px, "adv_cap"))
                if cfg.partial_fill_handling == "defer":
                    residual[i] = tgt[i]
                continue

            # realised participation for the *filled* notional, for slippage.
            advf_filled = None
            if cap_on:
                advf = adv_fraction[i]
                if np.isfinite(advf) and abs(delta) > 0.0:
                    advf_filled = advf * (abs(fill) / abs(delta))

            new_w = cur[i] + fill
            # trade_cost returns a *rate* (fraction of the traded notional); the
            # NAV hit is that rate times the traded notional |fill| (a fraction of
            # NAV). Without the |fill| factor a 0.05% rate would cost 0.05% of NAV
            # per *name* traded — ~N times too much (design-doc 2.6.3 / costs.py).
            cost_rate = self.cost_model.trade_cost(side, abs(fill), advf_filled)
            cost = cost_rate * abs(fill)
            total_cost += cost
            executed[i] = new_w
            diag["n_trades"] += 1.0

            # T+1 inventory: a buy (or any increase) marks this name acquired now.
            if t_plus_1_on and fill > 0.0:
                acquired_step[i] = step

            # residual: if we capped+defer, we still want the rest next step. A
            # partial fill is already counted in ``n_trades``; it is *not* also a
            # ``blocked_adv`` event (that counts only fully-blocked orders), so the
            # §4.4 attempt denominator never double-counts a single partial fill.
            if capped and cfg.partial_fill_handling == "defer":
                residual[i] = tgt[i]
            else:
                residual[i] = new_w

            trades.append(
                Trade(
                    date=date,
                    symbol=sym,
                    side=side,
                    target_w=float(tgt[i]),
                    exec_w=float(new_w),
                    price=float(px),
                    qty_frac=float(fill),
                    cost=float(cost),
                    blocked_reason=None,
                )
            )

        return ExecutionResult(
            executed_weights=executed,
            trades=trades,
            total_cost=float(total_cost),
            residual_targets=residual,
            diag=diag,
        )

    # -- helpers -----------------------------------------------------------
    @staticmethod
    def _blocked_trade(
        date: str, symbol: str, cur_w: float, tgt_w: float, price: float, reason: str
    ) -> Trade:
        """A zero-fill :class:`Trade` recording an intent the constraints blocked."""
        side = "buy" if tgt_w >= cur_w else "sell"
        return Trade(
            date=date,
            symbol=symbol,
            side=side,
            target_w=float(tgt_w),
            exec_w=float(cur_w),  # weight unchanged — block holds the position
            price=float(price) if np.isfinite(price) else float("nan"),
            qty_frac=0.0,
            cost=0.0,
            blocked_reason=reason,
        )
