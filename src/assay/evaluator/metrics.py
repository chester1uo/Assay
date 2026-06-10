"""IC / RankIC kernels over aligned ``(T, N)`` matrices (engineering-docs §6.2).

Pure numpy/numba: no engine or data-layer imports. The factor and forward-return
panels are ``(T, N)`` float matrices — ``T`` dates on axis 0, ``N`` symbols on
axis 1. For every date we compute a single cross-sectional correlation:

* :func:`ic_series`      — Pearson IC per date.
* :func:`rank_ic_series` — Spearman RankIC per date (Pearson of average ranks).

Both are strictly NaN-aware: each date uses only the symbols that are **finite in
both** the factor and the return row, and yields ``NaN`` when fewer than two such
symbols remain (or either side is constant across them). :func:`ic_summary`
reduces a per-date series to ``(mean, mean/std)`` ignoring NaN with ``ddof=1``.

:func:`evaluate_ic` is the headline entry point used by the report assembler: it
ranks the factor **once** and reuses those ranks across every horizon (§6.2,
"Multi-horizon fusion"), so ``factor == fwd`` yields ``IC ≈ RankIC ≈ 1``.

The heavy per-date loops are JIT-compiled with ``numba.njit(parallel=True)`` when
numba is importable and compiles; otherwise the module transparently falls back
to vectorised numpy with identical numerics.
"""

from __future__ import annotations

import numpy as np

# --- optional numba acceleration -------------------------------------------
# We define plain-Python reference kernels, then try to njit them. Any import
# or compile failure falls back to the pure-numpy implementations below so the
# module is always importable and correct (just slower).
try:  # pragma: no cover - exercised by whichever branch the host supports
    import numba

    _HAS_NUMBA = True
except Exception:  # pragma: no cover
    numba = None  # type: ignore[assignment]
    _HAS_NUMBA = False


# ---------------------------------------------------------------------------
# scalar helpers (njit-able; operate on a single date's two rows)
# ---------------------------------------------------------------------------


def _pearson_pair(x: np.ndarray, y: np.ndarray) -> float:
    """Pearson correlation of ``x`` vs ``y`` over their shared finite entries.

    Returns ``NaN`` when fewer than two shared-finite points remain or when
    either side has zero variance on those points.
    """
    n = x.shape[0]
    # First pass: count & means over jointly-finite entries.
    cnt = 0
    sx = 0.0
    sy = 0.0
    for i in range(n):
        xi = x[i]
        yi = y[i]
        if np.isfinite(xi) and np.isfinite(yi):
            cnt += 1
            sx += xi
            sy += yi
    if cnt < 2:
        return np.nan
    mx = sx / cnt
    my = sy / cnt
    cov = 0.0
    vx = 0.0
    vy = 0.0
    for i in range(n):
        xi = x[i]
        yi = y[i]
        if np.isfinite(xi) and np.isfinite(yi):
            dx = xi - mx
            dy = yi - my
            cov += dx * dy
            vx += dx * dx
            vy += dy * dy
    if vx <= 0.0 or vy <= 0.0:
        return np.nan
    return cov / np.sqrt(vx * vy)


