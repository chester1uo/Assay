"""Weight construction from a processed cross-sectional signal — design-doc §2.4 / §3.3.

Pure numpy + scipy (+ OSQP/Clarabel when installed): no engine, data-layer, cvxpy or sklearn
imports. The input is the *already cross-sectionally normalised* signal for one
rebalance date — a 1-D ``(N,)`` vector aligned to the eligible symbols of that
date — plus, for the risk-aware methods, a ``(W, N)`` trailing returns window
(``W`` lookback dates on axis 0, ``N`` symbols on axis 1). Every function returns
a 1-D ``(N,)`` target weight vector scaled to the config's gross/net exposure; the
per-position box, turnover, count and sector constraints (design-doc §2.5) are
applied *separately* by the caller (the constraint applicator), not here.

Everything is strictly **NaN-aware**: a symbol whose signal (or whose returns
column) is non-finite is simply excluded from the cross-section for that date and
receives weight ``0`` — one missing symbol never poisons the whole vector. All
methods degrade gracefully: an empty or all-NaN cross-section yields an all-zero
weight vector, and the risk-aware optimisers fall back to the cheap closed-form
constructors when their numerics are degenerate.

Method → engine:

* :func:`equal_weight`     — pure numpy (long top / short bottom half by sign).
* :func:`signal_prop`      — pure numpy (weights ∝ signal, demeaned if long/short).
* :func:`quantile_weight`  — pure numpy (top/bottom quantile groups, equal within).
* :func:`ledoit_wolf_cov`  — pure numpy (closed-form LW shrinkage constant, §7.5).
* :func:`mean_variance`    — OSQP (+ Clarabel for a binding turnover cap) when installed
  (``perf`` extra), else scipy ``optimize.minimize(method='SLSQP')`` (§3.3);
  equal-weight fallback on failure.
* :func:`risk_parity`      — pure numpy (iterative equal-risk-contribution).
* :func:`construct_weights`— dispatch on ``config.weight_method``.

The math is grounded to the only data Assay has (US equities); nothing here reads
ST/suspended/sector/index inputs, so the module runs identically on any market.
"""

from __future__ import annotations

import numpy as np
from scipy import optimize, sparse

try:  # optional (``perf`` extra): the mean-variance QP in ~0.3 s instead of SLSQP's ~3 s
    import osqp
except ImportError:  # pragma: no cover - depends on the environment
    osqp = None
try:  # optional (``perf`` extra): the turnover-capped QP, where OSQP's ADMM is slow
    import clarabel
except ImportError:  # pragma: no cover - depends on the environment
    clarabel = None

from assay.portfolio.config import PortfolioBacktestConfig

# ---------------------------------------------------------------------------
# shared helpers
# ---------------------------------------------------------------------------


def _finite_mask(signal: np.ndarray) -> np.ndarray:
    """Boolean ``(N,)`` mask of the cross-section's finite (tradable) entries."""
    return np.isfinite(signal)


def _scale_long_only(w: np.ndarray, gross: float) -> np.ndarray:
    """Scale a non-negative weight vector so its sum equals ``gross``.

    Long-only weights have net == gross, so scaling to gross also pins the net.
    A degenerate all-zero vector is returned unchanged.
    """
    total = w.sum()
    if total <= 0.0 or not np.isfinite(total):
        return w
    return w * (gross / total)


def _scale_long_short(w: np.ndarray, gross: float, net: float) -> np.ndarray:
    """Scale signed weights to a target ``gross`` (sum |w|) and ``net`` (sum w).

    The positive and negative legs are scaled independently so that
    ``L + S == gross`` (gross budget) and ``L - S == net`` (net budget), i.e.
    long leg ``L = (gross + net) / 2`` and short leg ``S = (gross - net) / 2``.
    Whichever leg is empty collapses that constraint gracefully (the other leg
    simply carries the achievable exposure).
    """
    pos = np.clip(w, 0.0, None)
    neg = np.clip(-w, 0.0, None)
    sp, sn = pos.sum(), neg.sum()
    long_budget = max((gross + net) / 2.0, 0.0)
    short_budget = max((gross - net) / 2.0, 0.0)
    out = np.zeros_like(w)
    if sp > 0.0 and np.isfinite(sp):
        out += pos * (long_budget / sp)
    if sn > 0.0 and np.isfinite(sn):
        out -= neg * (short_budget / sn)
    return out


def _scale(w: np.ndarray, long_short: bool, gross: float, net: float) -> np.ndarray:
    """Scale a raw weight vector to the config gross/net budget (dispatch on side)."""
    if long_short:
        return _scale_long_short(w, gross, net)
    return _scale_long_only(np.clip(w, 0.0, None), gross)


# ---------------------------------------------------------------------------
# 1. equal weight  (numpy)
# ---------------------------------------------------------------------------


