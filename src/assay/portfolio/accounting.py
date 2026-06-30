"""Portfolio accountant — design-doc 1.1 (PortfolioAccountant) + 4.1/4.3.

The final pipeline stage: walk the ``(T,)`` date axis day by day, drift the
weights with realised returns, execute the scheduled rebalances through the
:class:`~assay.portfolio.execution.ExecutionSimulator`, deduct costs from NAV, and
emit the series the :class:`~assay.portfolio.report.PortfolioReport` is built from.

**Accounting model — weight-based (documented).** The simulator works in
NAV-fraction space, not in integer share lots. Each day every held weight grows by
that name's simple return; the portfolio's gross daily return is
``sum_i w_i,t * r_i,t`` (cash earns 0). On a rebalance day the target weights are
applied through the simulator *at that day's drifted weights*, costs are deducted
as a one-off NAV haircut (``nav *= 1 - total_cost``), and the post-trade weights
become the base that drifts forward. This is exact for a continuously-divisible
portfolio and is the standard factor-backtest convention; it omits share-lot
rounding and odd-lot effects (immaterial at index scale, and Assay has no lot-size
data). Both **long-only** and **long/short** books are supported — weights may be
negative; ``cash = 1 - sum(weights)`` (signed) absorbs the residual, and the
gross-return dot product handles shorts naturally (a short's positive return is a
loss).

Two NAV paths are kept in lockstep on the *same* drifted weights: ``nav_series``
(net — costs deducted) and ``gross_nav`` (cost-free). Their terminal ratio is the
``cost_drag`` of section 4.3. Per-rebalance realised one-way turnover
(``sum|w_new - w_old| / 2``) is recorded for the annual-turnover metric.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from assay.portfolio.config import PortfolioBacktestConfig
from assay.portfolio.costs import TransactionCostModel
from assay.portfolio.execution import ExecutionSimulator


@dataclass
class AccountingResult:
    """Everything the accountant produces over one backtest walk.

    Series are aligned to the ``(T,)`` date axis of the input returns; ``weights``
    is the post-drift / post-rebalance book *at each date* (the position log).
    """

    nav_series: np.ndarray         # (T,) net NAV, starts 1.0
    gross_nav: np.ndarray          # (T,) cost-free NAV, starts 1.0
    weights: np.ndarray            # (T, N) book at each date (fraction of NAV)
    daily_returns: np.ndarray      # (T,) net portfolio simple return (NAV_t/NAV_{t-1}-1)
    trades: list                   # flat list[Trade] across all rebalances
    turnover_per_rebalance: list[float]  # one-way turnover at each executed rebalance
    rebalance_dates_idx: list[int]       # date-axis indices where a rebalance executed
    diag: dict[str, float]         # summed limit_hit_count / forced_hold_count etc.


@dataclass
class PortfolioAccountant:
    """Walk the date axis: drift, rebalance, mark-to-market (design-doc 1.1).

    Holds the ``config``, a :class:`TransactionCostModel`, and the
    :class:`ExecutionSimulator` that enforces the 2.6/2.7 constraints. Stateless
    across :meth:`run` calls — all walk state is local.
    """

    config: PortfolioBacktestConfig
    cost_model: TransactionCostModel = field(default=None)  # type: ignore[assignment]
    simulator: ExecutionSimulator = field(default=None)  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.cost_model is None:
            self.cost_model = TransactionCostModel(self.config)
        if self.simulator is None:
            self.simulator = ExecutionSimulator(self.config, self.cost_model)

    # -- main walk ---------------------------------------------------------
    def run(
        self,
        daily_returns: np.ndarray,
        schedule: dict[int, np.ndarray],
        *,
        exec_prices: np.ndarray | None = None,
        prev_closes: np.ndarray | None = None,
        limit_ups: np.ndarray | None = None,
        limit_downs: np.ndarray | None = None,
        adv_fractions: np.ndarray | None = None,
        tradable_masks: np.ndarray | None = None,
        dates: list[str] | None = None,
        symbols: list[str] | None = None,
    ) -> AccountingResult:
        """Simulate the book over the ``(T,)`` date axis.

        Parameters
        ----------
        daily_returns:
            ``(T, N)`` per-name simple returns. ``r[t, i]`` is name ``i``'s return
            *into* date ``t`` (so the weight held over ``[t-1, t]`` earns it). NaN
            is treated as 0 for that name (a missing symbol contributes nothing and
            does not poison the date's portfolio return).
        schedule:
            ``{date_index: target}`` — the rebalance schedule. ``target`` is either
            a target-weights ``(N,)`` vector or a **callable** ``fn(drifted_w) ->
            (N,) | None`` invoked with that day's drifted book (so the target can be
            built against the live position; ``None`` => skip this date, no trade).
            On each keyed date the resolved target is applied through the simulator
            at that day's drifted weights. Indices outside ``[0, T)`` are ignored.
        exec_prices, prev_closes, limit_ups, limit_downs, adv_fractions, tradable_masks:
            optional ``(T, N)`` per-date market inputs forwarded to the simulator
            (execution price, previous close for flat-pct price limits, explicit
            per-name ceiling/floor bands, ADV participation, tradability mask).
            ``None`` => that constraint is inert (the US default for
            prev_close/limits/adv/mask). ``exec_prices`` ``None`` => an all-ones
            price (trades always tradable, no NaN-price blocks).
        dates, symbols:
            optional labels for emitted :class:`Trade` records.

        Returns
        -------
        AccountingResult
            NAV (net + gross), the per-date weight book, net daily returns, the flat
            trade list, per-rebalance one-way turnover, the executed-rebalance date
            indices, and summed execution diagnostics.
        """
        rets = np.asarray(daily_returns, dtype=np.float64)
        if rets.ndim != 2:
            raise ValueError("daily_returns must be 2-D (T, N)")
        T, N = rets.shape
        rets = np.where(np.isfinite(rets), rets, 0.0)  # NaN return -> 0 for that name

        nav = np.empty(T, dtype=np.float64)
        gross = np.empty(T, dtype=np.float64)
        weight_log = np.zeros((T, N), dtype=np.float64)
        daily_port_ret = np.zeros(T, dtype=np.float64)

        cur_w = np.zeros(N, dtype=np.float64)  # current book; starts in cash
        acquired_step: dict[int, int] = {}     # T+1 inventory (symbol_idx -> step)
        nav_val = 1.0
        gross_val = 1.0

        trades: list = []
        turnover_per_rebalance: list[float] = []
        rebalance_idx: list[int] = []
        diag = {"limit_hit_count": 0.0, "forced_hold_count": 0.0,
                "blocked_suspended": 0.0, "blocked_adv": 0.0, "n_trades": 0.0}

        step = 0
        for t in range(T):
            # 1) Drift: each held weight earns its name's return into date t. The
            #    portfolio's gross daily return is the weighted sum; cash earns 0.
            r_t = rets[t]
            port_ret = float(np.dot(cur_w, r_t))
            nav_val *= 1.0 + port_ret
            gross_val *= 1.0 + port_ret
            daily_port_ret[t] = port_ret

            # Weights drift multiplicatively, then renormalise by the same growth so
            # they stay fractions of the new NAV (long-only sums toward invested
            # fraction; the dot product above already captured the return). cash =
            # 1 - sum stays consistent.
            grown = cur_w * (1.0 + r_t)
            denom = 1.0 + port_ret
            cur_w = grown / denom if denom != 0.0 else grown

            # 2) Rebalance, if scheduled for this date. A schedule entry is either a
            #    precomputed target vector (the classic path) OR a callable that
            #    builds the target from the *live drifted book* ``cur_w`` passed in —
            #    so weight construction, the turnover cap and threshold gating all
            #    see the real current position rather than a stale prior target. A
            #    callable returning ``None`` means "do not trade this date" (e.g. a
            #    threshold candidate whose drift did not breach the gate).
            if t in schedule:
                sched_val = schedule[t]
                target = sched_val(cur_w) if callable(sched_val) else sched_val
                if target is not None:
                    target = np.asarray(target, dtype=np.float64)
                    px = exec_prices[t] if exec_prices is not None else np.ones(N)
                    prev = prev_closes[t] if prev_closes is not None else None
                    lim_up = limit_ups[t] if limit_ups is not None else None
                    lim_dn = limit_downs[t] if limit_downs is not None else None
                    advf = adv_fractions[t] if adv_fractions is not None else None
                    mask = tradable_masks[t] if tradable_masks is not None else None
                    d_lbl = dates[t] if dates is not None else ""

                    res = self.simulator.execute(
                        target,
                        cur_w,
                        exec_price=px,
                        prev_close=prev,
                        limit_up=lim_up,
                        limit_down=lim_dn,
                        adv_fraction=advf,
                        tradable_mask=mask,
                        acquired_step=acquired_step,
                        step=step,
                        symbols=symbols,
                        date=d_lbl,
                    )
                    # One-way turnover realised by this rebalance.
                    turnover = 0.5 * float(np.nansum(np.abs(res.executed_weights - cur_w)))
                    turnover_per_rebalance.append(turnover)
                    rebalance_idx.append(t)

                    # Cost is a one-off NAV haircut on the *net* path only.
                    nav_val *= 1.0 - res.total_cost
                    cur_w = res.executed_weights
                    trades.extend(res.trades)
                    for k in diag:
                        diag[k] += res.diag.get(k, 0.0)
                    step += 1

            nav[t] = nav_val
            gross[t] = gross_val
            weight_log[t] = cur_w

        # Recompute net daily return from the (cost-inclusive) NAV path so a
        # rebalance day's cost shows up in that day's return.
        net_daily = np.empty(T, dtype=np.float64)
        if T > 0:
            net_daily[0] = nav[0] - 1.0
            net_daily[1:] = nav[1:] / nav[:-1] - 1.0

        return AccountingResult(
            nav_series=nav,
            gross_nav=gross,
            weights=weight_log,
            daily_returns=net_daily,
            trades=trades,
            turnover_per_rebalance=turnover_per_rebalance,
            rebalance_dates_idx=rebalance_idx,
            diag=diag,
        )

    # -- diagnostics -------------------------------------------------------
    @staticmethod
    def cost_drag(net_nav: np.ndarray, gross_nav: np.ndarray) -> float:
        """Total return lost to costs = gross total return - net total return (4.3)."""
        if net_nav.size == 0 or gross_nav.size == 0:
            return float("nan")
        net_tot = float(net_nav[-1] / net_nav[0] - 1.0)
        gross_tot = float(gross_nav[-1] / gross_nav[0] - 1.0)
        return gross_tot - net_tot