def _avg_ranks_masked(x: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """Average (tie-broken) ranks of ``x`` over entries where ``mask`` is True.

    Masked-out entries receive ``NaN``. Ranks are 0-based dense positions with
    ties averaged, matching ``scipy.stats.rankdata(method='average') - 1``.
    """
    n = x.shape[0]
    out = np.full(n, np.nan)
    m = 0
    for i in range(n):
        if mask[i]:
            m += 1
    if m == 0:
        return out
    vals = np.empty(m)
    idx = np.empty(m, dtype=np.int64)
    k = 0
    for i in range(n):
        if mask[i]:
            vals[k] = x[i]
            idx[k] = i
            k += 1
    order = np.argsort(vals, kind="mergesort")
    i = 0
    while i < m:
        j = i + 1
        while j < m and vals[order[j]] == vals[order[i]]:
            j += 1
        avg = (i + j - 1) / 2.0
        for p in range(i, j):
            out[idx[order[p]]] = avg
        i = j
    return out


def _ic_series_impl(factor: np.ndarray, fwd: np.ndarray) -> np.ndarray:
    t_n = factor.shape[0]
    out = np.full(t_n, np.nan)
    for t in range(t_n):  # parallelisable over dates
        out[t] = _pearson_pair(factor[t], fwd[t])
    return out


def _rank_ic_series_impl(
    factor_rank: np.ndarray, fwd: np.ndarray, factor: np.ndarray
) -> np.ndarray:
    """RankIC per date using *precomputed* factor average-ranks.

    ``factor_rank`` holds, per date, the average ranks of ``factor`` over that
    date's finite-factor symbols (NaN elsewhere) — computed once and reused
    across horizons. The forward row is ranked on the fly, but only over the
    symbols finite in **both** sides, so the masks always agree.
    """
    t_n, n_sym = factor.shape
    out = np.full(t_n, np.nan)
    for t in range(t_n):  # parallelisable over dates
        # Joint-finite mask for this date.
        mask = np.empty(n_sym, dtype=np.bool_)
        cnt = 0
        for j in range(n_sym):
            ok = np.isfinite(factor[t, j]) and np.isfinite(fwd[t, j])
            mask[j] = ok
            if ok:
                cnt += 1
        if cnt < 2:
            continue
        fr = _avg_ranks_masked(factor[t], mask)
        rr = _avg_ranks_masked(fwd[t], mask)
        out[t] = _pearson_pair(fr, rr)
    return out


# --- compile (or not) ------------------------------------------------------
if _HAS_NUMBA:  # pragma: no cover - host-dependent
    try:
        _pearson_pair = numba.njit(cache=True, fastmath=False)(_pearson_pair)
        _avg_ranks_masked = numba.njit(cache=True, fastmath=False)(_avg_ranks_masked)

        @numba.njit(parallel=True, cache=True, fastmath=False)
        def _ic_series_impl(factor, fwd):  # noqa: F811 - njit override
            t_n = factor.shape[0]
            out = np.full(t_n, np.nan)
            for t in numba.prange(t_n):
                out[t] = _pearson_pair(factor[t], fwd[t])
            return out

        @numba.njit(parallel=True, cache=True, fastmath=False)
        def _rank_ic_series_impl(factor_rank, fwd, factor):  # noqa: F811
            t_n, n_sym = factor.shape
            out = np.full(t_n, np.nan)
            for t in numba.prange(t_n):
                mask = np.empty(n_sym, dtype=np.bool_)
                cnt = 0
                for j in range(n_sym):
                    ok = np.isfinite(factor[t, j]) and np.isfinite(fwd[t, j])
                    mask[j] = ok
                    if ok:
                        cnt += 1
                if cnt < 2:
                    continue
                fr = _avg_ranks_masked(factor[t], mask)
                rr = _avg_ranks_masked(fwd[t], mask)
                out[t] = _pearson_pair(fr, rr)
            return out

        # Smoke-compile so a lazy compile failure degrades to numpy here, once.
        _probe = np.array([[0.0, 1.0, 2.0], [2.0, 1.0, 0.0]])
        _ic_series_impl(_probe, _probe)
        _rank_ic_series_impl(_probe, _probe, _probe)
    except Exception:  # pragma: no cover - fall back to the pure-numpy defs above
        _HAS_NUMBA = False

        def _ic_series_impl(factor, fwd):  # type: ignore[misc] # noqa: F811
            t_n = factor.shape[0]
            out = np.full(t_n, np.nan)
            for t in range(t_n):
                out[t] = _pearson_pair(factor[t], fwd[t])
            return out

        def _rank_ic_series_impl(factor_rank, fwd, factor):  # type: ignore[misc] # noqa: F811
            t_n, n_sym = factor.shape
            out = np.full(t_n, np.nan)
            for t in range(t_n):
                mask = np.isfinite(factor[t]) & np.isfinite(fwd[t])
                if int(mask.sum()) < 2:
                    continue
                fr = _avg_ranks_masked(factor[t], mask)
                rr = _avg_ranks_masked(fwd[t], mask)
                out[t] = _pearson_pair(fr, rr)
            return out


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------


def ic_series(factor: np.ndarray, fwd: np.ndarray) -> np.ndarray:
    """Per-date Pearson IC, ``(T,)`` float64. NaN-aware (joint-finite, >=2 valid)."""
    factor = np.ascontiguousarray(factor, dtype=np.float64)
    fwd = np.ascontiguousarray(fwd, dtype=np.float64)
    if factor.shape != fwd.shape:
        raise ValueError("factor and fwd must share the same (T, N) shape")
    return np.asarray(_ic_series_impl(factor, fwd), dtype=np.float64)


def _factor_ranks(factor: np.ndarray) -> np.ndarray:
    """Per-date average ranks of ``factor`` over its finite symbols (NaN elsewhere)."""
    t_n, n_sym = factor.shape
    out = np.full((t_n, n_sym), np.nan)
    for t in range(t_n):
        mask = np.isfinite(factor[t])
        out[t] = _avg_ranks_masked(factor[t], mask)
    return out


def rank_ic_series(factor: np.ndarray, fwd: np.ndarray) -> np.ndarray:
    """Per-date Spearman RankIC, ``(T,)`` float64.

    Spearman is computed as the Pearson correlation of average ranks (ties
    averaged) over the symbols finite in both rows.
    """
    factor = np.ascontiguousarray(factor, dtype=np.float64)
    fwd = np.ascontiguousarray(fwd, dtype=np.float64)
    if factor.shape != fwd.shape:
        raise ValueError("factor and fwd must share the same (T, N) shape")
    # factor_rank is recomputed per-date inside the kernel against the joint mask;
    # the precomputed-ranks argument is kept for the multi-horizon fast path below.
    factor_rank = _factor_ranks(factor)
    return np.asarray(
        _rank_ic_series_impl(factor_rank, fwd, factor), dtype=np.float64
    )


def ic_summary(series: np.ndarray) -> tuple[float, float]:
    """Reduce a per-date IC series to ``(mean, mean/std)``, ignoring NaN (ddof=1)."""
    s = np.asarray(series, dtype=np.float64)
    finite = s[np.isfinite(s)]
    if finite.size == 0:
        return (np.nan, np.nan)
    mean = float(finite.mean())
    if finite.size < 2:
        return (mean, np.nan)
    std = float(finite.std(ddof=1))
    icir = mean / std if std > 0.0 else np.nan
    return (mean, icir)


def evaluate_ic(
    factor: np.ndarray, fwd_by_h: dict[int, np.ndarray]
) -> dict[str, object]:
    """Headline IC bundle across horizons; factor ranked once and reused.

    The shortest horizon ``min(h)`` is the headline used for ``ic`` / ``icir`` /
    ``rank_ic`` / ``rank_icir`` and the returned ``ic_series`` / ``rank_ic_series``.
    ``ic_by_horizon`` maps every horizon to its mean RankIC.

    Returns keys: ``ic, icir, rank_ic, rank_icir, ic_by_horizon, ic_series,
    rank_ic_series``.
    """
    if not fwd_by_h:
        raise ValueError("fwd_by_h must contain at least one horizon")
    factor = np.ascontiguousarray(factor, dtype=np.float64)
    horizons = sorted(fwd_by_h.keys())
    head = horizons[0]

    # Rank the factor once (per date) and reuse across all horizons.
    factor_rank = _factor_ranks(factor)

    rank_ic_by_h: dict[int, float] = {}
    head_ic_series = None
    head_rank_series = None
    for h in horizons:
        fwd = np.ascontiguousarray(fwd_by_h[h], dtype=np.float64)
        if fwd.shape != factor.shape:
            raise ValueError(f"fwd[{h}] shape {fwd.shape} != factor {factor.shape}")
        rseries = np.asarray(
            _rank_ic_series_impl(factor_rank, fwd, factor), dtype=np.float64
        )
        rank_ic_by_h[h] = ic_summary(rseries)[0]
        if h == head:
            head_rank_series = rseries
            head_ic_series = ic_series(factor, fwd)

    ic_mean, icir = ic_summary(head_ic_series)
    rank_mean, rank_icir = ic_summary(head_rank_series)
    return {
        "ic": ic_mean,
        "icir": icir,
        "rank_ic": rank_mean,
        "rank_icir": rank_icir,
        "ic_by_horizon": rank_ic_by_h,
        "ic_series": head_ic_series,
        "rank_ic_series": head_rank_series,
    }
