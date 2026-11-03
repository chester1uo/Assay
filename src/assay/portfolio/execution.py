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
        record_trades: bool = True,
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
        record_trades:
            ``False`` skips building the :class:`Trade` records (``trades`` comes
            back empty; weights, costs and diagnostics are unchanged) — for runs
            that do not keep a trade log.

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

        # Every name is decided at once, in the order the constraints bind: a name
        # with a wanted trade (``active``) ends in exactly one of untradable / limit
        # up / limit down / T+1 hold / capacity-cancelled / traded. The arithmetic
        # is the per-name arithmetic, element by element, and the cost total is
        # summed in symbol order, so results match a name-by-name walk bit for bit.
        with np.errstate(invalid="ignore", divide="ignore"):
            active = np.isfinite(desired_delta) & (np.abs(desired_delta) >= _TRADE_EPS)
            buy = desired_delta > 0
            untradable = active & ~np.isfinite(price)
            if tradable_mask is not None:
                untradable |= active & ~np.asarray(tradable_mask, dtype=bool)
            live = active & ~untradable

            up_hit = np.zeros(n, dtype=bool)
            down_hit = np.zeros(n, dtype=bool)
            if limits_on:
                up_hit = live & buy & np.isfinite(lim_up) & (price >= lim_up * _LIMIT_UP_TOL)
                down_hit = live & ~buy & np.isfinite(lim_down) & (price <= lim_down * _LIMIT_DOWN_TOL)
                live &= ~(up_hit | down_hit)

            held = np.zeros(n, dtype=bool)
            if t_plus_1_on and acquired_step:
                today = np.zeros(n, dtype=bool)
                today[[i for i, st in acquired_step.items() if st == step and 0 <= i < n]] = True
                held = live & ~buy & today
                live &= ~held

            fill = desired_delta.copy()
            capped = np.zeros(n, dtype=bool)
            advf = np.asarray(adv_fraction, dtype=np.float64) if cap_on else None
            if cap_on and cfg.partial_fill_handling != "force":
                # participation scales linearly with the traded fraction; cap the
                # trade so participation <= max_adv_fraction.
                ratio = cfg.max_adv_fraction / advf
                capped = live & np.isfinite(advf) & (advf > 0.0) & (ratio < 1.0)
                fill = np.where(capped, desired_delta * ratio, desired_delta)

            cancelled = live & (np.abs(fill) < _TRADE_EPS)   # capacity cancelled the whole trade
            traded = live & ~cancelled

            # realised participation for the *filled* notional, for slippage (NaN = none)
            advf_filled = None
            if cap_on:
                advf_filled = np.where(np.isfinite(advf) & (np.abs(desired_delta) > 0.0),
                                       advf * (np.abs(fill) / np.abs(desired_delta)), np.nan)

            new_w = cur + fill
            # trade_costs returns *rates* (fractions of the traded notional); the NAV
            # hit is the rate times the traded notional |fill| (a fraction of NAV).
            # Without the |fill| factor a 0.05% rate would cost 0.05% of NAV per
            # *name* traded — ~N times too much (design-doc 2.6.3 / costs.py).
            cost = self.cost_model.trade_costs(~buy, np.abs(fill), advf_filled) * np.abs(fill)
        paid = cost[traded]
        total_cost = float(np.cumsum(paid)[-1]) if paid.size else 0.0
        executed[traded] = new_w[traded]

        # T+1 inventory: a buy (or any increase) marks the name acquired now.
        if t_plus_1_on:
            for i in np.flatnonzero(traded & (fill > 0.0)).tolist():
                acquired_step[i] = step

        # residual: blocked names (and capacity cuts with 'defer') still want their
        # target next step. A partial fill counts in ``n_trades`` only, never also as
        # ``blocked_adv`` (fully blocked orders), so the §4.4 attempt denominator
        # never double-counts it.
        defer = cfg.partial_fill_handling == "defer"
        want = untradable | up_hit | down_hit | held | (cancelled & defer) | (traded & capped & defer)
        residual[want] = tgt[want]
        filled = traded & ~(capped & defer)
        residual[filled] = new_w[filled]

        diag["blocked_suspended"] = float(untradable.sum())
        diag["limit_hit_count"] = float(up_hit.sum() + down_hit.sum())
        diag["forced_hold_count"] = float(held.sum())
        diag["blocked_adv"] = float(cancelled.sum())
        diag["n_trades"] = float(traded.sum())

        if not record_trades:
            return ExecutionResult(executed_weights=executed, trades=trades, total_cost=float(total_cost),
                                   residual_targets=residual, diag=diag)
        kind = np.zeros(n, dtype=np.int8)
        for code, mask in enumerate((untradable, up_hit, down_hit, held, cancelled), start=1):
            kind[mask] = code
        reasons = (None, "suspended", "limit_up", "limit_down", "t_plus_1", "adv_cap")
        cur_l, tgt_l, px_l, new_l, fill_l, cost_l = (v.tolist() for v in (cur, tgt, price, new_w, fill, cost))
        kind_l, buy_l = kind.tolist(), buy.tolist()
        for i in np.flatnonzero(active).tolist():
            sym = symbols[i] if symbols is not None else str(i)
            if kind_l[i]:
                trades.append(self._blocked_trade(date, sym, cur_l[i], tgt_l[i], px_l[i], reasons[kind_l[i]]))
                continue
            trades.append(
                Trade(
                    date=date,
                    symbol=sym,
                    side="buy" if buy_l[i] else "sell",
                    target_w=float(tgt_l[i]),
                    exec_w=float(new_l[i]),
                    price=float(px_l[i]),
                    qty_frac=float(fill_l[i]),
                    cost=float(cost_l[i]),
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