def equal_weight(
    signal: np.ndarray,
    long_short: bool = False,
    gross: float = 1.0,
    net: float = 1.0,
) -> np.ndarray:
    """Equal-weight the eligible cross-section (design-doc §2.4 ``equal``).

    Long-only: every finite-signal symbol gets ``gross / k``. Long/short: the
    top half by signal goes long, the bottom half short, each leg equal-weighted
    and scaled to the gross/net budget. NaN signals receive weight ``0``.
    """
    signal = np.asarray(signal, dtype=np.float64).ravel()
    n = signal.shape[0]
    w = np.zeros(n, dtype=np.float64)
    mask = _finite_mask(signal)
    k = int(mask.sum())
    if k == 0:
        return w
    if not long_short:
        w[mask] = 1.0
        return _scale_long_only(w, gross)
    # Long/short: split the finite cross-section at its median signal.
    vals = signal[mask]
    med = np.median(vals)
    longs = mask & (signal >= med)
    shorts = mask & (signal < med)
    # If everything ties at the median, fall back to a sign split on demeaned signal.
    if not shorts.any() or not longs.any():
        demeaned = np.where(mask, signal - np.mean(vals), np.nan)
        longs = mask & (demeaned >= 0)
        shorts = mask & (demeaned < 0)
    w[longs] = 1.0
    w[shorts] = -1.0
    return _scale_long_short(w, gross, net)


# ---------------------------------------------------------------------------
# 2. signal-proportional  (numpy)
# ---------------------------------------------------------------------------


def signal_prop(
    signal: np.ndarray,
    long_short: bool = False,
    gross: float = 1.0,
    net: float = 1.0,
) -> np.ndarray:
    """Weights proportional to the (processed) signal (design-doc §2.4 ``signal_prop``).

    Long-only: weight ∝ the non-negative part of the signal (shifted up by its
    cross-sectional min so a uniformly negative signal still allocates). Long/short:
    weight ∝ the **demeaned** signal, so above-average names go long and
    below-average short, then scaled to the gross/net budget. NaN → weight ``0``.
    """
    signal = np.asarray(signal, dtype=np.float64).ravel()
    n = signal.shape[0]
    w = np.zeros(n, dtype=np.float64)
    mask = _finite_mask(signal)
    if not mask.any():
        return w
    vals = signal[mask]
    if long_short:
        # Demean within the eligible cross-section → signed tilts around zero.
        demeaned = vals - np.mean(vals)
        w[mask] = demeaned
        out = _scale_long_short(w, gross, net)
        # All-equal signal demeans to zero everywhere → fall back to equal L/S.
        if not np.any(out):
            return equal_weight(signal, long_short=True, gross=gross, net=net)
        return out
    # Long-only: shift to non-negative so the smallest name gets ~0 weight.
    lo = np.min(vals)
    shifted = vals - lo
    if shifted.sum() <= 0.0:  # all equal → equal weight
        w[mask] = 1.0
    else:
        w[mask] = shifted
    return _scale_long_only(w, gross)


# ---------------------------------------------------------------------------
# 3. quantile / quintile / decile  (numpy)
# ---------------------------------------------------------------------------


