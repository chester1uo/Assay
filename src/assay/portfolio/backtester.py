"""Portfolio backtester — the assembled pipeline (design-doc section 1.1).

:class:`PortfolioBacktester` wires every Phase-5 stage into a single run: it
re-evaluates a factor expression to an aligned ``(T, N)`` matrix on a
:class:`~assay.engine.FactorEngine`, then walks

    UniverseFilter -> SignalProcessor -> RebalanceScheduler -> WeightConstructor
    -> ConstraintApplicator -> ExecutionSimulator -> PortfolioAccountant
    -> PortfolioReport

producing the section-5 :class:`~assay.portfolio.report.PortfolioReport`.

**What the backtester owns (vs the stage modules).** The numerics live in the
stage modules (:mod:`signal`, :mod:`rebalance`, :mod:`weights`, :mod:`constraints`,
:mod:`execution`, :mod:`accounting`, :mod:`metrics`); this module owns only the
*orchestration* — deriving the daily-return / eligibility / execution-price
matrices the stages consume, mapping each rebalance's signal date to its execution
date (``execution_offset_days``), threading a trailing returns window into the
risk-aware weight constructors, building the schedule the accountant walks, picking
the benchmark, and folding everything into the report (plus the ``run_id`` /
``factor_id`` lineage).

**Grounding to reality** (the only data Assay has is US-equity OHLCV). The
optional A-share / microstructure inputs — ``groups`` (sector-neutral),
``tradable_mask`` (ST/suspended), ``prev_close`` (price limits), ``adv``
(capacity), ``benchmark`` (custom index series) — are all *caller-supplied* and
default to ``None``; each stage no-ops gracefully when its input is absent, so a
plain US backtest runs on OHLCV alone and never fabricates ST / suspension /
sector / index data (documented per stage in the modules cited above). The benchmark
defaults to an ``equal_weight`` eligible-universe proxy because Assay has **no index
price series** — a true index benchmark needs index data the DataStore lacks.

**Correctness notes** (design-doc section 7). Survivorship bias is handled upstream
by the PIT universe the :class:`FactorEngine` reads (``get_universe(..., as_of)``);
corp-action / adjustment versioning is fixed by the engine's ``adj`` parameter
queried as-of the backtest date — both are properties of the engine the backtester
builds, not re-derived here. Look-ahead is impossible by construction: the signal
on date ``t`` is only ever executed on ``t + execution_offset_days`` (>= 1).

The backtester **never raises on a bad factor**: an all-NaN / degenerate signal,
an empty universe, or a too-short period yields a well-formed report with an empty
NAV series and a clear diagnostic ``note`` (mirroring the engine's diagnostics
philosophy). The caller stamps ``lineage.eval_timestamp`` (this module never reads
wall-clock time, so a backtest is a pure function of its inputs).

House style: ``from __future__ import annotations``; numpy ``(T, N)`` float64
matrices (axis 0 = dates, axis 1 = symbols); NaN-aware throughout.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

import numpy as np

from assay.portfolio.accounting import PortfolioAccountant
from assay.portfolio.config import PortfolioBacktestConfig
from assay.portfolio.constraints import apply_constraints
from assay.portfolio.costs import TransactionCostModel
from assay.portfolio.execution import ExecutionSimulator
from assay.portfolio.metrics import compute_metrics
from assay.portfolio.rebalance import rebalance_dates, should_rebalance
from assay.portfolio.report import PortfolioLineage, PortfolioReport
from assay.portfolio.signal import normalize, process_signal
from assay.portfolio.weights import construct_weights

__all__ = ["PortfolioBacktester", "run_portfolio_backtest"]


# Trading days per year (annualisation constant; mirrors metrics._PPY / design-doc §4).
_PPY = 252


@dataclass
class PortfolioBacktester:
    """Run a full portfolio backtest from a factor expression and a config.

    Construct with a :class:`DataStore` (or ``None`` and monkeypatch
    :meth:`FactorEngine.from_store` for offline tests); call :meth:`run` with an
    expression string and a :class:`PortfolioBacktestConfig`.
    """

    store: Any = None

    # ------------------------------------------------------------------ run --
    def run(
        self,
        expr: str,
        config: PortfolioBacktestConfig,
        *,
        as_of: str | None = None,
        groups: np.ndarray | None = None,
        tradable_mask: np.ndarray | None = None,
        prev_close: np.ndarray | None = None,
        adv: np.ndarray | None = None,
        benchmark: Sequence[float] | np.ndarray | None = None,
    ) -> PortfolioReport:
        """Execute the section-1.1 pipeline and return a :class:`PortfolioReport`.

        Parameters
        ----------
        expr:
            Factor expression (re-evaluated to the ``(T, N)`` signal matrix —
            :class:`PortfolioReport` does not store factor values).
        config:
            The full :class:`PortfolioBacktestConfig` (universe, period, rebalance,
            weights, constraints, costs, benchmark, output flags).
        as_of:
            PIT cutoff for the engine panel; defaults to ``config.as_of_date`` then
            ``config.period_end``. Guarantees survivorship-bias-free universe and
            adjustment versioning (design-doc §7.1/§7.2).
        groups, tradable_mask, prev_close, adv:
            All optional market inputs are aligned to the **engine's symbol axis**
            (``FactorEngine`` sorts symbols ascending, so a caller building these
            from raw data must order columns to match ``engine.symbols`` / sorted
            ticker order). A misaligned column ordering silently mis-prices names.
        groups:
            optional ``(N,)`` per-symbol sector labels for the sector-neutral signal
            / constraint hook. ``None`` (US default) => no-op (no sector data).
        tradable_mask:
            optional ``(T, N)`` or ``(N,)`` bool mask; ``False`` => untradable
            (ST/suspended). ``None`` => everything tradable.
        prev_close:
            optional ``(T, N)`` previous-close matrix for A-share price-limit
            detection. ``None`` (US) => no limit logic.
        adv:
            optional ``(T, N)`` ADV-participation matrix (``|order|/ADV`` per name)
            for the capacity cap / impact. ``None`` => no capacity cap, no impact.
        benchmark:
            optional benchmark **NAV series** (length ``T``) used only when
            ``config.benchmark == 'custom'``. For ``'equal_weight'`` / ``'index'``
            an equal-weight eligible-universe proxy is built (a true index needs
            index data Assay lacks); ``'none'`` / ``'cash'`` => flat risk-free cash.

        Returns
        -------
        PortfolioReport
            Fully-assembled section-5 report. Never raises on a bad factor: a
            degenerate run yields an empty-NAV report carrying a diagnostic note in
            ``attribution['note']``.
        """
        canonical, factor_id = self._identity(expr)
        run_id = PortfolioReport.compute_run_id(factor_id, config.config_hash())

        # --- 1. engine + matrices (PIT universe + adj versioning => §7.1/§7.2) ---
        try:
            engine, factor, close, open_, dates, symbols = self._build_matrices(
                expr, config, as_of
            )
        except Exception as exc:  # never raise on a data/engine problem
            return self._empty_report(
                run_id, factor_id, config, note=f"engine build failed: {exc}"
            )

        T, N = factor.shape
        if T < 2 or N == 0:
            return self._empty_report(
                run_id, factor_id, config,
                note=f"insufficient panel ({T} dates x {N} symbols) for a backtest",
                dates=dates,
            )

        # --- 2. daily simple returns + eligibility (UniverseFilter) -------------
        daily_ret = self._daily_returns(close)  # (T, N), r[t] earned over [t-1, t]
        eligible = self._eligibility(factor, close, tradable_mask, T, N)  # (T, N) bool
        if not eligible.any():
            return self._empty_report(
                run_id, factor_id, config,
                note="no eligible symbol on any date (all factor or price values NaN)",
                dates=dates,
            )

        # --- 3. clean signal (SignalProcessor) ----------------------------------
        # Auto-load industry groups from the store for sector-neutralisation when
        # the caller did not supply them and the config asks for it (no-op for US
        # / offline runs). Gated on sector_neutral so a plain run is never silently
        # sector-balanced.
        cutoff = as_of or config.as_of_date or config.period_end
        if groups is None and config.sector_neutral:
            groups = self._load_groups(symbols, cutoff)
        signal = process_signal(factor, config, groups)
        signal = np.where(eligible, signal, np.nan)  # ineligible -> NaN (weight 0)

        # --- 4. rebalance schedule (RebalanceScheduler) + execution offset ------
        reb_idx = rebalance_dates(factor, dates, config)
        offset = max(1, int(config.execution_offset_days))

        # --- 5/6. per-rebalance weights + constraints -> accountant schedule ----
        schedule, n_scheduled = self._build_schedule(
            reb_idx, offset, signal, daily_ret, eligible, config, groups, T, N
        )

        # --- 7. execution inputs (prices / prev_close / limits / adv / mask) ----
        exec_prices = self._exec_price_matrix(config, open_, close)
        prev_mat = self._as_tn(prev_close, T, N)
        adv_mat = self._as_tn(adv, T, N)
        mask_mat = self._as_tn_bool(tradable_mask, T, N)
        # Real per-board A-share price-limit bands, auto-loaded from the store's
        # trade_status and rebased into the panel's adjustment basis (no-op for US
        # / offline-engine runs). ``close`` is the engine's *adjusted* close.
        # (``cutoff`` computed in step 3.)
        cn_up, cn_dn = self._cn_limit_matrices(config, close, dates, symbols, cutoff)

        # --- 8. accountant walk (ExecutionSimulator + PortfolioAccountant) ------
        cost_model = TransactionCostModel(config)
        accountant = PortfolioAccountant(
            config,
            cost_model=cost_model,
            simulator=ExecutionSimulator(config, cost_model),
        )
        date_lbls = [str(d) for d in dates]
        acct = accountant.run(
            daily_ret,
            schedule,
            exec_prices=exec_prices,
            prev_closes=prev_mat,
            limit_ups=cn_up,
            limit_downs=cn_dn,
            adv_fractions=adv_mat,
            tradable_masks=mask_mat,
            dates=date_lbls,
            symbols=[str(s) for s in symbols],
        )

        # --- 9. benchmark series ------------------------------------------------
        # The equal-weight index proxy must be *factor-independent* — keyed off
        # price + tradability only — so the benchmark universe does not shrink or
        # shift with the tested factor's NaN pattern (a factor-eligible benchmark
        # would otherwise depend on the very factor it is meant to measure).
        market_eligible = self._eligibility(
            np.ones((T, N), dtype=np.float64), close, tradable_mask, T, N
        )
        bench_nav = self._benchmark_nav(
            config, daily_ret, market_eligible, acct.nav_series, benchmark
        )

        # --- 10. assemble the report -------------------------------------------
        return self._assemble(
            run_id=run_id,
            factor_id=factor_id,
            config=config,
            acct=acct,
            bench_nav=bench_nav,
            dates=date_lbls,
            symbols=[str(s) for s in symbols],
            n_scheduled=n_scheduled,
            adj=self._adj(config),
        )

    # ================================================================= helpers ==

    @staticmethod
    def _identity(expr: str) -> tuple[str, str]:
        """Canonical expression + its ``factor_id`` (stable across syntaxes).

        Mirrors the service: ``str(parse(expr))`` is the canonical form and
        :meth:`FactorReport.compute_factor_id` its 16-hex id. Falls back to the raw
        expression when it does not parse, so even a malformed factor gets a stable
        id (the report will carry the failure note).
        """
        from assay.engine import parse
        from assay.library import FactorReport

        text = expr if isinstance(expr, str) else str(expr)
        try:
            canonical = str(parse(text))
        except Exception:
            canonical = text
        return canonical, FactorReport.compute_factor_id(canonical)

    @staticmethod
    def _adj(config: PortfolioBacktestConfig) -> str:
        """Adjustment basis in force (design-doc §7.2 ``lineage.adj_version``).

        Derived from ``market`` (which is part of the config-identity hash, so the
        basis never silently aliases two runs in the evaluation cache): A-share /
        HK use **total-return** (dividends reinvested — the correct basis for alpha
        P&L), US keeps the **split**-only basis it has always used. An explicit
        ``config.default_adj`` still wins if a caller sets one.
        """
        explicit = getattr(config, "default_adj", None)
        if explicit:
            return explicit
        return "total" if config.market in ("A", "HK") else "split"

    def _build_matrices(
        self, expr: str, config: PortfolioBacktestConfig, as_of: str | None
    ) -> tuple[Any, np.ndarray, np.ndarray, np.ndarray, list, list]:
        """Build the engine and pull the aligned factor / close / open matrices.

        The engine is constructed point-in-time as-of ``as_of`` (-> survivorship-safe
        universe + adjustment versioning, design-doc §7.1/§7.2). ``open`` falls back
        to ``close`` when the panel has no open column (so ``next_open`` execution
        still has a price).
        """
        from assay.engine import FactorEngine

        cutoff = as_of or config.as_of_date or config.period_end
        adj = self._adj(config)
        engine = FactorEngine.from_store(
            self.store,
            config.universe,
            (config.period_start, config.period_end),
            as_of=cutoff,
            adj=adj,
        )
        factor = np.asarray(engine.evaluate(expr).values, dtype=np.float64)
        close = np.asarray(engine.field_matrix("close"), dtype=np.float64)
        has_open = "open" in getattr(engine, "_field_cols", [])
        open_ = np.asarray(engine.field_matrix("open"), dtype=np.float64) if has_open else close
        return engine, factor, close, open_, list(engine.dates), list(engine.symbols)

    @staticmethod
    def _daily_returns(close: np.ndarray) -> np.ndarray:
        """``(T, N)`` per-name simple returns ``close_t / close_{t-1} - 1`` (row 0 = 0).

        NaN-aware: a non-finite price on either side leaves that name's return ``NaN``
        for the day; the accountant reads ``NaN`` as 0 (no contribution) so one
        missing symbol never poisons the date's portfolio return. ``r[t, i]`` is the
        return *into* date ``t`` — earned by the weight held over ``[t-1, t]``.
        """
        close = np.asarray(close, dtype=np.float64)
        T, N = close.shape
        out = np.full((T, N), np.nan, dtype=np.float64)
        if T >= 2:
            prev, cur = close[:-1], close[1:]
            with np.errstate(invalid="ignore", divide="ignore"):
                ok = np.isfinite(prev) & np.isfinite(cur) & (prev > 0)
                r = np.where(ok, cur / np.where(prev == 0, np.nan, prev) - 1.0, np.nan)
            out[1:] = r
        out[0] = 0.0  # no return on the first day (NAV starts 1.0)
        return out

    @staticmethod
    def _eligibility(
        factor: np.ndarray,
        close: np.ndarray,
        tradable_mask: np.ndarray | None,
        T: int,
        N: int,
    ) -> np.ndarray:
        """Per-date tradable universe (UniverseFilter): finite factor & finite price.

        ANDs in ``tradable_mask`` (ST/suspended) when supplied — broadcast from a
        ``(N,)`` per-symbol mask or used as a full ``(T, N)`` matrix; ``None`` =>
        no extra filter (the US default).
        """
        elig = np.isfinite(factor) & np.isfinite(close)
        if tradable_mask is not None:
            tm = np.asarray(tradable_mask)
            if tm.ndim == 1 and tm.shape[0] == N:
                tm = np.broadcast_to(tm.astype(bool), (T, N))
            elif tm.shape == (T, N):
                tm = tm.astype(bool)
            else:  # misaligned mask -> ignore rather than crash
                tm = np.ones((T, N), dtype=bool)
            elig = elig & tm
        return elig

    def _build_schedule(
        self,
        reb_idx: list[int],
        offset: int,
        signal: np.ndarray,
        daily_ret: np.ndarray,
        eligible: np.ndarray,
        config: PortfolioBacktestConfig,
        groups: np.ndarray | None,
        T: int,
        N: int,
    ) -> tuple[dict[int, Any], int]:
        """Map each rebalance to ``{execution_index: rebalance_closure}``.

        For each scheduled signal date ``s`` the execution date is ``s + offset``
        (``execution_offset_days``, >= 1 — no look-ahead). The schedule value is a
        *closure* (not a precomputed vector): the accountant calls it at the
        execution date with the **live drifted book**, and only then are
        :func:`construct_weights` (with a trailing returns window for the risk-aware
        methods) and :func:`apply_constraints` evaluated — so the optimiser's
        warm-start and the §2.5 turnover cap diff against the *actual* current
        position rather than a stale prior target (design-doc 2.5/3.3). For the
        ``threshold`` type the closure additionally applies the :func:`should_rebalance`
        gate, comparing the new target's weights/ranks to the live book and returning
        ``None`` (no trade) when neither breaches its threshold (design-doc 3.2).

        Two executions colliding on the same date keep the later signal (a dict
        overwrite); the accountant applies one rebalance per keyed date.
        """
        schedule: dict[int, Any] = {}
        if not reb_idx:
            return schedule, 0

        cov_window = max(2, int(config.cov_window))
        is_threshold = config.rebalance_type == "threshold"
        # Threshold gate state, threaded across closures in execution order (the
        # accountant invokes them chronologically, so this evolves with the book).
        gate_state = {"prev_ranks": np.full(N, np.nan), "established": False}
        n_scheduled = 0

        for s in reb_idx:
            exec_idx = s + offset
            if exec_idx >= T:
                continue  # signal too late to execute within the period
            sig_row = signal[s]
            if not np.isfinite(sig_row).any():
                continue  # no eligible name on the signal date
            schedule[exec_idx] = self._make_rebalance_fn(
                s, sig_row, daily_ret, cov_window, config, groups,
                is_threshold, gate_state,
            )
            n_scheduled += 1

        return schedule, n_scheduled

    def _make_rebalance_fn(
        self,
        s: int,
        sig_row: np.ndarray,
        daily_ret: np.ndarray,
        cov_window: int,
        config: PortfolioBacktestConfig,
        groups: np.ndarray | None,
        is_threshold: bool,
        gate_state: dict[str, Any],
    ):
        """Build the per-rebalance closure the accountant calls with the live book.

        The returned ``fn(cur_w)`` constructs the target weights from the signal at
        ``s`` against the *drifted* current book ``cur_w`` — so the mean-variance
        warm-start and the turnover cap in :func:`apply_constraints` measure the
        trade against the real position (fixes the stale-``prev_target`` baseline).
        For ``threshold`` rebalancing it then runs :func:`should_rebalance` on the
        new target vs the live book (weight drift) and the new vs prior decile ranks
        (rank shift), returning ``None`` when the gate does not fire — except the
        first established position, which always trades. ``gate_state`` (shared
        across the run's closures) carries the prior ranks and the established flag.
        """

        def _fn(cur_w: np.ndarray) -> np.ndarray | None:
            returns_window = self._returns_window(daily_ret, s, cov_window)
            raw = construct_weights(sig_row, returns_window, cur_w, config)
            target = apply_constraints(raw, cur_w, config, sectors=groups)
            if is_threshold:
                new_ranks = normalize(sig_row, method="rank").ravel() * 10.0  # decile
                # Gate the *target* (real weights) against the live drifted book and
                # the new decile ranks against the prior ones.
                fire, _ = should_rebalance(
                    cur_w, target, gate_state["prev_ranks"], new_ranks, config
                )
                gate_state["prev_ranks"] = new_ranks
                if gate_state["established"] and not fire:
                    return None  # neither drift nor rank-shift breached → hold
                gate_state["established"] = True
            return target

        return _fn

    @staticmethod
    def _returns_window(daily_ret: np.ndarray, s: int, window: int) -> np.ndarray:
        """Trailing ``(W, N)`` returns block ending at the signal date ``s`` (inclusive).

        Used only by the risk-aware weight methods (``mv`` / ``risk_parity`` / ``bl``)
        for covariance; the cheap constructors ignore it. Strictly historical (rows
        ``[s-W+1 .. s]``) so it never peeks past the signal date.
        """
        lo = max(0, s - window + 1)
        return daily_ret[lo : s + 1]

    def _exec_price_matrix(
        self, config: PortfolioBacktestConfig, open_: np.ndarray, close: np.ndarray
    ) -> np.ndarray:
        """``(T, N)`` execution-price matrix per ``config.execution_price``.

        ``next_open`` / ``arrival`` -> the open at the execution date; ``next_close``
        / ``vwap`` -> the close (Assay has no intraday VWAP, so close is the honest
        proxy — documented). The ``execution_offset_days`` lag that makes 'next' the
        *next bar* is already applied by indexing the schedule at ``s + offset``, so
        this returns the price *at* the execution bar.
        """
        ep = config.execution_price
        if ep in ("next_close", "vwap"):
            return np.asarray(close, dtype=np.float64)
        # next_open / arrival -> open of the execution bar.
        return np.asarray(open_, dtype=np.float64)

    def _cn_limit_matrices(
        self,
        config: PortfolioBacktestConfig,
        close_adj: np.ndarray,
        dates: list,
        symbols: list,
        as_of,
    ) -> tuple[np.ndarray | None, np.ndarray | None]:
        """Per-board A-share price-limit bands as ``(T, N)``, or ``(None, None)``.

        Loads the store's ``trade_status`` (raw 涨停价/跌停价 + raw close) for the
        engine's exact ``dates`` × ``symbols`` grid and rebases each raw band into
        the panel's adjustment basis: the executor compares the *adjusted*
        ``exec_price`` to these bands, while the limits are quoted on *raw* prices,
        so each band is multiplied by ``f = adj_close / raw_close`` (the same
        forward factor the engine applied). ``f`` cancels in the inequality, so
        ``exec_price >= up`` is basis-consistent.

        No-ops (``None, None``) unless ``market == 'A'`` and the store exposes a
        non-empty ``trade_status`` — so US and offline/monkeypatched-engine runs
        are unaffected. Any failure degrades to ``None`` (the constraint goes
        inert) rather than breaking the backtest.
        """
        if config.market != "A" or self.store is None:
            return None, None
        getter = getattr(self.store, "get_trade_status", None)
        if getter is None:
            return None, None
        try:
            ts = getter(symbols, dates[0], dates[-1], as_of)
        except Exception:  # noqa: BLE001 — store hiccup must not break the run
            return None, None
        if ts is None or getattr(ts, "is_empty", lambda: True)():
            return None, None

        import datetime as _dt

        def _norm(d):
            # Engine dates are numpy datetime64; trade_status dates are python
            # datetime.date — normalise both to datetime.date so the grid aligns.
            if isinstance(d, np.datetime64):
                d = d.astype("datetime64[D]").item()
            if isinstance(d, _dt.datetime):
                return d.date()
            return d

        T, N = len(dates), len(symbols)
        di = {_norm(d): i for i, d in enumerate(dates)}
        sj = {str(s): j for j, s in enumerate(symbols)}
        up = np.full((T, N), np.nan, dtype=np.float64)
        dn = np.full((T, N), np.nan, dtype=np.float64)
        raw = np.full((T, N), np.nan, dtype=np.float64)
        for d, s, u, lo, c in zip(
            ts["date"], ts["symbol"], ts["up_limit"], ts["down_limit"], ts["close"]
        ):
            i = di.get(_norm(d))
            j = sj.get(str(s))
            if i is None or j is None:
                continue
            up[i, j] = u
            dn[i, j] = lo
            raw[i, j] = c

        ca = np.asarray(close_adj, dtype=np.float64)
        with np.errstate(invalid="ignore", divide="ignore"):
            f = np.where((raw > 0) & np.isfinite(ca), ca / raw, np.nan)
        return up * f, dn * f

    def _load_groups(self, symbols: list, as_of) -> np.ndarray | None:
        """``(N,)`` industry/sector labels aligned to ``symbols``, or ``None``.

        Reads the store's ``security_groups`` (no-op when the store lacks it, e.g.
        US / offline runs). Symbols with no label fall into ``'UNKNOWN'`` so the
        neutraliser still treats them as a (residual) group rather than dropping
        them. Returns ``None`` if no labels are available at all.
        """
        if self.store is None:
            return None
        getter = getattr(self.store, "get_groups", None)
        if getter is None:
            return None
        try:
            mapping = getter(symbols, as_of)
        except Exception:  # noqa: BLE001 — a store hiccup must not break the run
            return None
        if not mapping:
            return None
        return np.array([str(mapping.get(str(s), "UNKNOWN")) for s in symbols], dtype=object)

    @staticmethod
    def _as_tn(x: np.ndarray | None, T: int, N: int) -> np.ndarray | None:
        """Coerce an optional ``(N,)`` / ``(T, N)`` float input to ``(T, N)`` or ``None``.

        A ``(N,)`` vector is broadcast across dates; a misaligned shape is dropped to
        ``None`` (the constraint no-ops) rather than crashing the run.
        """
        if x is None:
            return None
        a = np.asarray(x, dtype=np.float64)
        if a.ndim == 1 and a.shape[0] == N:
            return np.broadcast_to(a, (T, N)).copy()
        if a.shape == (T, N):
            return a
        return None

    @staticmethod
    def _as_tn_bool(x: np.ndarray | None, T: int, N: int) -> np.ndarray | None:
        """Coerce an optional ``(N,)`` / ``(T, N)`` bool mask to ``(T, N)`` or ``None``."""
        if x is None:
            return None
        a = np.asarray(x)
        if a.ndim == 1 and a.shape[0] == N:
            return np.broadcast_to(a.astype(bool), (T, N)).copy()
        if a.shape == (T, N):
            return a.astype(bool)
        return None

    def _benchmark_nav(
        self,
        config: PortfolioBacktestConfig,
        daily_ret: np.ndarray,
        eligible: np.ndarray,
        port_nav: np.ndarray,
        custom: Sequence[float] | np.ndarray | None,
    ) -> np.ndarray | None:
        """Benchmark NAV series aligned to the portfolio (design-doc §2.8).

        * ``'custom'`` — the caller-supplied ``custom`` NAV series (length ``T``;
          rebased to 1.0). ``None`` falls through to the equal-weight proxy.
        * ``'equal_weight'`` / ``'index'`` — an **equal-weight eligible-universe**
          daily-return index. NOTE: a true index benchmark needs an index price
          series Assay does not have for any universe, so this proxy stands in for
          it (documented). Each day the benchmark earns the equal-weight mean of the
          eligible names' returns.
        * ``'none'`` / ``'cash'`` — flat risk-free cash: NAV compounding the daily
          risk-free rate (``risk_free_rate / 252``), i.e. ~flat for a 0 rate.

        Returns ``None`` only if no sensible series can be formed (degenerate T).
        """
        T = daily_ret.shape[0]
        if T == 0:
            return None
        bench = config.benchmark

        if bench == "custom" and custom is not None:
            c = np.asarray(custom, dtype=np.float64).reshape(-1)
            if c.size == 0:
                return None
            if c.size != T:  # length-mismatch: clip/pad to T by holding the last value
                fixed = np.full(T, np.nan)
                m = min(T, c.size)
                fixed[:m] = c[:m]
                c = fixed
            base = c[np.isfinite(c)]
            if base.size == 0 or base[0] == 0:
                return None
            return c / base[0]  # rebase to 1.0

        if bench in ("none", "cash"):
            rf_daily = float(getattr(config, "risk_free_rate", 0.0) or 0.0) / _PPY
            nav = np.empty(T, dtype=np.float64)
            v = 1.0
            for t in range(T):
                v *= 1.0 + (0.0 if t == 0 else rf_daily)
                nav[t] = v
            return nav

        # 'equal_weight' / 'index' (default): equal-weight eligible-universe proxy.
        ew_ret = np.zeros(T, dtype=np.float64)
        for t in range(T):
            row = daily_ret[t]
            elig_t = eligible[t] & np.isfinite(row)
            if t == 0 or not elig_t.any():
                ew_ret[t] = 0.0
            else:
                ew_ret[t] = float(row[elig_t].mean())
        return np.cumprod(1.0 + ew_ret)

    def _assemble(
        self,
        *,
        run_id: str,
        factor_id: str,
        config: PortfolioBacktestConfig,
        acct,
        bench_nav: np.ndarray | None,
        dates: list[str],
        symbols: list[str],
        n_scheduled: int,
        adj: str,
    ) -> PortfolioReport:
        """Fold the accountant output + metrics into a :class:`PortfolioReport`.

        Computes every section-4 scalar via :func:`compute_metrics` (passing the
        gross NAV for ``cost_drag`` and the per-rebalance one-way turnover for
        ``annual_turnover`` / ``avg_holding_days``), resolves the drawdown date
        labels from the returned indices, attaches the optional trade/position logs
        per the §2.9 flags, and fills ``a_share_metrics`` only when ``market == 'A'``.
        """
        nav = acct.nav_series
        gross = acct.gross_nav
        n_rebal = len(acct.rebalance_dates_idx)
        one_way = (
            float(np.mean(acct.turnover_per_rebalance))
            if acct.turnover_per_rebalance
            else 0.0
        )

        m = compute_metrics(
            nav,
            dates,
            bench_nav=bench_nav,
            config=config,
            gross_nav=gross,
            one_way_per_rebal=one_way if n_rebal > 0 else None,
            n_rebalances=n_rebal if n_rebal > 0 else None,
        )

        # Drawdown date labels from the metric's indices.
        def _date_at(i: int) -> str | None:
            return dates[i] if isinstance(i, int) and 0 <= i < len(dates) else None

        mdd_start = _date_at(m.get("max_drawdown_peak_idx", -1))
        mdd_end = _date_at(m.get("max_drawdown_trough_idx", -1))

        out_freq = getattr(config, "output_frequency", "daily")
        nav_dates, nav_series, bench_series = self._sample_series(
            dates, nav, bench_nav, out_freq
        )

        trade_log = list(acct.trades) if config.save_trade_log else []
        position_log = (
            self._position_log(acct.weights, dates, nav, symbols)
            if config.save_position_log
            else []
        )

        a_share = self._a_share_metrics(config, acct, nav) if config.market == "A" else None

        return PortfolioReport(
            run_id=run_id,
            factor_id=factor_id,
            config=config.to_dict(),
            period_start=dates[0] if dates else config.period_start,
            period_end=dates[-1] if dates else config.period_end,
            n_trading_days=len(dates),
            n_rebalances=n_rebal,
            total_return=m["total_return"],
            annual_return=m["annual_return"],
            gross_return=m["gross_return"],
            excess_return=m["excess_return"],
            sharpe=m["sharpe"],
            sortino=m["sortino"],
            calmar=m["calmar"],
            information_ratio=m["information_ratio"],
            max_drawdown=m["max_drawdown"],
            max_drawdown_start=mdd_start,
            max_drawdown_end=mdd_end,
            drawdown_recovery_days=m.get("drawdown_recovery_days"),
            beta=m["beta"],
            alpha_capm=m["alpha_capm"],
            tracking_error=m["tracking_error"],
            annual_turnover=m["annual_turnover"],
            cost_drag=m["cost_drag"],
            avg_holding_days=m["avg_holding_days"],
            nav_series=nav_series,
            nav_dates=nav_dates,
            benchmark_series=bench_series,
            monthly_returns=m.get("monthly_returns", {}),
            trade_log=trade_log,
            position_log=position_log,
            attribution=None,
            a_share_metrics=a_share,
            lineage=PortfolioLineage(
                data_snapshot=self._data_snapshot(),
                eval_timestamp=None,  # the caller (service) stamps wall-clock time
                adj_version=adj,
            ),
        )

    @staticmethod
    def _sample_series(
        dates: list[str],
        nav: np.ndarray,
        bench_nav: np.ndarray | None,
        out_freq: str,
    ) -> tuple[list[str], list[float], list[float]]:
        """Down-sample the NAV / benchmark series to ``output_frequency`` (design-doc §2.9).

        ``daily`` keeps every point; ``weekly`` / ``monthly`` keep the last point of
        each ISO-week / calendar-month group (always including the final point). The
        benchmark is sampled on the same indices so the two stay aligned.
        """
        navl = [float(x) for x in np.asarray(nav, dtype=np.float64)]
        benchl = (
            [float(x) for x in np.asarray(bench_nav, dtype=np.float64)]
            if bench_nav is not None
            else []
        )
        if out_freq == "daily" or len(dates) <= 1:
            return list(dates), navl, benchl

        import pandas as pd

        idx = pd.DatetimeIndex(pd.to_datetime(dates, errors="coerce"))
        if out_freq == "weekly":
            iso = idx.isocalendar()
            keys = list(zip(iso["year"].to_numpy(), iso["week"].to_numpy()))
        else:  # monthly
            keys = list(zip(idx.year.to_numpy(), idx.month.to_numpy()))
        picks: list[int] = []
        for i in range(len(dates)):
            if i + 1 == len(dates) or keys[i + 1] != keys[i]:
                picks.append(i)
        s_dates = [dates[i] for i in picks]
        s_nav = [navl[i] for i in picks]
        s_bench = [benchl[i] for i in picks] if benchl else []
        return s_dates, s_nav, s_bench

    @staticmethod
    def _position_log(
        weights: np.ndarray, dates: list[str], nav: np.ndarray, symbols: list[str]
    ) -> list:
        """Daily :class:`PositionSnapshot` list (design-doc §2.9 ``save_position_log``).

        Each date carries the non-zero weights (symbol -> weight), the NAV, and the
        residual cash weight ``1 - sum(weights)`` (signed; absorbs the long/short
        net residual).
        """
        from assay.portfolio.report import PositionSnapshot

        w = np.asarray(weights, dtype=np.float64)
        navl = np.asarray(nav, dtype=np.float64)
        T = min(w.shape[0], len(dates), navl.shape[0])
        log = []
        for t in range(T):
            row = w[t]
            held = {
                str(symbols[j]): float(row[j])
                for j in range(row.shape[0])
                if np.isfinite(row[j]) and row[j] != 0.0
            }
            log.append(
                PositionSnapshot(
                    date=dates[t],
                    weights=held,
                    nav=float(navl[t]) if np.isfinite(navl[t]) else float("nan"),
                    cash=float(1.0 - sum(held.values())),
                )
            )
        return log

    @staticmethod
    def _a_share_metrics(
        config: PortfolioBacktestConfig, acct, nav: np.ndarray
    ) -> dict[str, Any]:
        """A-share section-4.4 metrics from the execution diagnostics (market == 'A').

        ``limit_hit_rate`` = price-limit-blocked trades / total trade attempts;
        ``forced_hold_ratio`` = T+1-deferred sells / total trade attempts. The
        data-dependent metrics (suspension impact, ST-exposure days, index-recon
        alpha, northbound corr) need ST/suspension/index/flow data the DataStore
        lacks, so they are reported ``None`` rather than fabricated (documented).
        """
        diag = acct.diag
        attempts = (
            diag.get("n_trades", 0.0)
            + diag.get("limit_hit_count", 0.0)
            + diag.get("forced_hold_count", 0.0)
            + diag.get("blocked_suspended", 0.0)
            + diag.get("blocked_adv", 0.0)
        )
        denom = attempts if attempts > 0 else float("nan")
        return {
            "limit_hit_rate": float(diag.get("limit_hit_count", 0.0) / denom)
            if denom == denom
            else None,
            "forced_hold_ratio": float(diag.get("forced_hold_count", 0.0) / denom)
            if denom == denom
            else None,
            "n_limit_hits": int(diag.get("limit_hit_count", 0.0)),
            "n_forced_holds": int(diag.get("forced_hold_count", 0.0)),
            "n_blocked_suspended": int(diag.get("blocked_suspended", 0.0)),
            "n_blocked_adv": int(diag.get("blocked_adv", 0.0)),
            # data-dependent (no ST/suspension/index/flow data) -> not fabricated:
            "suspension_impact": None,
            "st_exposure_days": None,
            "index_recon_alpha": None,
            "northbound_flow_corr": None,
        }

    def _data_snapshot(self) -> str | None:
        """Best-effort DataStore snapshot id for lineage (design-doc §7.2).

        Reads ``store.snapshot_id`` / ``store.data_snapshot`` if the store exposes
        one; ``None`` otherwise (e.g. the monkeypatched test engine has no store).
        """
        for attr in ("snapshot_id", "data_snapshot", "snapshot"):
            v = getattr(self.store, attr, None)
            if isinstance(v, str) and v:
                return v
        return None

    def _empty_report(
        self,
        run_id: str,
        factor_id: str,
        config: PortfolioBacktestConfig,
        *,
        note: str,
        dates: list | None = None,
    ) -> PortfolioReport:
        """A well-formed but empty-NAV report for a degenerate run (never raise).

        Carries the diagnostic ``note`` in ``attribution`` (so an agent loop / UI can
        surface *why* the backtest produced nothing) and leaves every metric ``NaN``
        / empty — mirroring the engine's diagnostics philosophy.
        """
        date_lbls = [str(d) for d in dates] if dates else []
        return PortfolioReport(
            run_id=run_id,
            factor_id=factor_id,
            config=config.to_dict(),
            period_start=date_lbls[0] if date_lbls else config.period_start,
            period_end=date_lbls[-1] if date_lbls else config.period_end,
            n_trading_days=len(date_lbls),
            n_rebalances=0,
            attribution={"note": note},
            a_share_metrics=None,
            lineage=PortfolioLineage(
                data_snapshot=self._data_snapshot(),
                eval_timestamp=None,
                adj_version=self._adj(config),
            ),
        )


# ---------------------------------------------------------------------------
# convenience
# ---------------------------------------------------------------------------
def run_portfolio_backtest(
    expr: str,
    config: PortfolioBacktestConfig,
    *,
    store: Any = None,
    **kw: Any,
) -> PortfolioReport:
    """One-shot helper: build a :class:`PortfolioBacktester` and :meth:`run` it.

    ``store`` defaults to ``None`` (the offline/monkeypatched-engine path);
    remaining keywords (``as_of``, ``groups``, ``tradable_mask``, ``prev_close``,
    ``adv``, ``benchmark``) pass straight through to :meth:`PortfolioBacktester.run`.
    """
    return PortfolioBacktester(store=store).run(expr, config, **kw)
