"""Assay — a high-performance factor backtesting engine for agent-driven alpha mining.

This package ships the data layer (MASSIVE US-equity loaders, point-in-time stores),
the factor execution engine + diagnostics, the IC/decay/turnover evaluator, the
factor library, and — exposed here — the ergonomic **Python SDK** (architecture §3,
engineering-docs §7.1): a thin, in-process wrapper over the singleton
:class:`~assay.service.AssayService`.

Quick start::

    import assay

    assay.init()                                   # reads config from env / .env
    report = assay.backtest("ts_returns(close, 20)", universe="NASDAQ100")
    print(report.rank_ic, report.rank_icir)

    reports = assay.batch_backtest(factors, n_jobs=8, sort_by="rank_icir")

    with assay.Session(universe="NASDAQ100") as sess:  # amortise panel load
        r1 = sess.backtest("ts_returns(close, 20)")
        r2 = sess.backtest("ts_corr(close, volume, 20)")

    top = assay.library.list(min_rank_icir=0.5)        # library proxy

Importing the package never touches MASSIVE credentials: :class:`DataStore` is built
lazily by the service only when a backtest actually needs data, so the library proxy
and offline paths work credential-free (architecture §2).
"""

from __future__ import annotations

from typing import AsyncGenerator

from assay.config import AssayConfig, MassiveConfig
from assay.service import AssayService

__version__ = "0.1.0"


# ---------------------------------------------------------------------------
# initialization
# ---------------------------------------------------------------------------
def init(config: AssayConfig | str | None = None) -> AssayService:
    """Initialise (or replace) the process-wide :class:`AssayService` and return it.

    * ``None``  — build an :class:`AssayConfig` from the environment / project
      ``.env`` (:meth:`AssayConfig.from_env`).
    * :class:`AssayConfig` — used as-is.
    * ``str`` / path — tolerated for ergonomics: treated as the ``data_dir`` of an
      offline config (no MASSIVE creds), so ``assay.init("/some/dir")`` never raises.
    """
    if config is None:
        cfg = AssayConfig.from_env()
    elif isinstance(config, AssayConfig):
        cfg = config
    else:  # a path-like: locate library/cache under it without requiring creds.
        cfg = AssayConfig.for_tests(data_dir=str(config))
    return AssayService.init(cfg)


def _service() -> AssayService:
    """Return the live service, auto-initialising from env on first use."""
    try:
        return AssayService.get()
    except RuntimeError:
        return init()


# ---------------------------------------------------------------------------
# single / batch evaluation
# ---------------------------------------------------------------------------
def backtest(expr, **kw):
    """Evaluate one factor expression -> :class:`~assay.library.FactorReport`.

    Thin wrapper over :meth:`AssayService.evaluate`; auto-initialises the service
    from the environment on first call. See architecture §3.2 for the full keyword
    set (``universe``, ``period``, ``horizons``, ``execution``, ``neutralize``,
    ``as_of``, ``adj``, ``save`` ...).
    """
    return _service().evaluate(expr, **kw)


def batch_backtest(exprs, **kw):
    """Evaluate many expressions in parallel, sorted by quality (architecture §3.3).

    Wrapper over :meth:`AssayService.batch`; auto-initialises from the environment.
    Reuses one shared session over the common universe+period so the panel load and
    forward returns are paid once.
    """
    return _service().batch(exprs, **kw)


async def stream(expr, **kw) -> AsyncGenerator[dict, None]:
    """Async generator of evaluation events for ``expr`` (architecture §3.7 / §4.2).

    Yields ``eval.started`` / ``eval.ic_series`` / ``eval.decay`` / ``eval.groups`` /
    ``eval.complete`` dicts. Auto-initialises the service from the environment.
    """
    async for event in _service().stream(expr, **kw):
        yield event


