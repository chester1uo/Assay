"""Session-level cache: amortise per-session setup across factor evaluations.

Engineering-docs section 5 (and 6.1) describe the *session-level panel cache*:
a session pre-loads the aligned ``(T, N)`` field matrices and the precomputed
forward-return matrices **once**, then reuses them for every factor evaluated in
that session. The first factor pays the panel-load / pivot cost; subsequent
factors in the same session skip it entirely (engineering-docs §5 budget table:
``get_panel`` 222ms -> 0ms on the 2nd+ factor).

This module implements that cache plus a small registry that issues opaque
session ids and owns their lifecycle.

Design notes / house rules:

* The numeric core is the same as the engine's: aligned ``(T, N)`` float64
  matrices with ``axis0`` = dates (time) and ``axis1`` = symbols (cross-section).
  A missing symbol on a date is ``NaN`` and never poisons that date's
  cross-section.
* :class:`SessionCache` is a *store*, not a compute layer. It holds the field
  matrices, the forward-return matrices, and a generic ``get_or_compute`` memo so
  the service can park derived per-session artefacts (e.g. a built
  ``FactorEngine``) on the session without this module importing them.
* :class:`SessionRegistry` is thread-safe (a single ``threading.Lock``) and uses
  a monotonic counter for ids — *not* a wall clock, so importing this module
  never reads the clock and session ids are deterministic within a process.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Callable

import numpy as np
import polars as pl


def _panel_to_matrices(
    panel: pl.DataFrame,
    dates: np.ndarray,
    symbols: np.ndarray,
) -> dict[str, np.ndarray]:
    """Pivot every non-axis field of a long panel into aligned ``(T, N)`` matrices.

    Mirrors :meth:`assay.engine.FactorEngine._matrix`: rows are ``dates`` (sorted
    ascending), columns are ``symbols`` (sorted); cells absent from the long frame
    stay ``NaN``. Returns one float64 matrix per field column.
    """
    d_all = panel["date"].to_numpy()
    s_all = panel["symbol"].to_numpy()
    di = np.searchsorted(dates, d_all)
    sj = np.searchsorted(symbols, s_all)
    shape = (dates.shape[0], symbols.shape[0])
    out: dict[str, np.ndarray] = {}
    for col in panel.columns:
        if col in ("date", "symbol"):
            continue
        arr = np.full(shape, np.nan, dtype=np.float64)
        arr[di, sj] = panel[col].to_numpy().astype(np.float64)
        out[col] = arr
    return out


@dataclass
class SessionCache:
    """Per-session store of aligned field + forward-return matrices and a memo.

    Constructed once per session from a long point-in-time ``panel`` (columns
    ``date``, ``symbol``, ``*fields``) plus the session axes. The panel is pivoted
    eagerly into ``(T, N)`` field matrices so the cost is paid once; forward
    returns are attached later (they depend on horizons/execution, owned by the
    backtest layer) via :meth:`put_forward_returns`.

    ``get_or_compute`` is a generic, thread-safe-per-session memo: pass a key and
    a zero-arg factory; the factory runs at most once per key and its result is
    cached for the session's lifetime. This lets the service stash derived
    artefacts (a built engine, a neutralisation basis, ...) on the session without
    this module taking a dependency on them.
    """

    session_id: str
    _matrices: dict[str, np.ndarray]
    _dates: np.ndarray
    _symbols: np.ndarray
    _fwd_returns: dict[int, np.ndarray] = field(default_factory=dict)
    _memo: dict[str, object] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def __init__(
        self,
        session_id: str,
        panel: pl.DataFrame,
        dates: np.ndarray | list | None = None,
        symbols: np.ndarray | list | None = None,
    ):
        if panel is None or panel.is_empty():
            raise ValueError("cannot build a SessionCache on an empty panel")
        for required in ("date", "symbol"):
            if required not in panel.columns:
                raise ValueError(f"panel is missing the required {required!r} column")

        # Axes default to the sorted unique values in the panel (the engine's
        # convention); explicit axes are honoured when the caller has already
        # computed them (e.g. to share an exact axis with a FactorEngine).
        d = panel["date"].to_numpy() if dates is None else np.asarray(dates)
        s = panel["symbol"].to_numpy() if symbols is None else np.asarray(symbols)
        self._dates = np.unique(d) if dates is None else d
        self._symbols = np.unique(s) if symbols is None else s

        self.session_id = session_id
        self._matrices = _panel_to_matrices(panel, self._dates, self._symbols)
        self._fwd_returns = {}
        self._memo = {}
        self._lock = threading.Lock()

    # -- axes / panel accessors ----------------------------------------------
    @property
    def panel(self) -> dict[str, np.ndarray]:
        """The pivoted field matrices: ``{field -> (T, N) float64}`` (NaN-padded)."""
        return self._matrices

    @property
    def dates(self) -> np.ndarray:
        """Time axis: sorted unique dates (``axis0`` of every matrix)."""
        return self._dates

    @property
    def symbols(self) -> np.ndarray:
        """Cross-section axis: sorted unique symbols (``axis1`` of every matrix)."""
        return self._symbols

    @property
    def shape(self) -> tuple[int, int]:
        """``(T, N)`` — number of dates by number of symbols."""
        return (self._dates.shape[0], self._symbols.shape[0])

    @property
    def fields(self) -> list[str]:
        """Names of the cached field matrices."""
        return list(self._matrices)

    def field_matrix(self, name: str) -> np.ndarray:
        """Return the aligned ``(T, N)`` matrix for field ``name`` (NaN-padded)."""
        try:
            return self._matrices[name]
        except KeyError:
            raise ValueError(
                f"field {name!r} is not in the session panel (have: {sorted(self._matrices)})"
            ) from None

    # -- forward returns (session-shared, set by the backtest layer) ----------
    def put_forward_returns(self, horizon: int, matrix: np.ndarray) -> None:
        """Attach a precomputed forward-return ``(T, N)`` matrix for ``horizon``.

        Forward returns are computed once per session and reused across every
        factor (engineering-docs §6.1). The matrix must match the session shape so
        it stays index-aligned with factor outputs.
        """
        arr = np.asarray(matrix, dtype=np.float64)
        if arr.shape != self.shape:
            raise ValueError(
                f"forward-return matrix for horizon {horizon} has shape {arr.shape}, "
                f"expected session shape {self.shape}"
            )
        self._fwd_returns[int(horizon)] = arr

    def forward_returns(self, horizon: int) -> np.ndarray | None:
        """Return the cached forward-return matrix for ``horizon`` (``None`` if absent)."""
        return self._fwd_returns.get(int(horizon))

    @property
    def horizons(self) -> list[int]:
        """Horizons for which forward returns have been precomputed (ascending)."""
        return sorted(self._fwd_returns)

    # -- generic per-session memo --------------------------------------------
    def get_or_compute(self, key: str, fn: Callable[[], object]) -> object:
        """Return ``memo[key]``, computing it via ``fn()`` once on first miss.

        Thread-safe within the session: ``fn`` runs at most once per key even
        under concurrent access. Use for derived per-session artefacts whose cost
        should be amortised across factors (a built engine, a cached basis, ...).
        """
        # Fast path: already memoised (dict reads are atomic under the GIL).
        hit = self._memo.get(key, _MISS)
        if hit is not _MISS:
            return hit
        with self._lock:
            hit = self._memo.get(key, _MISS)
            if hit is _MISS:
                hit = fn()
                self._memo[key] = hit
            return hit

    def __contains__(self, key: str) -> bool:
        return key in self._memo


# Sentinel for "absent" so a memoised ``None`` is distinguishable from a miss.
_MISS: object = object()


class SessionRegistry:
    """Thread-safe registry of live :class:`SessionCache` objects.

    Issues opaque, monotonically increasing session ids (``sess_1``, ``sess_2``,
    ...) from an in-process counter — never a wall clock — so id allocation is
    deterministic and import-time has no clock dependency. All mutation is guarded
    by a single lock; reads of an individual session are lock-free thereafter.
    """

    _ID_PREFIX = "sess_"

    def __init__(self) -> None:
        self._sessions: dict[str, SessionCache] = {}
        self._counter: int = 0
        self._lock = threading.Lock()

    def create_session(
        self,
        panel: pl.DataFrame,
        dates: np.ndarray | list | None = None,
        symbols: np.ndarray | list | None = None,
    ) -> str:
        """Build a :class:`SessionCache` from ``panel`` and return its session id."""
        with self._lock:
            self._counter += 1
            session_id = f"{self._ID_PREFIX}{self._counter}"
            self._sessions[session_id] = SessionCache(session_id, panel, dates, symbols)
            return session_id

    def get(self, session_id: str) -> SessionCache | None:
        """Return the live session for ``session_id`` (``None`` if unknown/expired)."""
        return self._sessions.get(session_id)

    def expire(self, session_id: str) -> bool:
        """Drop a session, releasing its matrices. ``True`` if it existed."""
        with self._lock:
            return self._sessions.pop(session_id, None) is not None

    def active_sessions(self) -> int:
        """Number of live sessions (engineering REST ``/health`` field)."""
        return len(self._sessions)

    def __len__(self) -> int:
        return len(self._sessions)

    def __contains__(self, session_id: str) -> bool:
        return session_id in self._sessions
