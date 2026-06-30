""":class:`AssayService` — the singleton facade every surface routes through.

Architecture §2: in the target design the SDK, REST API, MCP server and WebUI all
call one in-process :class:`AssayService` that owns the data store, the factor
engine, the two-level cache and the factor library. This module implements that
facade on top of the now-existing layers:

* :class:`~assay.data.store.DataStore`        — point-in-time price panels (§3.4).
* :class:`~assay.engine.FactorEngine`         — parse + evaluate to ``(T, N)`` matrices.
* :mod:`assay.evaluator`                       — IC / RankIC / decay / groups / turnover (§6).
* :class:`~assay.library.FactorLibrary`        — append-only report store + redundancy (§7.2).
* :class:`~assay.cache.SessionRegistry`        — per-session panel/forward-return cache (§5).
* :class:`~assay.cache.L2FactorCache`          — cross-session factor-result cache (§5.4).

The service is a thin orchestrator: it resolves keyword defaults from
:class:`~assay.config.AssayConfig`, drives the engine + evaluator, and assembles a
:class:`~assay.library.FactorReport` (engineering-docs §7.2). It never re-implements
numerics — those live in the evaluator — and it builds the :class:`DataStore`
**lazily** so importing ``assay`` and querying the library work without MASSIVE
credentials (architecture §2, "never instantiated more than once per process").

House style: ``from __future__ import annotations``, dataclasses/type hints, numpy
``(T, N)`` float64 core, polars frames, NaN-aware throughout.
"""

from __future__ import annotations

import datetime as dt
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, AsyncGenerator, Iterable

import numpy as np

from assay import evaluator
from assay.cache import L2FactorCache, SessionCache, SessionRegistry
from assay.config import AssayConfig
from assay.engine import FactorEngine, parse
from assay.library import FactorLibrary, FactorReport, FactorSummary, Lineage

__all__ = ["AssayService"]