def quantile_weight(
    signal: np.ndarray,
    long_n: int = 1,
    short_n: int = 1,
    n_groups: int = 5,
    long_short: bool = False,
    gross: float = 1.0,
    net: float = 1.0,
) -> np.ndarray:
    """Bucket the cross-section into ``n_groups`` equal-count quantiles and trade the tails.

    The top ``long_n`` groups go long (equal-weight within), and — when
    ``long_short`` — the bottom ``short_n`` groups go short. With ``n_groups == 5``
    this is the quintile method; with ``10`` the decile method (design-doc §2.4
    ``quintile`` / ``decile``). Ranking is dense and NaN-aware: only finite-signal
    symbols are bucketed; the rest get weight ``0``. Degrades to equal-weight if
    the cross-section is too small to split into groups.
    """
    signal = np.asarray(signal, dtype=np.float64).ravel()
    n = signal.shape[0]
    w = np.zeros(n, dtype=np.float64)
    mask = _finite_mask(signal)
    k = int(mask.sum())
    n_groups = max(int(n_groups), 2)
    long_n = max(int(long_n), 1)
    short_n = max(int(short_n), 0)
    if k == 0:
        return w
    if k < n_groups:  # too few names to bucket meaningfully
        return equal_weight(signal, long_short=long_short, gross=gross, net=net)
    idx = np.where(mask)[0]
    vals = signal[idx]
    # Ascending rank order; lowest signal = group 0, highest = group n_groups-1.
    order = np.argsort(vals, kind="stable")
    # Assign each ranked position a group label by its rank fraction.
    ranks = np.empty(k, dtype=np.int64)
    ranks[order] = np.arange(k)
    group = np.minimum((ranks * n_groups) // k, n_groups - 1)
    long_groups = group >= (n_groups - long_n)
    leg = np.zeros(k, dtype=np.float64)
    leg[long_groups] = 1.0
    if long_short and short_n > 0:
        short_groups = group < short_n
        leg[short_groups] = -1.0
    w[idx] = leg
    return _scale(w, long_short, gross, net)


# ---------------------------------------------------------------------------
# 4. Ledoit-Wolf shrinkage covariance  (numpy, no sklearn)
# ---------------------------------------------------------------------------


def _ledoit_wolf_dense(x: np.ndarray) -> np.ndarray:
    """LW shrinkage on a dense, finite, **already demeaned** ``(W, K)`` block → ``(K, K)``.

    Closed-form shrinkage of the sample covariance ``S`` toward a scaled-identity
    target ``μ·I`` (``μ = trace(S)/K`` — the average variance): the shrunk estimate
    is ``δ·μ·I + (1-δ)·S`` with the optimal intensity ``δ`` from Ledoit & Wolf
    (2004), ``δ = b² / d²`` where ``d² = ‖S - μI‖²_F`` (dispersion of ``S`` from the
    target) and ``b²`` is the estimation error of ``S`` (clipped to ``[0, d²]`` so
    ``δ ∈ [0, 1]``). The ~15-line numpy replacement for ``sklearn``'s ``LedoitWolf``.
    """
    w_obs, k = x.shape
    sample = (x.T @ x) / w_obs  # 1/W normalisation (LW convention)
    mu = np.trace(sample) / k
    target = mu * np.eye(k)
    d2 = float(np.sum((sample - target) ** 2))
    if d2 <= 0.0:  # sample already == target (e.g. one asset) → nothing to shrink
        return sample
    # b² : variance of the sample-covariance entries (Ledoit-Wolf "pi-hat"),
    # Σ_t ‖x_t x_tᵀ − S‖²_F = Σ_t ‖x_t‖⁴ − W·‖S‖²_F (as Σ_t x_tᵀ S x_t = W·‖S‖²_F):
    # O(W·K) instead of W outer products of K × K.
    b2_sum = float(np.sum(np.sum(x * x, axis=1) ** 2)) - w_obs * float(np.sum(sample * sample))
    b2 = min(max(b2_sum, 0.0) / (w_obs**2), d2)  # clip → δ ∈ [0, 1]
    delta = b2 / d2
    shrunk = delta * target + (1.0 - delta) * sample
    return 0.5 * (shrunk + shrunk.T)  # symmetrise against round-off


def ledoit_wolf_cov(returns_window: np.ndarray) -> np.ndarray:
    """Ledoit-Wolf shrinkage covariance of a ``(W, N)`` returns window (design-doc §7.5).

    Shrinks the sample covariance toward a scaled-identity target with the optimal
    Ledoit-Wolf intensity (see :func:`_ledoit_wolf_dense`) — the cvxpy/sklearn-free
    numpy replacement for ``sklearn.covariance.LedoitWolf``.

    Strictly NaN-aware on a per-symbol basis: **dead columns** (any non-finite
    entry — e.g. a symbol with no return history this window) are dropped *first*,
    the LW estimate is computed on the live sub-block (over its fully-finite rows),
    then re-embedded into the full ``(N, N)`` matrix with ``NaN`` on the dead
    rows/columns. That NaN marker lets downstream eligibility masks
    (:func:`mean_variance`, :func:`risk_parity`) exclude the dead symbols rather
    than mistake an identity-filled diagonal for tradable variance. On a degenerate
    window (no live columns, or fewer than two clean rows) it returns a tiny
    diagonal so callers stay numerically safe.
    """
    r = np.asarray(returns_window, dtype=np.float64)
    if r.ndim != 2:
        raise ValueError("returns_window must be a 2-D (W, N) matrix")
    n = r.shape[1]
    if n == 0:
        return np.zeros((0, 0), dtype=np.float64)
    # Live symbols: columns with no non-finite entries over the window. A whole
    # all-NaN column must not void every row, so we drop dead columns up front.
    live = np.isfinite(r).all(axis=0)
    if not live.any():
        return np.eye(n, dtype=np.float64) * 1e-6
    sub = r[:, live]
    # Within the live block, drop any rows that are still non-finite (defensive).
    sub = sub[np.isfinite(sub).all(axis=1)]
    k = int(live.sum())
    if sub.shape[0] < 2:
        return np.eye(n, dtype=np.float64) * 1e-6
    x = sub - sub.mean(axis=0, keepdims=True)
    cov_live = _ledoit_wolf_dense(x)
    if k == n:
        return cov_live
    # Re-embed live block into the full matrix; dead rows/cols carry NaN so the
    # eligibility masks downstream exclude those symbols cleanly.
    out = np.full((n, n), np.nan, dtype=np.float64)
    idx = np.where(live)[0]
    out[np.ix_(idx, idx)] = cov_live
    return out


# ---------------------------------------------------------------------------
# 5. mean-variance optimisation  (OSQP, scipy SLSQP without it)
# ---------------------------------------------------------------------------


def _covariance(returns_window: np.ndarray, config: PortfolioBacktestConfig) -> np.ndarray:
    """Covariance estimate per ``config.cov_method`` over the trailing ``cov_window``."""
    r = np.asarray(returns_window, dtype=np.float64)
    if r.ndim != 2 or r.shape[0] == 0:
        n = 0 if r.ndim != 2 else r.shape[1]
        return np.eye(n, dtype=np.float64) * 1e-6
    window = r[-int(config.cov_window) :]
    n = r.shape[1]
    if config.cov_method == "sample":
        # Mirror ledoit_wolf_cov's per-symbol NaN handling: drop dead columns, cov
        # on the live block, re-embed with NaN on dead rows/cols.
        live = np.isfinite(window).all(axis=0)
        if not live.any():
            return np.eye(n, dtype=np.float64) * 1e-6
        sub = window[:, live]
        sub = sub[np.isfinite(sub).all(axis=1)]
        if sub.shape[0] < 2:
            return np.eye(n, dtype=np.float64) * 1e-6
        cov_live = np.cov(sub, rowvar=False)
        cov_live = np.atleast_2d(cov_live)
        if int(live.sum()) == n:
            return cov_live
        out = np.full((n, n), np.nan, dtype=np.float64)
        idx = np.where(live)[0]
        out[np.ix_(idx, idx)] = cov_live
        return out
    # 'ledoit_wolf' (default) and 'factor_model' (no factor data → LW fallback).
    return ledoit_wolf_cov(window)


def _covariance_factor(returns_window: np.ndarray, config: PortfolioBacktestConfig):
    """:func:`_covariance` as ``(F, d)`` with ``Σ = d·I + FᵀF`` on its finite block.

    Both estimators are a scaled identity plus a rank-``W`` term: Ledoit-Wolf is
    ``δμ·I + (1−δ)·xᵀx/W``, the sample covariance ``xᵀx/(W−1)``, with ``x`` the
    demeaned ``(W, K)`` window of live columns. ``F`` is ``(W, N)`` with NaN on the
    dead columns (the NaN rows/columns of :func:`_covariance`); degenerate windows
    give ``F`` with no rows and ``d = 1e-6`` (its tiny diagonal). Computing ``δ``
    from the ``W × W`` Gram matrix agrees with the dense estimate to round-off."""
    r = np.asarray(returns_window, dtype=np.float64)
    n = r.shape[1] if r.ndim == 2 else 0
    tiny = (np.zeros((0, n)), 1e-6)
    if r.ndim != 2 or r.shape[0] == 0:
        return tiny
    window = r[-int(config.cov_window) :]
    live = np.isfinite(window).all(axis=0)
    if not live.any():
        return tiny
    sub = window[:, live]
    sub = sub[np.isfinite(sub).all(axis=1)]
    w_obs, k = sub.shape
    if w_obs < 2:
        return tiny
    x = sub - sub.mean(axis=0, keepdims=True)
    f = np.full((w_obs, n), np.nan, dtype=np.float64)
    if config.cov_method == "sample":
        f[:, live] = x / np.sqrt(w_obs - 1)
        return f, 0.0
    # Ledoit-Wolf intensity as in _ledoit_wolf_dense, with ‖S‖²_F = ‖x xᵀ‖²_F / W².
    gram = x @ x.T if w_obs < k else x.T @ x
    s2 = float(np.sum(gram * gram)) / w_obs**2
    mu = float(np.sum(x * x)) / (w_obs * k)
    d2 = s2 - k * mu * mu
    if d2 <= 0.0:
        f[:, live] = x / np.sqrt(w_obs)
        return f, 0.0
    b2_sum = float(np.sum(np.sum(x * x, axis=1) ** 2)) - w_obs * s2
    delta = min(max(b2_sum, 0.0) / (w_obs**2), d2) / d2
    f[:, live] = x * np.sqrt((1.0 - delta) / w_obs)
    return f, delta * mu


_OSQP_SETTINGS = {"verbose": False, "eps_abs": 1e-6, "eps_rel": 1e-6, "max_iter": 20000, "polishing": False}
# eps 1e-6 bounds each constraint row; the budget and the turnover sum over the
# names, so their error grows to ~k·1e-6. The constraint applicator rescales the
# exposure and caps the turnover exactly afterwards.
_OSQP_TOL = 1e-4
# With the turnover cap binding the program is close to an LP (the risk term is
# small next to αᵀw) and ADMM needs ~10⁴ iterations at eps 1e-6 (~5 s at k≈500,
# W=252). eps 1e-5 with rho 0.01 halves that (1-3 s) and stays within ~5e-4 of
# the optimum, against ~0.1 for scaling the uncapped optimum onto the cap.
_OSQP_CAPPED = {"eps_abs": 1e-5, "eps_rel": 1e-5}
_OSQP_CAPPED_TOL = 1e-3


def _mv_capped_clarabel(a, f, d, cur, lam, lo, hi, net, cap):
    """The turnover-capped program with Clarabel (interior point): ~15 iterations
    and ~1 s at k≈500, W=252, exact to ~1e-8, where ADMM takes 10⁴ iterations on
    this near-LP. Same variables as :func:`_mv_osqp`: ``[w; y; t]`` (``y = F w`` when
    ``W < k``) with ``t ≥ |w − cur|`` and ``Σt ≤ 2·cap``.

    Returns the weights, ``False`` when the program is infeasible, or ``None`` when
    Clarabel did not solve it."""
    k, m = a.shape[0], f.shape[0]
    low_rank = 0 < m < k
    i_k = sparse.identity(k, format="csc")
    ones = sparse.csc_matrix(np.ones((1, k)))
    if low_rank:
        blocks = [sparse.diags(np.full(k, lam * d)), sparse.diags(np.full(m, lam))]
        mid = [None]
    else:
        blocks = [sparse.csc_matrix(np.triu(lam * (d * np.eye(k) + f.T @ f)))]
        mid = []
    blocks.append(sparse.csc_matrix((k, k)))
    # Zero cone: Σw = net (and F w − y = 0); nonnegative cone (rows ≤ b): the box,
    # w − t ≤ cur, −w − t ≤ −cur and Σt ≤ 2·cap.
    rows = [[ones, *mid, None]]
    if low_rank:
        rows.append([sparse.csc_matrix(f), -sparse.identity(m, format="csc"), None])
    n_eq = 1 + (m if low_rank else 0)
    rows += [[i_k, *mid, None], [-i_k, *mid, None], [i_k, *mid, -i_k], [-i_k, *mid, -i_k], [None, *mid, ones]]
    b = np.concatenate([[net], np.zeros(n_eq - 1), np.full(k, hi), np.full(k, -lo), cur, -cur, [2.0 * cap]])
    nx = sum(blk.shape[0] for blk in blocks)
    settings = clarabel.DefaultSettings()
    settings.verbose = False
    sol = clarabel.DefaultSolver(
        sparse.block_diag(blocks, format="csc"), np.concatenate([-a, np.zeros(nx - k)]),
        sparse.bmat(rows, format="csc"), b,
        [clarabel.ZeroConeT(n_eq), clarabel.NonnegativeConeT(4 * k + 1)], settings,
    ).solve()
    status = clarabel.SolverStatus
    if sol.status in (status.PrimalInfeasible, status.AlmostPrimalInfeasible):
        return False
    if sol.status not in (status.Solved, status.AlmostSolved):
        return None
    x = np.asarray(sol.x)[:k]
    return np.clip(x, lo, hi) if np.all(np.isfinite(x)) else None


def _mv_osqp(a, f, d, cur, lam, lo, hi, net, cap):
    """The mean-variance QP with OSQP, for ``Σ = d·I + FᵀF`` (``F`` is ``(W, k)``).

    When ``W < k`` the risk term is written ``½λ(d‖w‖² + ‖y‖²)`` with ``y = F w``: a
    diagonal objective and ``W`` extra rows instead of a dense ``k × k`` one, ~3×
    cheaper per iteration. The program is first solved without the turnover cap;
    only if that optimum breaks the cap is it solved again with ``t ≥ |w − cur|``,
    ``Σt ≤ 2·cap`` (the L1 constraint made linear) — by Clarabel when it is installed
    (:func:`_mv_capped_clarabel`), else by OSQP, whose ADMM needs far more iterations
    there: it runs at eps 1e-5, and a second ``rho`` is tried when the first does not
    converge. If neither converges, an iterate within 1e-2 of the budget and the cap
    is kept; failing that (or if the capped program is infeasible) the uncapped
    optimum is returned.
    Either way the constraint applicator then enforces the cap exactly by scaling
    the trade toward ``cur``.

    Returns the weights, ``False`` when the budget and box alone are infeasible, or
    ``None`` when OSQP did not converge on the uncapped program (the caller tries
    SLSQP)."""
    k, m = a.shape[0], f.shape[0]
    low_rank = 0 < m < k
    i_k = sparse.identity(k, format="csc")
    if low_rank:
        p_w = sparse.diags(np.full(k, lam * d), format="csc")
    else:
        p_w = sparse.csc_matrix(np.triu(lam * (d * np.eye(k) + f.T @ f)))
    ones = sparse.csc_matrix(np.ones((1, k)))
    rows = [[ones], [i_k]]
    lower, upper = [[net], np.full(k, lo)], [[net], np.full(k, hi)]
    blocks = [p_w]
    x0 = [cur]
    if low_rank:
        blocks.append(sparse.diags(np.full(m, lam), format="csc"))
        rows = [r + [None] for r in rows] + [[sparse.csc_matrix(f), -sparse.identity(m, format="csc")]]
        lower.append(np.zeros(m))
        upper.append(np.zeros(m))
        x0.append(f @ cur)
    statuses = osqp.SolverStatus

    def solve(rows, blocks, lower, upper, x0, **settings):
        nx = sum(b.shape[0] for b in blocks)
        solver = osqp.OSQP()
        solver.setup(sparse.block_diag(blocks, format="csc"), np.concatenate([-a, np.zeros(nx - k)]),
                     sparse.bmat(rows, format="csc"), np.concatenate(lower), np.concatenate(upper),
                     **{**_OSQP_SETTINGS, **settings})
        solver.warm_start(x=np.concatenate(x0))
        res = solver.solve(raise_error=False)
        x = res.x[:k] if res.x is not None else None
        w = np.clip(x, lo, hi) if x is not None and np.all(np.isfinite(x)) else None
        return res.info.status_val, w

    def within_cap(w, tol):
        return np.abs(w - cur).sum() <= 2.0 * cap + tol

    status, w = solve(rows, blocks, lower, upper, x0)
    if status in (statuses.OSQP_PRIMAL_INFEASIBLE, statuses.OSQP_PRIMAL_INFEASIBLE_INACCURATE):
        return False
    if status != statuses.OSQP_SOLVED or w is None or abs(w.sum() - net) > _OSQP_TOL:
        return None
    if cap >= 2.0 or within_cap(w, _OSQP_TOL):  # 2.0 is the no-op ceiling (turnover budget never binds)
        return w
    if clarabel is not None:
        try:
            wc = _mv_capped_clarabel(a, f, d, cur, lam, lo, hi, net, cap)
        except Exception:  # noqa: BLE001 - any Clarabel failure falls through to OSQP
            wc = None
        if wc is False:
            return w
        if wc is not None and abs(wc.sum() - net) <= _OSQP_TOL and within_cap(wc, _OSQP_TOL):
            return wc
    # The cap binds: add t (one block column) with w − t ≤ cur ≤ w + t and Σt ≤ 2·cap.
    pad = [None] * (len(blocks) - 1)
    rows = [r + [None] for r in rows] + [[i_k, *pad, -i_k], [i_k, *pad, i_k], [None, *pad, ones]]
    lower += [np.full(k, -np.inf), cur, [-np.inf]]
    upper += [cur, np.full(k, np.inf), [2.0 * cap]]
    blocks = blocks + [sparse.csc_matrix((k, k))]
    x0 = x0 + [np.abs(w - cur)]
    tol = _OSQP_CAPPED_TOL
    near = None
    for rho in (0.01, 0.1):
        status, wc = solve(rows, blocks, lower, upper, x0, rho=rho, **_OSQP_CAPPED)
        if status in (statuses.OSQP_PRIMAL_INFEASIBLE, statuses.OSQP_PRIMAL_INFEASIBLE_INACCURATE):
            break
        if wc is None:
            continue
        if status == statuses.OSQP_SOLVED and abs(wc.sum() - net) <= tol and within_cap(wc, tol):
            return wc
        if near is None and abs(wc.sum() - net) <= 10 * tol and within_cap(wc, 10 * tol):
            near = wc
    return near if near is not None else w


def _mv_slsqp(a, cov, cur, lam, lo, hi, net, cap):
    """The same program with ``scipy.optimize.minimize(method='SLSQP')``; ``None`` on failure."""
    k = a.shape[0]
    bounds = [(lo, hi)] * k

    def neg_obj(w: np.ndarray) -> float:
        return -(a @ w) + 0.5 * lam * float(w @ cov @ w)

    def neg_grad(w: np.ndarray) -> np.ndarray:
        return -a + lam * (cov @ w)

    # Constraint Jacobians are given: without them SLSQP differences each constraint
    # numerically, k evaluations per iteration.
    constraints: list[dict] = [
        {"type": "eq", "fun": lambda w: float(np.sum(w) - net), "jac": lambda w: np.ones_like(w)}
    ]
    # Turnover cap as an L1 constraint: ‖w − cur‖₁ ≤ 2·cap (subgradient −sign(w − cur)).
    # SLSQP handles the piecewise-linear L1 acceptably for these small N.
    if cap < 2.0:  # 2.0 is the no-op ceiling (turnover budget never binds)
        constraints.append(
            {"type": "ineq", "fun": lambda w: 2.0 * cap - float(np.sum(np.abs(w - cur))),
             "jac": lambda w: -np.sign(w - cur)}
        )

    # Warm-start from the current weights projected onto the eligible set.
    x0 = cur.copy()
    s = x0.sum()
    if not np.isfinite(s) or s == 0.0:
        x0 = np.full(k, net / k)
    try:
        res = optimize.minimize(
            neg_obj,
            x0,
            jac=neg_grad,
            method="SLSQP",
            bounds=bounds,
            constraints=constraints,
            options={"maxiter": 200, "ftol": 1e-9},
        )
    except Exception:
        return None
    if not res.success or not np.all(np.isfinite(res.x)):
        return None
    return res.x


def mean_variance(
    signal_alpha: np.ndarray,
    returns_window: np.ndarray,
    current_w: np.ndarray,
    config: PortfolioBacktestConfig,
) -> np.ndarray:
    """Maximise ``αᵀw − (λ/2)·wᵀΣw`` over the box / budget / turnover set (§3.3).

    Solved with OSQP when it is installed (the ``perf`` extra), else with
    ``scipy.optimize.minimize(method='SLSQP')`` (minimising the negated objective),
    the cvxpy-free replacement for the design-doc ``OptimizationRebalancer``.
    SLSQP's cost grows as k³ in the names (≈3 s per rebalance at k≈490, and with a
    binding turnover cap it ran for minutes and failed); OSQP on the low-rank form of
    ``Σ`` (:func:`_covariance_factor`) takes ~0.3 s; when the cap binds, Clarabel
    takes ~1 s more (OSQP alone ~2 s).
    SLSQP is the fallback when OSQP does not converge. Constraints, mirroring §3.3:

    * budget ``sum(w) == net_exposure`` (equality);
    * box bounds — long-only ``[min_single_weight, max_single_weight]``, long/short
      ``[-max_single_weight, max_single_weight]``;
    * optional L1 turnover ``‖w − current_w‖₁ ≤ 2·max_turnover_per_period`` when the
      cap is binding (< the no-op value of 2.0) — linear with auxiliary variables
      for OSQP, a subgradient constraint for SLSQP.

    ``Σ`` comes from :func:`_covariance` (Ledoit-Wolf by default), ``λ`` from
    ``mv_risk_aversion``. NaN-aware: non-finite-alpha symbols are pinned to weight
    ``0`` and dropped from the program. When the budget and box are infeasible, or
    on any solver failure / non-finite result, the function returns the
    :func:`equal_weight` fallback (design-doc §3.3, "Fallback: equal weight within
    eligible universe"). With OSQP, a turnover cap that cannot be met is left to the
    constraint applicator instead (see :func:`_mv_osqp`).
    """
    alpha = np.asarray(signal_alpha, dtype=np.float64).ravel()
    n = alpha.shape[0]
    current_w = np.asarray(current_w, dtype=np.float64).ravel()
    if current_w.shape[0] != n:
        current_w = np.zeros(n, dtype=np.float64)
    out = np.zeros(n, dtype=np.float64)
    mask = _finite_mask(alpha)
    k = int(mask.sum())
    if k == 0:
        return out

    fallback = lambda: equal_weight(  # noqa: E731 - tiny local alias for the §3.3 fallback
        alpha, config.long_short, config.gross_exposure, config.net_exposure
    )

    if config.long_short:
        lo, hi = -config.max_single_weight, config.max_single_weight
    else:
        lo, hi = config.min_single_weight, config.max_single_weight
    lo, hi = float(lo), float(hi)
    lam = float(config.mv_risk_aversion)
    net, cap = float(config.net_exposure), float(config.max_turnover_per_period)
    # A feasible box must straddle the per-name share of the net budget; if the
    # caller's box is too tight to sum to net, the program is infeasible → fallback.

    w = None
    if osqp is not None:
        f_full, d = _covariance_factor(returns_window, config)
        if f_full.shape[1] != n:  # returns window misaligned with the signal
            return fallback()
        # Tradable = finite alpha AND a live returns column (see below).
        idx = np.where(mask & np.isfinite(f_full).all(axis=0))[0]
        if idx.size == 0:
            return fallback()
        try:
            # 1e-8: the same diagonal ridge as the dense path below.
            w = _mv_osqp(alpha[idx], f_full[:, idx], d + 1e-8, current_w[idx], lam, lo, hi, net, cap)
        except Exception:  # noqa: BLE001 - any OSQP failure falls through to SLSQP
            w = None
        if w is False:
            return fallback()
        if w is not None:
            out[idx] = w
            return out

    cov_full = _covariance(returns_window, config)
    if cov_full.shape != (n, n):  # returns window misaligned with the signal
        return fallback()
    # Tradable = finite alpha AND a finite covariance variance (a symbol with a
    # finite signal but no return history has a NaN diagonal from ledoit_wolf_cov;
    # including it would poison the QP, so drop it to weight 0). The sub-block over
    # the surviving diagonal is fully finite.
    mask = mask & np.isfinite(np.diag(cov_full))
    k = int(mask.sum())
    if k == 0:
        return fallback()
    idx = np.where(mask)[0]
    cov = cov_full[np.ix_(idx, idx)]
    # Ridge the diagonal so the QP is strictly convex even on a rank-deficient cov.
    cov = cov + np.eye(k) * 1e-8
    w = _mv_slsqp(alpha[idx], cov, current_w[idx], lam, lo, hi, net, cap)
    if w is None:
        return fallback()
    out[idx] = w
    return out


# ---------------------------------------------------------------------------
# 6. risk parity  (numpy iterative equal-risk-contribution)
# ---------------------------------------------------------------------------


def risk_parity(
    returns_window: np.ndarray,
    config: PortfolioBacktestConfig | None = None,
    gross: float = 1.0,
    max_iter: int = 500,
    tol: float = 1e-8,
) -> np.ndarray:
    """Long-only equal-risk-contribution weights from a ``(W, N)`` returns window (§2.4 ``risk_parity``).

    Each holding contributes the same share of portfolio variance. Starts from the
    inverse-volatility portfolio (the diagonal-only risk-parity solution) and runs
    the standard multiplicative fixed-point iteration
    ``wᵢ ← wᵢ · √(b / RCᵢ)`` (``RCᵢ = wᵢ·(Σw)ᵢ``, ``b = 1/N``), renormalising each
    step, until the risk contributions equalise. Pure numpy — no optimiser.

    NaN-aware: symbols with a non-finite covariance variance (NaN diagonal — i.e.
    no return history this window) or zero variance are dropped and get weight
    ``0``. The result is scaled to ``gross`` (or ``config.gross_exposure`` when a
    config is given). Falls back to inverse-vol if the iteration fails to converge.
    """
    if config is not None:
        gross = config.gross_exposure
    r = np.asarray(returns_window, dtype=np.float64)
    if r.ndim != 2 or r.shape[1] == 0:
        return np.zeros(0 if r.ndim != 2 else r.shape[1], dtype=np.float64)
    n = r.shape[1]
    out = np.zeros(n, dtype=np.float64)
    cov_full = ledoit_wolf_cov(r if config is None else r[-int(config.cov_window) :])
    var = np.diag(cov_full)
    # Eligible = finite, strictly-positive-variance assets. ledoit_wolf_cov marks a
    # dead symbol with a NaN diagonal, so testing the diagonal alone excludes it
    # (the off-diagonal carries NaN against dead symbols, so the *sub-block* over
    # the surviving diagonal is fully finite).
    elig = np.isfinite(var) & (var > 0)
    k = int(elig.sum())
    if k == 0:
        return out
    idx = np.where(elig)[0]
    cov = cov_full[np.ix_(idx, idx)]
    vol = np.sqrt(np.diag(cov))
    w = 1.0 / vol  # inverse-vol seed
    w = w / w.sum()
    b = 1.0 / k  # equal risk-budget target
    converged = False
    for _ in range(int(max_iter)):
        sigma_w = cov @ w
        rc = w * sigma_w  # per-asset risk contribution
        port_var = float(w @ sigma_w)
        if port_var <= 0 or not np.isfinite(port_var):
            break
        # Multiplicative update toward equal risk contribution, then renormalise.
        w_new = w * np.sqrt(b / np.maximum(rc / port_var, 1e-16))
        w_new = np.clip(w_new, 0.0, None)
        s = w_new.sum()
        if s <= 0 or not np.isfinite(s):
            break
        w_new /= s
        if np.max(np.abs(w_new - w)) < tol:
            w = w_new
            converged = True
            break
        w = w_new
    if not converged and not np.all(np.isfinite(w)):  # degenerate → inverse-vol
        w = (1.0 / vol) / np.sum(1.0 / vol)
    out[idx] = w
    return _scale_long_only(out, gross)


# ---------------------------------------------------------------------------
# 7. dispatch
# ---------------------------------------------------------------------------


def construct_weights(
    signal: np.ndarray,
    returns_window: np.ndarray | None,
    current_w: np.ndarray | None,
    config: PortfolioBacktestConfig,
) -> np.ndarray:
    """Build target weights for one rebalance date, dispatching on ``config.weight_method``.

    Routes the processed ``(N,)`` signal to the matching constructor and returns a
    ``(N,)`` weight vector scaled to the config's gross/net exposure. The §2.5 box,
    turnover, count and sector constraints are applied *separately* by the caller —
    only ``mv`` consults them here (its box/turnover live inside the QP).

    Method map (design-doc §2.4):

    * ``equal``        → :func:`equal_weight`
    * ``signal_prop``  → :func:`signal_prop`
    * ``quintile``     → :func:`quantile_weight` (5 groups)
    * ``decile``       → :func:`quantile_weight` (10 groups)
    * ``mv``           → :func:`mean_variance` (OSQP or SLSQP; equal-weight fallback)
    * ``risk_parity``  → :func:`risk_parity` (long-only; ignores ``long_short``)
    * ``bl``           → Black-Litterman has no posterior data here, so it routes to
      ``mv`` using the raw signal as the alpha view (documented note; same result
      as ``mv`` on this data).

    Robust to all-NaN / empty signals (returns an all-zero vector) and to a missing
    returns window (the risk-aware methods need it; ``mv``/``risk_parity`` fall back
    to their degenerate-window handling, which yields equal-weight / inverse-vol).
    """
    signal = np.asarray(signal, dtype=np.float64).ravel()
    n = signal.shape[0]
    if n == 0 or not _finite_mask(signal).any():
        return np.zeros(n, dtype=np.float64)

    method = config.weight_method
    ls = config.long_short
    gross = config.gross_exposure
    net = config.net_exposure

    if method == "equal":
        return equal_weight(signal, ls, gross, net)
    if method == "signal_prop":
        return signal_prop(signal, ls, gross, net)
    if method == "quintile":
        return quantile_weight(
            signal, config.quintile_long_n, config.quintile_short_n, 5, ls, gross, net
        )
    if method == "decile":
        return quantile_weight(
            signal, config.quintile_long_n, config.quintile_short_n, 10, ls, gross, net
        )
    if method in ("mv", "bl"):
        # 'bl' (Black-Litterman) has no equilibrium-return / view data available on
        # this data layer, so we treat the processed signal as the alpha view and
        # solve the same mean-variance program (documented in the design notes).
        rw = returns_window if returns_window is not None else np.empty((0, n))
        cw = current_w if current_w is not None else np.zeros(n)
        return mean_variance(signal, rw, cw, config)
    if method == "risk_parity":
        rw = returns_window if returns_window is not None else np.empty((0, n))
        return risk_parity(rw, config)
    # Unknown method (validator should preclude this) → safe equal-weight.
    return equal_weight(signal, ls, gross, net)
