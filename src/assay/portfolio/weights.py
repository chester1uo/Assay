"""Weight construction from a processed cross-sectional signal — design-doc §2.4 / §3.3.

Pure numpy + scipy: no engine, data-layer, cvxpy or sklearn imports. The input is
the *already cross-sectionally normalised* signal for one rebalance date — a 1-D
``(N,)`` vector aligned to the eligible symbols of that date — plus, for the
risk-aware methods, a ``(W, N)`` trailing returns window (``W`` lookback dates on
axis 0, ``N`` symbols on axis 1). Every function returns a 1-D ``(N,)`` target
weight vector scaled to the config's gross/net exposure; the per-position box,
turnover, count and sector constraints (design-doc §2.5) are applied *separately*
by the caller (the constraint applicator), not here.

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
* :func:`mean_variance`    — scipy ``optimize.minimize(method='SLSQP')`` QP (§3.3);
  equal-weight fallback on solver failure.
* :func:`risk_parity`      — pure numpy (iterative equal-risk-contribution).
* :func:`construct_weights`— dispatch on ``config.weight_method``.

The math is grounded to the only data Assay has (US equities); nothing here reads
ST/suspended/sector/index inputs, so the module runs identically on any market.
"""

from __future__ import annotations

import numpy as np
from scipy import optimize

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
# 5. mean-variance optimisation  (scipy SLSQP)
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


def mean_variance(
    signal_alpha: np.ndarray,
    returns_window: np.ndarray,
    current_w: np.ndarray,
    config: PortfolioBacktestConfig,
) -> np.ndarray:
    """Maximise ``αᵀw − (λ/2)·wᵀΣw`` over the box / budget / turnover set via SLSQP (§3.3).

    Solves the mean-variance QP with ``scipy.optimize.minimize(method='SLSQP')``
    (minimising the negated objective), the cvxpy-free replacement for the
    design-doc ``OptimizationRebalancer``. Constraints, mirroring §3.3:

    * budget ``sum(w) == net_exposure`` (equality);
    * box bounds — long-only ``[min_single_weight, max_single_weight]``, long/short
      ``[-max_single_weight, max_single_weight]``;
    * optional L1 turnover ``‖w − current_w‖₁ ≤ 2·max_turnover_per_period`` when the
      cap is binding (< the no-op value of 2.0), via an auxiliary-variable
      reformulation so SLSQP sees only smooth constraints.

    ``Σ`` comes from :func:`_covariance` (Ledoit-Wolf by default), ``λ`` from
    ``mv_risk_aversion``. NaN-aware: non-finite-alpha symbols are pinned to weight
    ``0`` and dropped from the program. On any solver failure / non-finite result
    the function returns the :func:`equal_weight` fallback (design-doc §3.3,
    "Fallback: equal weight within eligible universe").
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
    a = alpha[idx]
    cur = current_w[idx]
    cov = cov_full[np.ix_(idx, idx)]
    # Ridge the diagonal so the QP is strictly convex even on a rank-deficient cov.
    cov = cov + np.eye(k) * 1e-8
    lam = float(config.mv_risk_aversion)

    if config.long_short:
        lo, hi = -config.max_single_weight, config.max_single_weight
    else:
        lo, hi = config.min_single_weight, config.max_single_weight
    # A feasible box must straddle the per-name share of the net budget; if the
    # caller's box is too tight to sum to net, SLSQP will report failure → fallback.
    bounds = [(lo, hi)] * k

    def neg_obj(w: np.ndarray) -> float:
        return -(a @ w) + 0.5 * lam * float(w @ cov @ w)

    def neg_grad(w: np.ndarray) -> np.ndarray:
        return -a + lam * (cov @ w)

    # Constraint Jacobians are given: without them SLSQP differences each constraint
    # numerically, k evaluations per iteration.
    constraints: list[dict] = [
        {"type": "eq", "fun": lambda w: float(np.sum(w) - config.net_exposure),
         "jac": lambda w: np.ones_like(w)}
    ]
    # Turnover cap as an L1 constraint: ‖w − cur‖₁ ≤ 2·cap (subgradient −sign(w − cur)).
    # SLSQP handles the piecewise-linear L1 acceptably for these small N.
    cap = float(config.max_turnover_per_period)
    if cap < 2.0:  # 2.0 is the no-op ceiling (turnover budget never binds)
        constraints.append(
            {"type": "ineq", "fun": lambda w: 2.0 * cap - float(np.sum(np.abs(w - cur))),
             "jac": lambda w: -np.sign(w - cur)}
        )

    # Warm-start from the current weights projected onto the eligible set.
    x0 = cur.copy()
    s = x0.sum()
    if not np.isfinite(s) or s == 0.0:
        x0 = np.full(k, config.net_exposure / k)
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
        return fallback()
    if not res.success or not np.all(np.isfinite(res.x)):
        return fallback()
    out[idx] = res.x
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
    * ``mv``           → :func:`mean_variance` (SLSQP; equal-weight fallback)
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
