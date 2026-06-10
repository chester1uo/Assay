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
    def __init__(self, config: AssayConfig) -> None:
        self.config = config
        # DataStore is the only credential-hungry dependency: build it lazily.
        self._store = None
        self.library = FactorLibrary(config.library_path)
        self.cache = L2FactorCache(config.cache_path)
        self.sessions = SessionRegistry()

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
        """Lazily-built :class:`DataStore` (needs MASSIVE config / ingested data)."""
        if self._store is None:
            # Imported here, not at module load, so `import assay` stays offline-safe.
            from assay.data.store import DataStore

            self._store = DataStore(self.config)
        return self._store

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
        """Cold-path engine over a fresh PIT panel (architecture §2.1)."""
        return FactorEngine.from_store(
            self.store,
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
        if session_id is not None:
            engine, fwd_by_h = self._session_engine(session_id, horizons, execution)
        else:
            engine = self._build_engine(universe, period, as_of, adj, group_data)
            fwd_by_h = None  # computed after a successful diagnose (below)

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
        ``eval.groups`` and ``eval.complete``. The report is computed synchronously
        (the numerics are fast and CPU-bound) and the pieces are yielded as events;
        a failed factor still emits ``eval.started`` then ``eval.complete`` carrying
        the failure report.
        """
        report = self.evaluate(expr, **kw)
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

        Note: DAG/CSE de-duplication of shared sub-expressions across the batch is
        future work — each factor is still evaluated independently here.
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