def _f(v) -> float | None:
    """Coerce to a JSON-safe float; None/NaN/inf -> None."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if (f == f and f not in (float("inf"), float("-inf"))) else None


def _resample_ohlcv(df, rule: str):
    """Resample a daily ``[date, open, high, low, close, volume]`` frame to ``1w``/``1mo``.

    OHLC aggregation is first-open / max-high / min-low / last-close, volume summed;
    the bucket is labelled by its last in-bucket trading date so the x-axis reads as
    real sessions. Uses polars ``group_by_dynamic`` (date is the index).
    """
    import polars as pl

    every = "1w" if rule == "1w" else "1mo"
    out = (
        df.sort("date")
        .group_by_dynamic("date", every=every, label="left", closed="left")
        .agg(
            pl.col("open").first(),
            pl.col("high").max(),
            pl.col("low").min(),
            pl.col("close").last(),
            pl.col("volume").sum(),
            pl.col("date").last().alias("_last"),
        )
        .with_columns(pl.col("_last").alias("date"))
        .drop("_last")
        .sort("date")
    )
    return out


# Sort keys that are "higher is better" (descending); None/NaN-tolerant.
def _sort_key(report: FactorReport | None, attr: str) -> float:
    """Numeric sort key for ``report.attr``; None / NaN / missing -> -inf (ranks last)."""
    if report is None:
        return float("-inf")
    v = getattr(report, attr, None)
    if v is None:
        return float("-inf")
    try:
        f = float(v)
    except (TypeError, ValueError):
        return float("-inf")
    return f if f == f else float("-inf")  # NaN -> -inf


class AssayService:
    """Singleton service wiring the data, engine, cache, evaluator and library.

    Construct via :meth:`init` (once per process) and retrieve with :meth:`get`.
    The :class:`DataStore` is built lazily on first data access (see
    :attr:`store`), so the service — and therefore ``import assay`` — works with no
    MASSIVE credentials as long as only the library / offline paths are touched.
    """

    _instance: "AssayService | None" = None

    # ------------------------------------------------------------------ init --
    # Which market each known universe lives in (drives per-market store routing).
    _UNIVERSE_MARKET = {
        "NASDAQ100": "US", "SP500": "US", "RUSSELL2000": "US",
        "CSI300": "CN", "CSI500": "CN", "CSI1000": "CN", "CSI800": "CN",
        "HSI": "HK",
    }

    def __init__(self, config: AssayConfig) -> None:
        self.config = config
        # DataStore is the only credential-hungry dependency: build it lazily.
        self._store = None
        self._stores: dict[str, object] = {}  # market -> DataStore (multi-market serving)
        self.library = FactorLibrary(config.library_path)
        self.cache = L2FactorCache(config.cache_path)
        self.sessions = SessionRegistry()
        # Apply admin-editable system settings (parallelism / cache / eval defaults)
        # onto the live config, so a customised store takes effect at startup.
        self.apply_system_config()

    # ----------------------------------------------------- system settings -----
    def apply_system_config(self) -> dict:
        """Apply the admin ``system`` settings onto the live :class:`AssayConfig`.

        Reads the editable ``system`` section (parallelism / cache budgets /
        evaluation defaults / precompute knobs) and writes it onto ``self.config``,
        so changes saved via ``PUT /v1/admin/config`` take effect on the **next
        request** without a restart (the dataclass is intentionally non-frozen). The
        hot-changeable knobs — default universe/period/horizons/execution/adj,
        ``n_workers``, and the precompute parameters — are all read live by
        :meth:`_resolve`, :meth:`batch`, :meth:`evaluate_many` and the refresh hook.
        Best-effort: a missing / malformed store leaves the config untouched.
        """
        from assay import config_store

        try:
            sysc = config_store.persisted_system()  # only operator-saved keys
        except Exception:  # noqa: BLE001
            return {}
        if not sysc:
            return {}
        c = self.config

        def _set(attr: str, key: str, cast) -> None:
            if key in sysc and sysc[key] not in (None, ""):
                try:
                    setattr(c, attr, cast(sysc[key]))
                except (TypeError, ValueError):
                    pass

        _set("n_workers", "n_workers", int)
        _set("l1_memory_gb", "l1_memory_gb", float)
        _set("l2_max_gb", "l2_max_gb", float)
        _set("default_universe", "default_universe", str)
        _set("default_execution", "default_execution", str)
        _set("default_adj", "default_adj", str)
        _set("default_frequency", "default_frequency", str)
        _set("annualization_basis", "annualization_basis", str)
        ps, pe = sysc.get("default_period_start"), sysc.get("default_period_end")
        if ps and pe:
            c.default_period = (str(ps), str(pe))
        h = sysc.get("default_horizons")
        if h:
            try:
                hs = tuple(int(x) for x in str(h).replace(" ", "").split(",") if x)
                if hs:
                    c.default_horizons = hs
            except (TypeError, ValueError):
                pass
        # precompute knobs live on the config as plain attributes (read by the
        # refresh hook / evaluate_many); AssayConfig is non-frozen so this is fine.
        c.precompute_enabled = bool(sysc.get("precompute_enabled", True))
        c.precompute_auto_refresh = bool(sysc.get("precompute_auto_refresh", True))
        try:
            c.precompute_top_k = int(sysc.get("precompute_top_k", 256) or 256)
            c.precompute_min_count = int(sysc.get("precompute_min_count", 2) or 2)
        except (TypeError, ValueError):
            c.precompute_top_k, c.precompute_min_count = 256, 2
        c.precompute_corpus = (sysc.get("precompute_corpus") or "") or None
        try:
            c.risk_free_rate = float(sysc.get("risk_free_rate", 0.015))
        except (TypeError, ValueError):
            c.risk_free_rate = 0.015
        return sysc

    # ------------------------------------------------------- market routing ----
    def _market_for(self, universe: str | None) -> str:
        """Resolve the market a universe belongs to (defaults to the config market)."""
        return self._UNIVERSE_MARKET.get((universe or "").upper(), self.config.market)

    def _config_for_market(self, market: str):
        """Config view for ``market``: the same config, but with that market's data_dir.

        If ``market`` matches the configured market, the config is returned unchanged.
        Otherwise a shallow copy is made with ``market`` set and ``data_dir`` switched to
        ``config.market_dirs[market]`` when present (else the canonical data_dir, which
        may itself hold a ``price_raw/market={market}/`` subtree).
        """
        if market == self.config.market and market not in self.config.market_dirs:
            return self.config
        import dataclasses

        data_dir = self.config.market_dirs.get(market, self.config.data_dir)
        # library/cache stay on the primary data_dir so saved factors are shared.
        return dataclasses.replace(
            self.config, market=market, data_dir=data_dir,
            library_dir=self.config.library_path, cache_dir=self.config.cache_path,
        )

    def store_for_universe(self, universe: str | None):
        """The :class:`DataStore` that serves ``universe`` (per its market)."""
        return self._store_for(self._market_for(universe))

    def _store_for(self, market: str):
        """Lazily build + cache one :class:`DataStore` per market."""
        if market not in self._stores:
            from assay.data.store import DataStore

            self._stores[market] = DataStore(self._config_for_market(market))
        return self._stores[market]

    @classmethod
    def init(cls, config: AssayConfig) -> "AssayService":
        """Create (or replace) the process-wide singleton and return it."""
        cls._instance = cls(config)
        return cls._instance

    @classmethod
    def get(cls) -> "AssayService":
        """Return the live singleton; raise if :meth:`init` has not been called."""
        if cls._instance is None:
            raise RuntimeError(
                "AssayService not initialized — call AssayService.init(config) "
                "or assay.init() first."
            )
        return cls._instance

    @property
    def store(self):
        """Lazily-built :class:`DataStore` for the configured (default) market."""
        return self._store_for(self.config.market)

    # -------------------------------------------------------- default helpers --
    def _resolve(
        self,
        universe: str | None,
        period: tuple[str, str] | None,
        horizons: Iterable[int] | None,
        execution: str | None,
        as_of: str | None,
        adj: str | None,
    ) -> tuple[str, tuple[str, str], list[int], str, str, str]:
        """Fill missing evaluation parameters from the config defaults."""
        cfg = self.config
        universe = universe or cfg.default_universe
        period = tuple(period) if period else tuple(cfg.default_period)  # type: ignore[assignment]
        horizons = sorted({int(h) for h in (horizons or cfg.default_horizons)})
        execution = execution or cfg.default_execution
        as_of = as_of or period[1]
        adj = adj or cfg.default_adj
        return universe, period, horizons, execution, as_of, adj  # type: ignore[return-value]

    # ----------------------------------------------------------- engine setup --
    def _build_engine(
        self,
        universe: str,
        period: tuple[str, str],
        as_of: str,
        adj: str,
        group_data: dict[str, dict[str, object]] | None,
    ) -> FactorEngine:
        """Cold-path engine over a fresh PIT panel (architecture §2.1).

        Reads from the store that serves ``universe``'s market, so one service can
        evaluate US (NASDAQ100) and A-share (CSI300/…) factors side by side.
        """
        return FactorEngine.from_store(
            self.store_for_universe(universe),
            universe=universe,
            period=period,
            as_of=as_of,
            adj=adj,
            group_data=group_data,
        )

    def _session_engine(
        self,
        session_id: str,
        horizons: list[int],
        execution: str,
    ) -> tuple[FactorEngine, dict[int, np.ndarray]]:
        """Return the cached engine + forward returns for ``session_id``.

        The engine is built once per session (memoised on the :class:`SessionCache`)
        and forward returns are computed once per ``(execution, horizons)`` combo and
        attached to the session, so the 2nd+ factor in a session skips both
        (engineering-docs §5/§6.1).
        """
        sess = self.sessions.get(session_id)
        if sess is None:
            raise ValueError(f"unknown session_id {session_id!r} (expired or never created)")
        meta = sess.get_or_compute("_meta", dict)
        engine: FactorEngine = sess.get_or_compute(
            "engine",
            lambda: self._build_engine(
                meta["universe"], meta["period"], meta["as_of"], meta["adj"],
                meta.get("group_data"),
            ),
        )
        fwd = self._session_forward_returns(sess, engine, horizons, execution)
        return engine, fwd

    def _session_forward_returns(
        self,
        sess: SessionCache,
        engine: FactorEngine,
        horizons: list[int],
        execution: str,
    ) -> dict[int, np.ndarray]:
        """Forward returns for ``horizons`` under ``execution``, memoised per session."""
        # Key folds in execution: next_open / next_close give different matrices.
        key = f"fwd::{execution}"
        cache: dict[int, np.ndarray] = sess.get_or_compute(key, dict)
        missing = [h for h in horizons if h not in cache]
        if missing:
            close = engine.field_matrix("close")
            open_ = engine.field_matrix("open") if "open" in engine._field_cols else None
            new = evaluator.forward_returns(close, open_, missing, execution)
            cache.update(new)
        return {h: cache[h] for h in horizons}

    # ----------------------------------------------------------- evaluation ----
    def evaluate(
        self,
        expr,
        *,
        universe: str | None = None,
        period: tuple[str, str] | None = None,
        horizons: Iterable[int] | None = None,
        execution: str | None = None,
        neutralize: list[str] | None = None,
        as_of: str | None = None,
        adj: str | None = None,
        session_id: str | None = None,
        group_data: dict[str, dict[str, object]] | None = None,
        save: bool = False,
    ) -> FactorReport:
        """Evaluate one factor expression into a scored :class:`FactorReport`.

        Resolves defaults from the config, obtains an engine (a cached session engine
        when ``session_id`` is given, otherwise a fresh cold-path engine), and runs
        :meth:`FactorEngine.diagnose`. On a diagnostic error the report carries the
        ``failure_mode`` / ``suggestion`` / ``lookahead_detected`` flags and null
        metrics; otherwise the evaluator computes IC/RankIC, decay, group returns and
        turnover and the assembled report is returned (and persisted when ``save``).
        """
        t0 = time.perf_counter()
        universe, period, horizons, execution, as_of, adj = self._resolve(
            universe, period, horizons, execution, as_of, adj
        )
        expr_str = expr if isinstance(expr, str) else str(expr)

        # Acquire engine (+ session-shared forward returns when in a session).
        # A missing/empty panel (no ingested data for this universe+period, e.g. an
        # A-share universe with no CN store) must degrade to a NO_DATA report, never a
        # 500 — the SSE stream and the SDK both surface it cleanly.
        try:
            if session_id is not None:
                engine, fwd_by_h = self._session_engine(session_id, horizons, execution)
            else:
                engine = self._build_engine(universe, period, as_of, adj, group_data)
                fwd_by_h = None  # computed after a successful diagnose (below)
        except (ValueError, FileNotFoundError) as exc:
            duration_ms = (time.perf_counter() - t0) * 1000.0
            return self._no_data_report(
                expr_str, universe, period, execution, neutralize, adj, duration_ms, str(exc)
            )

        # Diagnose: parse + static checks + evaluate + output-quality, never raises.
        fd = engine.diagnose(expr)
        if not fd.ok:
            duration_ms = (time.perf_counter() - t0) * 1000.0
            return self._failed_report(
                expr_str, fd, universe, period, execution, neutralize, adj, duration_ms
            )

        factor = fd.result  # FactorResult
        if fwd_by_h is None:
            fwd_by_h = self._cold_forward_returns(engine, horizons, execution)

        # --- predictive quality (factor ranked once, reused across horizons) ---
        metrics = evaluator.evaluate_ic(factor.values, fwd_by_h)
        decay = evaluator.decay_halflife(metrics["ic_by_horizon"])
        groups = evaluator.group_returns(factor.values, fwd_by_h[min(horizons)])
        turn = evaluator.turnover(factor.values)

        # --- redundancy: cheap best-effort, never re-scores the whole library ---
        # TODO(redundancy): wire library.correlation_matrix on a shared engine when a
        # cheap incremental similarity index exists; re-evaluating every library
        # factor per call is too expensive for the hot path (engineering-docs §6.2).
        redundancy_score, most_similar = 0.0, None

        report = self._assemble_report(
            expr_str=expr_str,
            factor=factor,
            metrics=metrics,
            decay=decay,
            groups=groups,
            turnover=turn,
            redundancy_score=redundancy_score,
            most_similar=most_similar,
            universe=universe,
            period=period,
            execution=execution,
            neutralize=neutralize,
            adj=adj,
            diagnostics=fd,
            duration_ms=(time.perf_counter() - t0) * 1000.0,
        )
        if save:
            self.library.save(report)
        return report

    # -- forward-returns for the cold (sessionless) path ----------------------
    def _cold_forward_returns(
        self, engine: FactorEngine, horizons: list[int], execution: str
    ) -> dict[int, np.ndarray]:
        close = engine.field_matrix("close")
        open_ = engine.field_matrix("open") if "open" in engine._field_cols else None
        return evaluator.forward_returns(close, open_, horizons, execution)

    # -- report assembly ------------------------------------------------------
    def _lineage(self, adj: str) -> Lineage:
        return Lineage(
            eval_timestamp=dt.datetime.now(dt.timezone.utc).isoformat(),
            adj_version=adj,
            source="SDK",
        )

    def _failed_report(
        self,
        expr_str: str,
        fd,
        universe: str,
        period: tuple[str, str],
        execution: str,
        neutralize: list[str] | None,
        adj: str,
        duration_ms: float,
    ) -> FactorReport:
        """Build a metrics-free report for an expression that failed diagnostics."""
        # Canonicalise when parseable so a syntax error still gets a stable id.
        try:
            canonical = str(parse(expr_str))
        except Exception:
            canonical = expr_str
        first_err = fd.errors[0] if fd.errors else None
        suggestion = None
        if first_err is not None:
            suggestion = first_err.suggestion or first_err.code.hint
        fm = fd.failure_mode
        nan = float("nan")
        return FactorReport(
            factor_id=FactorReport.compute_factor_id(canonical),
            expr=expr_str,
            expr_canonical=canonical,
            ic=nan,
            icir=nan,
            rank_ic=nan,
            rank_icir=nan,
            ic_by_horizon={},
            decay_halflife_days=None,
            turnover_1d=None,
            redundancy_score=0.0,
            most_similar_factor=None,
            lookahead_detected=(fm == "LOOKAHEAD"),
            failure_mode=fm,
            suggestion=suggestion,
            eval_period=tuple(period),
            universe_id=universe,
            n_dates=0,
            n_symbols=0,
            execution=execution,
            neutralize=neutralize,
            lineage=self._lineage(adj),
            diagnostics=fd.to_dict(),
            duration_ms=duration_ms,
        )

    def _no_data_report(
        self,
        expr_str: str,
        universe: str,
        period: tuple[str, str],
        execution: str,
        neutralize: list[str] | None,
        adj: str,
        duration_ms: float,
        detail: str,
    ) -> FactorReport:
        """Metrics-free report when the panel is empty/missing (no ingested data)."""
        try:
            canonical = str(parse(expr_str))
        except Exception:
            canonical = expr_str
        nan = float("nan")
        market = self._market_for(universe)
        return FactorReport(
            factor_id=FactorReport.compute_factor_id(canonical),
            expr=expr_str,
            expr_canonical=canonical,
            ic=nan, icir=nan, rank_ic=nan, rank_icir=nan,
            ic_by_horizon={},
            decay_halflife_days=None,
            turnover_1d=None,
            redundancy_score=0.0,
            most_similar_factor=None,
            lookahead_detected=False,
            failure_mode="NO_DATA",
            suggestion=(
                f"No ingested data for universe {universe!r} (market {market}) over "
                f"{period[0]}..{period[1]}. Ingest it first (e.g. prepare-nasdaq100 for US, "
                f"prepare_cn for A-share) or point ASSAY_DATA_DIR_{market} at its store. [{detail}]"
            ),
            eval_period=tuple(period),
            universe_id=universe,
            n_dates=0,
            n_symbols=0,
            execution=execution,
            neutralize=neutralize,
            lineage=self._lineage(adj),
            duration_ms=duration_ms,
        )

    def _assemble_report(
        self,
        *,
        expr_str: str,
        factor,
        metrics: dict[str, Any],
        decay: float | None,
        groups: dict[str, Any],
        turnover: float,
        redundancy_score: float,
        most_similar: str | None,
        universe: str,
        period: tuple[str, str],
        execution: str,
        neutralize: list[str] | None,
        adj: str,
        diagnostics,
        duration_ms: float,
    ) -> FactorReport:
        """Assemble a fully-scored :class:`FactorReport` from evaluator outputs."""
        canonical = str(parse(expr_str))
        T, N = factor.values.shape
        # Optional detail series (JSON-safe lists; evaluator returns numpy arrays).
        ic_series = metrics.get("ic_series")
        rank_series = metrics.get("rank_ic_series")
        ic_list = [float(x) for x in np.asarray(ic_series)] if ic_series is not None else None
        rank_list = (
            [float(x) for x in np.asarray(rank_series)] if rank_series is not None else None
        )
        dates = [str(d) for d in factor.dates]
        qret = groups.get("quantile_returns") or {}
        quintiles = [float(qret[k]) for k in sorted(qret, key=lambda s: int(s[1:]))] or None
        halflife = int(round(decay)) if decay is not None and np.isfinite(decay) else None

        return FactorReport(
            factor_id=FactorReport.compute_factor_id(canonical),
            expr=expr_str,
            expr_canonical=canonical,
            ic=float(metrics["ic"]),
            icir=float(metrics["icir"]),
            rank_ic=float(metrics["rank_ic"]),
            rank_icir=float(metrics["rank_icir"]),
            ic_by_horizon={int(h): float(v) for h, v in metrics["ic_by_horizon"].items()},
            decay_halflife_days=halflife,
            turnover_1d=float(turnover),
            redundancy_score=float(redundancy_score),
            most_similar_factor=most_similar,
            lookahead_detected=False,
            failure_mode=diagnostics.failure_mode,  # may flag a CONSTANT warning
            suggestion=(diagnostics.warnings[0].suggestion if diagnostics.warnings else None),
            eval_period=tuple(period),
            universe_id=universe,
            n_dates=T,
            n_symbols=N,
            execution=execution,
            neutralize=neutralize,
            lineage=self._lineage(adj),
            ic_series=ic_list,
            rank_ic_series=rank_list,
            dates=dates,
            quintile_returns=quintiles,
            duration_ms=duration_ms,
            diagnostics=diagnostics.to_dict(),
        )

    # --------------------------------------------------------------- stream ----
    async def stream(self, expr, **kw) -> AsyncGenerator[dict, None]:
        """Async generator of SSE-shaped events for one evaluation (architecture §4.2).

        Yields, in order, ``eval.started``, ``eval.ic_series``, ``eval.decay``,
        ``eval.groups`` and ``eval.complete``. The (CPU-bound) report computation is
        offloaded to a worker thread via :func:`asyncio.to_thread` so it never blocks
        the server's event loop — a long evaluation can run while other requests are
        still served. A failed factor still emits ``eval.started`` then
        ``eval.complete`` carrying the failure report.
        """
        import asyncio

        report = await asyncio.to_thread(self.evaluate, expr, **kw)
        yield {"event": "eval.started", "data": {"factor_id": report.factor_id, "expr": report.expr}}
        if report.failure_mode is None or report.ic_series is not None:
            yield {
                "event": "eval.ic_series",
                "data": {
                    "ic": report.ic_series or [],
                    "rank_ic": report.rank_ic_series or [],
                    "dates": report.dates or [],
                    "ic_mean": report.ic,
                },
            }
            yield {
                "event": "eval.decay",
                "data": {
                    "ic_by_horizon": {int(k): v for k, v in report.ic_by_horizon.items()},
                    "halflife": report.decay_halflife_days,
                },
            }
            yield {
                "event": "eval.groups",
                "data": {"quintile_returns": report.quintile_returns or []},
            }
        yield {"event": "eval.complete", "data": report.to_dict()}

    # ---------------------------------------------------------------- batch ----
    def batch(
        self,
        exprs: list,
        *,
        n_jobs: int | None = None,
        sort_by: str = "rank_icir",
        universe: str | None = None,
        period: tuple[str, str] | None = None,
        as_of: str | None = None,
        adj: str | None = None,
        session_id: str | None = None,
        save: bool = False,
        **kw,
    ) -> list[FactorReport]:
        """Evaluate many expressions in parallel; sorted desc by ``sort_by``.

        All expressions share one engine/session over the common universe+period, so
        the panel load + forward returns are paid once (engineering-docs §5/§6.1). A
        :class:`ThreadPoolExecutor` runs the per-factor numerics concurrently (the
        numba IC kernels release the GIL; the engine pivots are read-only/shared).

        Note: this path scores each factor independently (IC / decay / groups /
        turnover per factor). For raw factor *values* over a large corpus, use
        :meth:`evaluate_many` — it shares common sub-expressions across the batch
        (CSE) and can be pre-seeded from :attr:`precompute_store` (§4.3).
        """
        exprs = list(exprs)
        if not exprs:
            return []
        n_jobs = int(n_jobs) if n_jobs else self.config.n_workers

        # One shared session amortises panel + forward-returns across the batch.
        own_session = session_id is None
        if own_session:
            session_id = self.create_session(
                universe=universe, period=period, as_of=as_of, adj=adj, group_data=kw.get("group_data")
            )["session_id"]

        try:
            def _run(e):
                return self.evaluate(
                    e, session_id=session_id, save=save,
                    # universe/period/etc. come from the session; horizons/execution/
                    # neutralize still flow through so per-call overrides work.
                    horizons=kw.get("horizons"),
                    execution=kw.get("execution"),
                    neutralize=kw.get("neutralize"),
                )

            if n_jobs <= 1:
                reports = [_run(e) for e in exprs]
            else:
                with ThreadPoolExecutor(max_workers=n_jobs) as pool:
                    reports = list(pool.map(_run, exprs))
        finally:
            if own_session:
                self.sessions.expire(session_id)

        reports.sort(key=lambda r: _sort_key(r, sort_by), reverse=True)
        return reports

    # ----------------------------------------------------- precompute (CSE) ---
    @property
    def precompute_store(self):
        """Lazily-built :class:`PrecomputeStore` under ``<data_dir>/precompute``.

        Holds the materialised common sub-expression matrices, content-addressed by
        subtree hash + panel fingerprint, so it is shared across processes and
        auto-refreshes when new history changes the fingerprint (engineering §4.3).
        """
        if getattr(self, "_precompute", None) is None:
            from pathlib import Path

            from assay.engine import PrecomputeStore

            self._precompute = PrecomputeStore(Path(self.config.data_dir) / "precompute")
        return self._precompute

    @staticmethod
    def _read_corpus(corpus) -> list[str]:
        """Coerce a corpus (list of exprs, or a path to a newline-delimited file) to a list."""
        if isinstance(corpus, (str, bytes)):
            from pathlib import Path

            p = Path(corpus)
            if p.is_file():
                return [ln.strip() for ln in p.read_text().splitlines() if ln.strip()]
            return [str(corpus).strip()]
        return [str(e).strip() for e in corpus if str(e).strip()]

    def build_precompute(
        self,
        corpus,
        *,
        universe: str | None = None,
        period: tuple[str, str] | None = None,
        as_of: str | None = None,
        adj: str | None = None,
        top_k: int = 512,
        min_count: int = 3,
        min_nodes: int = 2,
    ) -> dict:
        """Mine the most common sub-expressions of ``corpus`` and precompute them for all assets.

        ``corpus`` is a list of expressions or a path to a newline-delimited factor
        file (e.g. the sweep's ``factors_canonical_unique.txt``). Builds one PIT engine
        over ``universe``/``period`` and stores the top-``top_k`` shared subtrees'
        ``(T, N)`` matrices in :attr:`precompute_store`. Returns the build summary
        (``built`` / ``est_evals_saved`` / ``top`` / ``fingerprint`` + the resolved
        ``universe``/``period``/``n_corpus``). JSON-safe.
        """
        universe, period, _h, _ex, as_of, adj = self._resolve(
            universe, period, None, None, as_of, adj
        )
        exprs = self._read_corpus(corpus)
        engine = self._build_engine(universe, period, as_of, adj, None)
        info = self.precompute_store.build(
            engine, exprs, top_k=top_k, min_count=min_count, min_nodes=min_nodes
        )
        info.update({"universe": universe, "period": list(period), "n_corpus": len(exprs)})
        return info

    def common_subexpressions(self, corpus, *, top_k: int = 100, min_count: int = 3, min_nodes: int = 2) -> list[dict]:
        """Rank a corpus's reusable sub-expressions (no data needed) -> ``[{expr, count, score, ...}]``."""
        from assay.engine import common_subexpressions as _cse

        cs = _cse(self._read_corpus(corpus), min_count=min_count, min_nodes=min_nodes, top_k=top_k)
        return [c.to_dict() for c in cs]

    def evaluate_many(
        self,
        exprs,
        *,
        universe: str | None = None,
        period: tuple[str, str] | None = None,
        as_of: str | None = None,
        adj: str | None = None,
        use_precompute: bool = True,
    ):
        """CSE-accelerated batch evaluation of factor **values** (SDK / sweep use).

        Evaluates every expression over one shared panel with cross-factor common
        sub-expression elimination, optionally pre-seeded from
        :attr:`precompute_store`. Returns the list of
        :class:`~assay.engine.FactorResult` (numpy ``(T, N)`` values) aligned to
        ``exprs`` — values only (no IC metrics), so a sweep can pull raw factor
        matrices fast. A bad expression raises (pre-filter with :meth:`evaluate`'s
        diagnostics if you need soft failures).
        """
        universe, period, _h, _ex, as_of, adj = self._resolve(
            universe, period, None, None, as_of, adj
        )
        engine = self._build_engine(universe, period, as_of, adj, None)
        bound = self.precompute_store.bind(engine.panel_fingerprint()) if use_precompute else None
        return engine.evaluate_many(list(exprs), precompute=bound)

    # -- hot-cache <-> daily-data coupling -----------------------------------
    # Universes to warm per market (primary first). The auto-refresh after a data
    # update walks these; missing-data universes are skipped.
    _PRECOMPUTE_UNIVERSES = {
        "US": ["NASDAQ100", "SP500"],
        "CN": ["CSI300", "CSI500", "CSI1000"],
        "HK": ["HSI"],
    }

    def _precompute_corpus(self) -> list[str]:
        """Default corpus to mine: every expression in the factor library (de-duped).

        A configured ``precompute_corpus`` path (newline-delimited file) wins when set.
        """
        path = getattr(self.config, "precompute_corpus", None)
        if path:
            from pathlib import Path

            p = Path(path)
            if p.is_file():
                return [ln.strip() for ln in p.read_text().splitlines() if ln.strip()]
        seen: set[str] = set()
        out: list[str] = []
        for s in self.library_query(limit=-1, min_rank_icir=float("-inf")):
            e = (s.expr or "").strip()
            if e and e not in seen:
                seen.add(e)
                out.append(e)
        return out

    def _market_data_latest(self, market: str) -> str | None:
        """Latest ingested ASSAY date for ``market`` (cheap; no panel load)."""
        try:
            from assay.data import orchestrate

            for m in orchestrate.status().get("markets", []):
                if m.get("market") == market:
                    return m.get("assay_latest")
        except Exception:  # noqa: BLE001 — status is best-effort
            return None
        return None

    def refresh_precompute_for_market(
        self,
        market: str,
        *,
        corpus: list[str] | None = None,
        universes: list[str] | None = None,
        top_k: int | None = None,
        progress=None,
    ) -> dict:
        """Rebuild the hot cache for ``market``, aligned to the latest ingested data.

        Called automatically after a successful daily data-update job (and on demand
        from the admin page). Mines the corpus's common sub-expressions (default: the
        factor library) and precomputes them for each of the market's universes over
        the **full current data range**, recording a manifest per universe so the
        cache's validity period and freshness are inspectable. ``progress(frac, msg)``
        is an optional callback (the data job forwards it). Never raises: a universe
        with no data is skipped with a noted reason.
        """
        market = market.upper()
        if not getattr(self.config, "precompute_enabled", True):
            return {"market": market, "refreshed": [], "note": "precompute disabled in system settings"}
        top_k = int(top_k if top_k is not None else getattr(self.config, "precompute_top_k", 256))
        min_count = int(getattr(self.config, "precompute_min_count", 2))
        corpus = corpus if corpus is not None else self._precompute_corpus()
        unis = universes or self._PRECOMPUTE_UNIVERSES.get(market, [])
        data_latest = self._market_data_latest(market)
        built_at = dt.datetime.now(dt.timezone.utc).isoformat()
        results: list[dict] = []
        if not corpus:
            return {"market": market, "refreshed": [], "note": "no factors in library to mine"}
        if not data_latest:
            return {"market": market, "refreshed": [], "note": f"{market} not initialized (no data)"}

        # Span the full ingested history so the fingerprint reflects the data validity.
        period = ("2000-01-01", data_latest)
        adj = self.config.default_adj
        for i, uni in enumerate(unis):
            if progress:
                progress(0.1 + 0.85 * i / max(len(unis), 1), f"warming hot cache: {uni} …")
            try:
                engine = self._build_engine(uni, period, data_latest, adj, None)
            except Exception as exc:  # noqa: BLE001 — universe absent for this data
                results.append({"universe": uni, "built": 0, "skipped_reason": str(exc)[:120]})
                continue
            fp = engine.panel_fingerprint()
            # min_count from system settings (default 2): a subtree shared by >= that
            # many library factors is worth caching (library corpus < a raw sweep).
            info = self.precompute_store.build(engine, corpus, top_k=top_k, min_count=min_count)
            meta = {
                "universe": uni, "market": market,
                "period": [str(engine.dates[0]), str(engine.dates[-1])] if len(engine.dates) else list(period),
                "as_of": data_latest, "adj": adj, "fingerprint": fp,
                "built_at": built_at, "data_latest": data_latest,
                "n_entries": info.get("built", 0), "n_corpus": len(corpus),
                "est_evals_saved": info.get("est_evals_saved", 0),
                "top": info.get("top", [])[:8],
            }
            self.precompute_store.record_manifest(uni, meta)
            self.precompute_store.record_entries(uni, info.get("entries", []))
            results.append({"universe": uni, "built": info.get("built", 0),
                            "period": meta["period"], "fingerprint": fp})
        if progress:
            progress(1.0, "hot cache refreshed")
        return {"market": market, "data_latest": data_latest, "refreshed": results}

    def precompute_status(self) -> dict:
        """Hot-cache status for the admin page — footprint + per-scope freshness.

        Returns ``{store:{entries,bytes,dir}, scopes:[{...manifest, fresh,
        current_data_latest}]}``. ``fresh`` is ``True`` when the manifest's
        ``data_latest`` still equals the market's current latest ingested date — i.e.
        the cache is aligned to the live data validity period; ``False`` means the
        data has advanced since the cache was built (a refresh is due).
        """
        store = self.precompute_store
        current = {mk: self._market_data_latest(mk) for mk in ("US", "CN", "HK")}
        scopes = []
        for m in store.manifests():
            cur = current.get(m.get("market"))
            scopes.append({
                **m,
                "current_data_latest": cur,
                "fresh": bool(cur and m.get("data_latest") == cur),
            })
        st = store.stats()
        return {
            "store": {"entries": st["entries"], "bytes": st["bytes"], "dir": str(store.cache_dir)},
            "current_data_latest": current,
            "scopes": scopes,
        }

    def cache_entries(self, scope: str) -> dict:
        """The detailed **contents** of one cache scope (e.g. ``'NASDAQ100'``).

        Returns every precomputed sub-expression with its expression, occurrence
        count, node count, recompute-saved score, the cached matrix shape ``(T, N)``,
        on-disk bytes and finite coverage — plus ``present`` (still on disk for the
        scope's current fingerprint). The list is what is *actually* materialised in
        the hot cache, sorted by score (most recompute saved first).
        """
        store = self.precompute_store
        man = store.manifest(scope) or {}
        fp = man.get("fingerprint")
        ents = store.entries(scope)
        for e in ents:
            e["present"] = bool(fp) and store.has(e.get("struct_hash", ""), fp)
        ents.sort(key=lambda e: e.get("score", 0), reverse=True)
        return {
            "scope": scope, "universe": man.get("universe", scope),
            "fingerprint": fp, "period": man.get("period"),
            "as_of": man.get("as_of"), "built_at": man.get("built_at"),
            "data_latest": man.get("data_latest"), "count": len(ents),
            "bytes": int(sum(int(e.get("bytes", 0)) for e in ents)),
            "entries": ents,
        }

    # --------------------------------------------------------- portfolio bt ---
    def backtest_portfolio(self, expr, config, *, as_of: str | None = None, **kw):
        """Run a portfolio backtest over the service store (design-doc Phase 5).

        Builds a :class:`~assay.portfolio.PortfolioBacktester` over the store that
        serves ``config.universe``'s market (so one service can backtest US and
        A-share factors side by side, exactly like :meth:`evaluate`) and runs the
        section-1.1 pipeline, returning a
        :class:`~assay.portfolio.PortfolioReport`. The optional market inputs
        (``groups``, ``tradable_mask``, ``prev_close``, ``adv``, ``benchmark``) pass
        straight through; ``None`` (the US default) leaves each constraint inert.

        ``as_of`` defaults inside the backtester to ``config.as_of_date`` then
        ``config.period_end`` (PIT cutoff -> survivorship-safe universe + adjustment
        versioning, design-doc §7.1/§7.2). The backtester never reads wall-clock
        time, so this method stamps ``lineage.eval_timestamp`` (and ``source``-free
        provenance) here — normal app code — keeping the simulation a pure function.
        """
        from assay.portfolio import PortfolioBacktester

        store = self.store_for_universe(getattr(config, "universe", None))
        report = PortfolioBacktester(store=store).run(
            expr, config, as_of=as_of, **kw
        )
        report.lineage.eval_timestamp = dt.datetime.now(dt.timezone.utc).isoformat()
        return report

    # ----------------------------------------------------------- market data ---
    # A-share tickers are 6 digits + a venue suffix (.SZ/.SH/.BJ) or bare 6 digits;
    # HK is digits + .HK; everything else (alpha tickers) is US. Used to route a
    # single user-typed symbol to the right per-market store.
    @staticmethod
    def _market_for_symbol(symbol: str) -> str:
        s = (symbol or "").strip().upper()
        if s.endswith(".HK"):
            return "HK"
        if s.endswith((".SZ", ".SH", ".BJ")) or (len(s) == 6 and s.isdigit()):
            return "CN"
        return "US"

    def store_for_symbol(self, symbol: str):
        """The :class:`DataStore` serving ``symbol`` (per its inferred market)."""
        return self._store_for(self._market_for_symbol(symbol))

    def get_bars(
        self,
        symbol: str,
        *,
        period: tuple[str, str] | None = None,
        freq: str = "1d",
        adj: str = "none",
        as_of: str | None = None,
    ) -> dict:
        """OHLCV bars for one ``symbol`` (TradingView-style chart source).

        ``freq`` is ``1d`` | ``1w`` | ``1M`` (daily resampled to weekly/monthly);
        intraday (``1m``/``5m``/``15m``) returns an empty ``bars`` list with
        ``available=False`` because only daily aggregates are ingested. ``adj`` is
        ``none`` | ``split`` | ``total`` (alias ``forward``). Returns a JSON-safe dict
        ``{symbol, market, freq, adj, available, bars:[{date,open,high,low,close,volume}]}``.
        """
        import polars as pl

        market = self._market_for_symbol(symbol)
        period = tuple(period) if period else tuple(self.config.default_period)  # type: ignore[assignment]
        as_of = as_of or period[1]
        freq = (freq or "1d").lower()
        # Codes are unambiguous (no "1m" — that collides minute vs month):
        #   1min/5min/15min (intraday) · 1d (day) · 1w (week) · 1mo (month).
        intraday = freq in {"1min", "5min", "15min"}
        base = {"symbol": symbol, "market": market, "freq": freq, "adj": adj}
        if intraday:
            # No intraday data ingested (daily aggregates only) — signal gracefully.
            return {**base, "available": False, "bars": []}

        store = self.store_for_symbol(symbol)
        panel = store.get_panel(
            fields=["open", "high", "low", "close", "volume"],
            symbols=[symbol],
            start_date=period[0],
            end_date=period[1],
            as_of_date=as_of,
            adj=(adj or "none"),
        )
        if panel.is_empty():
            return {**base, "available": True, "bars": []}

        df = panel.sort("date")
        if freq in {"1w", "1week", "week", "weekly"}:
            df = _resample_ohlcv(df, "1w")
        elif freq in {"1mo", "1month", "month", "monthly"} or freq == "1m_month":
            df = _resample_ohlcv(df, "1mo")
        # daily ('1d') passes through unchanged

        bars = [
            {
                "date": str(r["date"]),
                "open": _f(r["open"]), "high": _f(r["high"]),
                "low": _f(r["low"]), "close": _f(r["close"]),
                "volume": _f(r["volume"]),
            }
            for r in df.iter_rows(named=True)
        ]
        return {**base, "available": True, "bars": bars}

    def factor_series(
        self,
        symbol: str,
        expr: str,
        *,
        period: tuple[str, str] | None = None,
        adj: str = "none",
        as_of: str | None = None,
    ) -> dict:
        """Evaluate ``expr`` for a single ``symbol`` → its daily value series.

        Builds a one-symbol :class:`FactorEngine` over the PIT panel and evaluates the
        expression, so any time-series alpha can be overlaid on the price chart.
        (Cross-sectional operators degenerate over one name but never raise.) Returns
        ``{symbol, expr, dates:[iso], values:[float|None]}``.
        """
        market = self._market_for_symbol(symbol)
        period = tuple(period) if period else tuple(self.config.default_period)  # type: ignore[assignment]
        as_of = as_of or period[1]
        store = self.store_for_symbol(symbol)
        panel = store.get_panel(
            fields=["open", "high", "low", "close", "volume"],
            symbols=[symbol],
            start_date=period[0],
            end_date=period[1],
            as_of_date=as_of,
            adj=(adj or "none"),
        )
        if panel.is_empty():
            return {"symbol": symbol, "market": market, "expr": expr, "dates": [], "values": []}
        engine = FactorEngine(panel)
        result = engine.evaluate(expr)
        # values is (T, N); for a single symbol take column 0 (or the matching col).
        syms = list(result.symbols)
        col = syms.index(symbol) if symbol in syms else 0
        vals = [_f(result.values[i, col]) for i in range(result.values.shape[0])]
        dates = [str(d) for d in result.dates]
        return {"symbol": symbol, "market": market, "expr": expr, "dates": dates, "values": vals}

    # ----------------------------------------------------------- demo seed ----
    def seed_demo_library(
        self,
        *,
        universe: str | None = None,
        period: tuple[str, str] | None = None,
        as_of: str | None = None,
        sources: Iterable[str] = ("ALPHA101", "ALPHA158"),
    ) -> dict:
        """Populate the factor library with the Alpha101 / Alpha158 demo catalogs.

        Batch-evaluates each catalog on ``universe``/``period`` (defaults from config),
        then saves every successfully-evaluated factor tagged with its catalog as the
        provenance ``source`` (``ALPHA101`` / ``ALPHA158``) so the library's source
        filter groups them. Factors that cannot evaluate on the available data (e.g.
        Alpha101 expressions needing ``vwap``/``cap``, or Alpha158 ``VWAP0``) are
        skipped, not saved. Idempotent — re-running overwrites by factor id. Returns
        a per-source ``{evaluated, saved, skipped}`` summary.
        """
        from assay.factors import ALPHA_101, ALPHA_158

        catalogs = {"ALPHA101": list(ALPHA_101.values()), "ALPHA158": list(ALPHA_158.values())}
        universe = universe or self.config.default_universe
        period = tuple(period) if period else tuple(self.config.default_period)  # type: ignore[assignment]
        out: dict[str, dict] = {}
        for tag in sources:
            exprs = catalogs.get(tag.upper())
            if not exprs:
                continue
            reports = self.batch(exprs, universe=universe, period=period, as_of=as_of)
            saved = 0
            for r in reports:
                if r.failure_mode is not None:
                    continue
                if r.lineage is None:
                    r.lineage = Lineage()
                r.lineage.source = tag.upper()
                r.universe_id = universe  # ensure the library list shows the eval universe
                self.library.save(r)
                saved += 1
            out[tag.upper()] = {"evaluated": len(reports), "saved": saved, "skipped": len(reports) - saved}
        return out

    def add_factors(
        self,
        exprs: list,
        *,
        universe: str | None = None,
        source: str = "CUSTOM",
        period: tuple[str, str] | None = None,
        as_of: str | None = None,
    ) -> dict:
        """Evaluate a batch of user-supplied expressions and save the good ones.

        Powers the WebUI's bulk "add factors" import. Each expression is evaluated on
        ``universe``/``period`` (config defaults otherwise); successfully-evaluated
        factors are tagged with the provenance ``source`` and saved to the library.
        Failures are reported (not saved). Returns ``{evaluated, saved, results:[...]}``
        where each result carries ``{expr, factor_id, saved, failure_mode, rank_ic,
        rank_icir}`` — small enough for a per-chunk progress UI to drive a progress bar.
        """
        universe = universe or self.config.default_universe
        period = tuple(period) if period else tuple(self.config.default_period)  # type: ignore[assignment]
        reports = self.batch(exprs, universe=universe, period=period, as_of=as_of)
        results = []
        saved = 0
        for r in reports:  # batch() returns reports (sorted); each carries its own expr
            ok = r.failure_mode is None
            if ok:
                if r.lineage is None:
                    r.lineage = Lineage()
                r.lineage.source = source
                r.universe_id = universe
                self.library.save(r)
                saved += 1
            results.append({
                "expr": r.expr, "factor_id": r.factor_id, "saved": ok,
                "failure_mode": r.failure_mode,
                "rank_ic": _f(r.rank_ic), "rank_icir": _f(r.rank_icir),
            })
        return {"evaluated": len(reports), "saved": saved, "results": results}

    # --------------------------------------------------------------- library ---
    def library_query(self, **filters) -> list[FactorSummary]:
        """Filtered/sorted/paged library view (delegates to :meth:`FactorLibrary.list`)."""
        return self.library.list(**filters)

    def correlation_matrix(
        self,
        factor_ids: list[str],
        *,
        universe: str | None = None,
        period: tuple[str, str] | None = None,
        as_of: str | None = None,
        adj: str | None = None,
    ) -> dict:
        """Signed-Spearman similarity matrix over stored factors, re-evaluated live.

        Each requested factor's stored expression is re-evaluated on a single shared
        engine (so every matrix is on the same ``(date, symbol)`` grid), then handed
        to :func:`assay.library.correlation_matrix`. Unknown ids and ids whose
        expression fails to evaluate are dropped from the returned axes.
        """
        from assay.library import correlation_matrix as _corr

        if not factor_ids:  # nothing to evaluate -> empty axes, no panel load
            return _corr({})
        universe, period, horizons, execution, as_of, adj = self._resolve(
            universe, period, None, None, as_of, adj
        )
        engine = self._build_engine(universe, period, as_of, adj, None)
        values_by_id: dict[str, np.ndarray] = {}
        for fid in factor_ids:
            report = self.library.get(fid)
            if report is None:
                continue
            fd = engine.diagnose(report.expr)
            if fd.ok and fd.result is not None:
                values_by_id[fid] = fd.result.values
        return _corr(values_by_id)

    # ------------------------------------------------------- library views -----
    def ic_heatmap(
        self,
        factor_ids: list[str],
        *,
        universe: str | None = None,
        period: tuple[str, str] | None = None,
        as_of: str | None = None,
        adj: str | None = None,
        horizon: int | None = None,
        bucket: str = "month",
    ) -> dict:
        """Per-factor RankIC bucketed over calendar time (the **IC heatmap** view).

        Re-evaluates each stored factor on one shared PIT engine, computes the
        horizon-``h`` forward returns once, and reduces each factor's per-date RankIC
        into calendar buckets (``month`` / ``quarter`` / ``week``). Returns
        ``{factor_ids, exprs, periods, horizon, bucket, matrix, summary, ...}`` where
        ``matrix[i][k]`` is factor ``i``'s mean RankIC in period ``k`` (``None`` when
        empty) and ``summary[i]`` its overall mean RankIC. JSON-safe (NaN -> None).
        """
        from assay.library.analysis import bucket_periods

        empty = {"factor_ids": [], "exprs": [], "periods": [], "matrix": [],
                 "summary": [], "bucket": bucket, "horizon": None}
        if not factor_ids:
            return empty
        universe, period, horizons, execution, as_of, adj = self._resolve(
            universe, period, None, None, as_of, adj
        )
        h = int(horizon) if horizon else int(min(horizons))
        engine = self._build_engine(universe, period, as_of, adj, None)
        close = engine.field_matrix("close")
        open_ = engine.field_matrix("open") if "open" in engine._field_cols else None
        fwd = evaluator.forward_returns(close, open_, [h], execution)[h]
        labels, groups = bucket_periods(list(engine.dates), bucket)

        fids, exprs, matrix, summary = [], [], [], []
        for fid in factor_ids:
            report = self.library.get(fid)
            if report is None:
                continue
            fd = engine.diagnose(report.expr)
            if not (fd.ok and fd.result is not None):
                continue
            ric = evaluator.rank_ic_series(fd.result.values, fwd)  # (T,)
            row = []
            for idxs in groups:
                seg = ric[idxs]
                seg = seg[np.isfinite(seg)]
                row.append(float(seg.mean()) if seg.size else None)
            mean = evaluator.ic_summary(ric)[0]
            fids.append(fid)
            exprs.append(report.expr)
            matrix.append(row)
            summary.append(float(mean) if np.isfinite(mean) else None)
        return {
            "factor_ids": fids, "exprs": exprs, "periods": labels, "horizon": h,
            "bucket": bucket, "matrix": matrix, "summary": summary,
            "universe": universe, "period": list(period),
        }

    @staticmethod
    def _project_2d(dist: np.ndarray, method: str) -> tuple[np.ndarray, str]:
        """Project a precomputed distance matrix to 2-D; returns ``(coords, used_method)``.

        ``mds`` (classical, numpy) is the always-available default; ``tsne`` / ``umap``
        are used when requested *and* their library is importable, else they degrade
        to ``mds`` (so the endpoint never fails on a missing optional dependency).
        """
        from assay.library.analysis import classical_mds

        m = (method or "mds").lower()
        n = dist.shape[0]
        if m in ("tsne", "umap") and n >= 4:
            try:
                if m == "umap":
                    import umap  # type: ignore

                    emb = umap.UMAP(n_components=2, metric="precomputed", random_state=0).fit_transform(dist)
                    return np.asarray(emb, dtype=np.float64), "umap"
                from sklearn.manifold import TSNE

                emb = TSNE(
                    n_components=2, metric="precomputed", init="random", random_state=0,
                    perplexity=float(min(30, max(2, n // 3))),
                ).fit_transform(dist)
                return np.asarray(emb, dtype=np.float64), "tsne"
            except Exception:  # noqa: BLE001 — optional libs / degenerate input -> MDS
                pass
        return classical_mds(dist, 2), "mds"

    @staticmethod
    def _cluster_2d(coords: np.ndarray) -> np.ndarray:
        """K-means cluster labels for the 2-D layout (single cluster if too few / no sklearn)."""
        n = coords.shape[0]
        if n < 4:
            return np.zeros(n, dtype=np.int64)
        try:
            from sklearn.cluster import KMeans

            k = max(2, min(8, n // 4))
            return KMeans(n_clusters=k, n_init=10, random_state=0).fit_predict(coords).astype(np.int64)
        except Exception:  # noqa: BLE001 — no sklearn -> a single cluster
            return np.zeros(n, dtype=np.int64)

    def factor_embedding(
        self,
        factor_ids: list[str],
        *,
        universe: str | None = None,
        period: tuple[str, str] | None = None,
        as_of: str | None = None,
        adj: str | None = None,
        method: str = "mds",
    ) -> dict:
        """2-D similarity map of factors (the **Alpha space map** view).

        Builds the signed-Spearman similarity matrix (via :meth:`correlation_matrix`),
        turns it into a distance (``1 - corr``), projects to 2-D (``mds`` default;
        ``tsne`` / ``umap`` when installed), and K-means-clusters the layout. Each
        returned point carries its expression, coordinates, ``rank_ic`` / ``rank_icir``
        (size/colour hints), provenance ``source`` and ``cluster`` — so redundant
        alphas visibly group together. JSON-safe.
        """
        corr = self.correlation_matrix(
            factor_ids, universe=universe, period=period, as_of=as_of, adj=adj
        )
        fids = corr.get("factor_ids", []) or []
        n = len(fids)
        if n == 0:
            return {"points": [], "method": method, "factor_ids": []}
        mat = np.array(corr["matrix"], dtype=np.float64)
        dist = 1.0 - mat
        dist = np.where(np.isfinite(dist), dist, 1.0)
        np.fill_diagonal(dist, 0.0)
        coords, used = self._project_2d(dist, method)
        clusters = self._cluster_2d(coords)

        idx = {s.factor_id: s for s in self.library_query(limit=-1, min_rank_icir=float("-inf"))}
        points = []
        for i, fid in enumerate(fids):
            s = idx.get(fid)
            points.append({
                "id": fid,
                "expr": (s.expr if s else fid),
                "x": float(coords[i, 0]), "y": float(coords[i, 1]),
                "rank_ic": _f(s.rank_ic) if s else None,
                "rank_icir": _f(s.rank_icir) if s else None,
                "source": (s.source if s else None),
                "cluster": int(clusters[i]),
            })
        return {"points": points, "method": used, "factor_ids": fids}

    def factor_lineage(
        self, factor_ids: list[str] | None = None, *, limit: int = 200
    ) -> dict:
        """Derivation DAG of the library's factors (the **Lineage** view).

        Builds the expression-AST containment graph (see
        :func:`assay.library.analysis.lineage_graph`): an edge ``a -> b`` means
        factor ``a``'s whole expression is a sub-expression of ``b``. With
        ``factor_ids`` the graph is restricted to those (else the top-``limit``
        library factors). Nodes are enriched with ``rank_ic`` / ``rank_icir`` /
        ``source`` for sizing/colour. Credential-free (no data load — ASTs only).
        """
        from assay.library.analysis import lineage_graph

        if factor_ids:
            id_to_expr: dict[str, str] = {}
            for fid in factor_ids:
                report = self.library.get(fid)
                if report is not None:
                    id_to_expr[fid] = report.expr
        else:
            id_to_expr = {s.factor_id: s.expr for s in self.library_query(limit=limit, min_rank_icir=float("-inf"))}

        graph = lineage_graph(id_to_expr)
        idx = {s.factor_id: s for s in self.library_query(limit=-1, min_rank_icir=float("-inf"))}
        for nd in graph["nodes"]:
            s = idx.get(nd["id"])
            nd["rank_ic"] = _f(s.rank_ic) if s else None
            nd["rank_icir"] = _f(s.rank_icir) if s else None
            nd["source"] = s.source if s else None
        return graph

    # ----------------------------------------------------------- combination ---
    @staticmethod
    def combination_methods() -> list[dict]:
        """List the available factor-combination methods (design-doc §6.3).

        Credential-free: returns ``[{name, kind, available}]`` where analytic /
        optimization schemes are always available and learned models are flagged by
        whether their library (scikit-learn / lightgbm / xgboost) is installed.
        """
        return evaluator.available_methods()

    def _resolve_factor_specs(self, factors: Iterable) -> list[tuple[str, str]]:
        """Resolve combination inputs to ``[(unique_name, expression)]``.

        Each spec selects a factor from a pool or supplies one inline:

        * a bare expression string — ``"rank(close)"`` (name defaults to the expr);
        * ``"lib:<factor_id>"`` — a stored library factor (its expression);
        * ``"alpha101:<n>"`` / ``"alpha158:<n>"`` — a catalog factor by number;
        * a dict ``{"name": ..., "expr": ...}`` or ``{"name": ..., "id": ...}``.

        Unknown ids / catalog numbers are silently skipped (the caller reports the
        drop). Names are made unique with a ``#k`` suffix so duplicates never
        collide in the combination's factor map.
        """
        from assay.factors import ALPHA_101, ALPHA_158

        resolved: list[tuple[str, str]] = []
        for spec in factors:
            name: str | None = None
            expr: str | None = None
            if isinstance(spec, dict):
                expr = spec.get("expr")
                if expr is None and spec.get("id"):
                    rep = self.library.get(str(spec["id"]))
                    expr = rep.expr if rep is not None else None
                name = spec.get("name") or spec.get("id") or expr
            else:
                s = str(spec).strip()
                low = s.lower()
                if low.startswith("lib:"):
                    fid = s[4:]
                    rep = self.library.get(fid)
                    if rep is not None:
                        name, expr = fid, rep.expr
                elif low.startswith("alpha101:") or low.startswith("alpha158:"):
                    catalog = ALPHA_101 if low.startswith("alpha101:") else ALPHA_158
                    try:
                        n = int(s.split(":", 1)[1])
                    except ValueError:
                        n = None
                    if n is not None and n in catalog:
                        name, expr = f"{low.split(':', 1)[0]}_{n}", catalog[n]
                else:
                    name, expr = s, s  # raw expression
            if expr:
                resolved.append((str(name), str(expr)))

        # De-duplicate names deterministically (keep order; suffix collisions).
        seen: dict[str, int] = {}
        out: list[tuple[str, str]] = []
        for name, expr in resolved:
            if name in seen:
                seen[name] += 1
                name = f"{name}#{seen[name]}"
            else:
                seen[name] = 0
            out.append((name, expr))
        return out

    def combine_factors(
        self,
        factors: Iterable,
        *,
        train: tuple[str, str],
        val: tuple[str, str],
        test: tuple[str, str],
        universe: str | None = None,
        horizons: Iterable[int] | None = None,
        execution: str | None = None,
        as_of: str | None = None,
        adj: str | None = None,
        method: str = "icir_weight",
        standardize: str = "zscore",
        horizon: int | None = None,
        ridge_lambda: float = 10.0,
        embargo: int | None = None,
        candidate_methods: list[str] | None = None,
        model_params: dict | None = None,
    ) -> dict:
        """Combine factors and score the composite out-of-sample (design-doc §6.3).

        Selects factors from the pool (library ids, Alpha101/Alpha158 catalog
        numbers) and/or accepts inline expressions, evaluates them all on **one**
        PIT engine spanning ``train``..``test``, computes forward returns, and runs
        :func:`assay.evaluator.combine_factors`: weights are fit on the **train**
        window, the scheme is validation-selected when ``method='auto'``, and the
        composite's IC/RankIC/ICIR is reported on **train / val / test** (the test
        window never touches the fit). A label embargo (default = max horizon) purges
        the train/val tails so overlapping forward labels do not leak across splits.

        Parameters mirror :meth:`evaluate` (``universe``/``horizons``/``execution``/
        ``as_of``/``adj`` resolve from config) plus the combination knobs
        (``method``, ``standardize``, ``horizon``, ``ridge_lambda``, ``embargo``,
        ``candidate_methods``). The three split windows are required ``(start, end)``
        ``YYYY-MM-DD`` pairs.

        Returns a JSON-safe dict: :meth:`CombinationResult.to_dict` plus ``universe``,
        ``period``, ``execution``, the resolved factor list, and any ``dropped`` specs
        that failed to evaluate. Degrades to ``{"failure": "NO_FACTORS", ...}`` (never
        raises) when fewer than one input evaluates on the available data.
        """
        # Engine spans the full train..test envelope; PIT cutoff at the test end.
        period = (min(train[0], val[0], test[0]), max(train[1], val[1], test[1]))
        universe, period, horizons, execution, as_of, adj = self._resolve(
            universe, period, horizons, execution, as_of or test[1], adj
        )

        resolved = self._resolve_factor_specs(factors)
        if not resolved:
            return {"failure": "NO_FACTORS", "detail": "no spec resolved to an expression",
                    "universe": universe, "period": list(period)}

        try:
            engine = self._build_engine(universe, period, as_of, adj, None)
        except (ValueError, FileNotFoundError) as exc:
            return {"failure": "NO_DATA", "detail": str(exc),
                    "universe": universe, "period": list(period)}

        # Evaluate every resolved factor on the shared grid; drop the failures.
        matrices: dict[str, np.ndarray] = {}
        dropped: list[dict] = []
        for name, expr in resolved:
            fd = engine.diagnose(expr)
            if fd.ok and fd.result is not None:
                matrices[name] = fd.result.values
            else:
                dropped.append({"name": name, "expr": expr, "failure_mode": fd.failure_mode})
        if not matrices:
            return {"failure": "NO_FACTORS", "detail": "no input factor evaluated cleanly",
                    "universe": universe, "period": list(period), "dropped": dropped}

        fwd_by_h = self._cold_forward_returns(engine, horizons, execution)
        emb = max(horizons) if embargo is None else int(embargo)
        train_mask, val_mask, test_mask = evaluator.make_splits(
            engine.dates, train, val, test, embargo=emb
        )
        result = evaluator.combine_factors(
            matrices, fwd_by_h, train_mask, val_mask, test_mask,
            method=method, standardize=standardize, horizon=horizon,
            ridge_lambda=ridge_lambda, candidate_methods=candidate_methods,
            model_params=model_params,
        )
        out = result.to_dict()
        out.update({
            "universe": universe,
            "period": list(period),
            "execution": execution,
            "adj": adj,
            "splits": {"train": list(train), "val": list(val), "test": list(test),
                       "embargo": emb},
            "resolved_factors": [{"name": n, "expr": e} for n, e in resolved],
            "dropped": dropped,
        })
        return out

    # --------------------------------------------------------------- session ---
    def create_session(
        self,
        *,
        universe: str | None = None,
        period: tuple[str, str] | None = None,
        as_of: str | None = None,
        adj: str | None = None,
        group_data: dict[str, dict[str, object]] | None = None,
    ) -> dict:
        """Create a session that pre-loads the panel and returns its descriptor.

        Builds the engine eagerly (paying the panel-load cost once) and registers a
        :class:`SessionCache` over the same panel, so subsequent ``evaluate`` /
        ``batch`` calls with this ``session_id`` skip the panel load and reuse the
        session-shared forward returns (architecture §4.2 ``/session/create``).
        """
        t0 = time.perf_counter()
        universe, period, _h, _exe, as_of, adj = self._resolve(
            universe, period, None, None, as_of, adj
        )
        engine = self._build_engine(universe, period, as_of, adj, group_data)
        # Register a SessionCache over the engine's own panel so axes match exactly.
        session_id = self.sessions.create_session(engine._panel)
        sess = self.sessions.get(session_id)
        assert sess is not None  # just created
        # Stash the built engine and the resolution metadata on the session memo so
        # _session_engine reuses them without rebuilding or re-reading config.
        sess.get_or_compute("engine", lambda: engine)
        sess.get_or_compute(
            "_meta",
            lambda: {
                "universe": universe,
                "period": period,
                "as_of": as_of,
                "adj": adj,
                "group_data": group_data,
            },
        )
        setup_ms = (time.perf_counter() - t0) * 1000.0
        return {
            "session_id": session_id,
            "universe": universe,
            "period": list(period),
            "as_of": as_of,
            "adj": adj,
            "n_dates": int(sess.shape[0]),
            "n_symbols": int(sess.shape[1]),
            "setup_ms": setup_ms,
        }

    def expire_session(self, session_id: str) -> bool:
        """Drop a session and release its matrices. ``True`` if it existed."""
        return self.sessions.expire(session_id)