# ---------------------------------------------------------------------------
# library proxy
# ---------------------------------------------------------------------------
class _LibraryProxy:
    """Module-level proxy delegating to the service's :class:`FactorLibrary`.

    ``assay.library.list(...)`` / ``.get(id)`` / ``.save(report)`` / ``.delete(ids)``
    /``.correlation_matrix(...)`` / ``.prune(...)`` (architecture §3.6). Resolves the
    live service lazily on every call so it works before and after :func:`init`.
    """

    def list(self, **filters):
        """Filtered/sorted/paged :class:`FactorSummary` rows (see ``FactorLibrary.list``)."""
        return _service().library_query(**filters)

    def get(self, factor_id: str):
        """Load the full :class:`FactorReport` for ``factor_id`` (``None`` if absent)."""
        return _service().library.get(factor_id)

    def save(self, report) -> str:
        """Persist ``report`` and return its ``factor_id``."""
        return _service().library.save(report)

    def delete(self, factor_ids) -> int:
        """Delete one id or a list of ids; returns the count removed."""
        return _service().library.delete(factor_ids)

    def correlation_matrix(self, factor_ids, **kw) -> dict:
        """Signed-Spearman similarity matrix over stored factors (re-evaluated live)."""
        return _service().correlation_matrix(list(factor_ids), **kw)

    def prune(self, *, redundancy_threshold: float = 0.7, dry_run: bool = True, **kw) -> dict:
        """Greedy redundancy pruning of the library (architecture §3.6 / §4.3).

        Builds the live similarity matrix over every stored factor, then keeps the
        higher-``rank_icir`` member of each over-threshold pair. ``dry_run`` only
        reports ``would_delete``; otherwise the losers are deleted and the count is
        returned alongside the plan.
        """
        from assay.library import prune as _prune

        svc = _service()
        summaries = svc.library_query(limit=-1)
        ids = [s.factor_id for s in summaries]
        scores = {s.factor_id: (s.rank_icir if s.rank_icir is not None else float("-inf"))
                  for s in summaries}
        if not ids:  # empty library -> nothing to prune (no panel load)
            return {"would_delete": [], "kept": [], "pairs_over_threshold": 0, "count": 0}
        corr = svc.correlation_matrix(ids, **kw)
        plan = _prune(corr["matrix"], corr["factor_ids"], scores, threshold=redundancy_threshold)
        if not dry_run and plan["would_delete"]:
            plan["deleted"] = svc.library.delete(list(plan["would_delete"]))
        plan["count"] = len(plan["would_delete"])
        return plan


library = _LibraryProxy()


# ---------------------------------------------------------------------------
# session context manager
# ---------------------------------------------------------------------------
class Session:
    """Context manager that amortises panel/forward-return setup (architecture §3.4).

    Entering the context creates a service session (loads the panel once); the
    ``.backtest`` / ``.batch_backtest`` methods then evaluate factors against the
    cached panel + forward returns (~30-50ms each vs a fresh panel load). Exiting
    releases the session's matrices.

        with assay.Session(universe="NASDAQ100", period=("2020-01-01", "2024-12-31")) as s:
            r1 = s.backtest("ts_returns(close, 20)")
            reports = s.batch_backtest(factors, n_jobs=8)
    """

    def __init__(
        self,
        *,
        universe: str | None = None,
        period: tuple[str, str] | None = None,
        as_of: str | None = None,
        adj: str | None = None,
        group_data: dict | None = None,
    ) -> None:
        self._kw = dict(
            universe=universe, period=period, as_of=as_of, adj=adj, group_data=group_data
        )
        self.session_id: str | None = None
        self.info: dict | None = None

    def __enter__(self) -> "Session":
        self.info = _service().create_session(**self._kw)
        self.session_id = self.info["session_id"]
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if self.session_id is not None:
            _service().expire_session(self.session_id)
            self.session_id = None
        return False  # never suppress exceptions

    def backtest(self, expr, **kw):
        """Evaluate one factor against the session's cached panel."""
        return _service().evaluate(expr, session_id=self.session_id, **kw)

    def batch_backtest(self, exprs, **kw):
        """Evaluate many factors against the session's cached panel, sorted by quality."""
        return _service().batch(exprs, session_id=self.session_id, **kw)


__all__ = [
    "AssayConfig",
    "MassiveConfig",
    "AssayService",
    "__version__",
    "init",
    "backtest",
    "batch_backtest",
    "stream",
    "library",
    "Session",
]
