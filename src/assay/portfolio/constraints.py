"""Portfolio constraint application — design-doc section 1.1 (ConstraintApplicator), 2.5.

The ``ConstraintApplicator`` stage takes a raw target weight vector for one
rebalance date and bends it onto the feasible set: per-stock position limits,
holding-count caps, turnover budget, and gross/net exposure targets (design-doc
2.5). Each function is pure — it takes a 1-D weight vector (one entry per symbol,
aligned to the rebalance date's universe) plus the relevant config knobs and
returns a new vector. All are NaN-aware: a NaN weight is treated as ``0`` (a
non-position) so a single missing symbol never poisons the vector.

**Grounding to reality.** ``sector_neutral`` matches sector weights to a
benchmark and is the hook for the section-2.5 sector-neutral constraint; the
DataStore has no sector classification for US equities, so ``sectors`` is ``None``
in practice and the function is a documented no-op. It never fabricates sector
labels. ``market_neutral`` / beta-neutral and ex-ante tracking-error constraints
need an index series / covariance Assay does not have for US universes and are not
applied here.

Vectors are ``float64``; sign convention follows the config — long-only weights
are ``>= 0``, long/short weights are signed. ``apply_constraints`` orchestrates the
individual rules into a single feasible vector, robust to all-zero and empty input.
"""

from __future__ import annotations

import numpy as np

from assay.portfolio.config import PortfolioBacktestConfig

__all__ = [
    "apply_position_limits",
    "cap_stock_count",
    "cap_turnover",
    "scale_exposure",
    "sector_neutral",
    "apply_constraints",
]


def _clean(w: np.ndarray) -> np.ndarray:
    """Coerce to a 1-D ``float64`` vector with non-finite entries -> ``0.0``."""
    arr = np.asarray(w, dtype=np.float64).reshape(-1).copy()
    arr[~np.isfinite(arr)] = 0.0
    return arr


def apply_position_limits(
    w: np.ndarray,
    max_single: float,
    min_single: float = 0.0,
    long_short: bool = False,
) -> np.ndarray:
    """Clip each non-zero position into the allowed per-stock weight band.

    Long-only (``long_short=False``): every weight is clipped to
    ``[0, max_single]``, and any surviving position below ``min_single`` in
    magnitude is dropped to ``0`` (anti-dust). Long/short: magnitudes are capped
    at ``max_single`` (so weights lie in ``[-max_single, +max_single]``), the
    sign is preserved, and sub-``min_single`` magnitudes are zeroed. NaN-safe.
    """
    w = _clean(w)
    max_single = float(max_single)
    min_single = float(min_single)
    if long_short:
        mag = np.minimum(np.abs(w), max_single)
        mag[mag < min_single] = 0.0
        return np.sign(w) * mag
    w = np.clip(w, 0.0, max_single)
    w[w < min_single] = 0.0
    return w


def _fill_to_target(mag: np.ndarray, max_single: float, target: float) -> np.ndarray:
    """Water-fill a non-negative magnitude vector to ``sum == target`` under a
    per-element cap ``max_single``.

    Clip to the cap, then redistribute any deficit proportionally onto the
    elements that still have headroom, repeating until the target is met or every
    element is pinned at the cap (``target > n_active * max_single`` — the closest
    feasible point). Operates in place on a copy.
    """
    mag = np.minimum(np.abs(mag), max_single)
    active = mag > 0.0
    for _ in range(int(active.sum()) + 1):  # bounded: >=1 element pins per pass
        deficit = target - float(mag[active].sum())
        if deficit <= 1e-15:
            break
        head = active & (mag < max_single - 1e-15)  # remaining headroom
        if not np.any(head):
            break  # all pinned at the cap — closest feasible point
        head_sum = float(mag[head].sum())
        if head_sum > 0.0:  # scale headroom elements up proportionally
            mag[head] = np.minimum(mag[head] * (1.0 + deficit / head_sum), max_single)
        else:  # headroom elements are all zero; spread the deficit evenly
            mag[head] = min(deficit / int(head.sum()), max_single)
    return mag


def _enforce_caps_preserving_gross(
    w: np.ndarray, max_single: float, gross: float, net: float, long_short: bool
) -> np.ndarray:
    """Clip magnitudes to ``max_single`` while preserving the exposure target —
    a water-filling projection.

    Naively clipping then rescaling re-inflates capped names above the cap; this
    redistributes the freed weight onto names with headroom instead. Long-only:
    holds ``sum(w) == gross``. Long/short: water-fills the long and short legs
    *independently* to ``(gross + net) / 2`` and ``(gross - net) / 2``, so both
    the gross **and** the net survive the cap. When the target exceeds what the
    caps can hold the result pins as many names as possible at the cap (the
    closest feasible point). Sign is preserved. NaN-safe.
    """
    w = _clean(w)
    max_single = float(max_single)
    if max_single <= 0.0:
        return np.zeros_like(w)
    if not np.any(w != 0.0):
        return w
    sign = np.sign(w)
    mag = np.abs(w)
    if not long_short:
        return sign * _fill_to_target(mag, max_single, float(gross))
    # Long/short: fill each leg to its own target so net is preserved.
    out = np.zeros_like(w)
    longs = w > 0.0
    shorts = w < 0.0
    target_long = (float(gross) + float(net)) / 2.0
    target_short = (float(gross) - float(net)) / 2.0
    if np.any(longs) and target_long > 0.0:
        out[longs] = _fill_to_target(mag[longs], max_single, target_long)
    if np.any(shorts) and target_short > 0.0:
        out[shorts] = -_fill_to_target(mag[shorts], max_single, target_short)
    return out


def cap_stock_count(
    w: np.ndarray, min_count: int = 0, max_count: int | None = None
) -> np.ndarray:
    """Cap the number of non-zero holdings to ``max_count``, keeping top-by-|w|.

    When more than ``max_count`` positions are non-zero, the smallest-magnitude
    positions are zeroed until exactly ``max_count`` remain (ties broken by
    index order, deterministically). ``min_count`` is advisory here — this
    function only *trims*; it cannot conjure positions, so it never adds
    holdings (the weight constructor is responsible for breadth). ``max_count``
    of ``None`` (or ``<= 0``) is a no-op. NaN-safe.
    """
    w = _clean(w)
    if max_count is None or max_count <= 0:
        return w
    nz = np.flatnonzero(w != 0.0)
    if nz.size <= max_count:
        return w
    # Keep the `max_count` largest |w|; stable order so ties resolve by index.
    order = sorted(nz.tolist(), key=lambda i: (-abs(w[i]), i))
    keep = set(order[:max_count])
    out = np.zeros_like(w)
    for i in keep:
        out[i] = w[i]
    return out


def cap_turnover(
    target_w: np.ndarray, current_w: np.ndarray, max_turnover: float
) -> np.ndarray:
    """Scale the trade vector so one-way turnover does not exceed ``max_turnover``.

    One-way turnover is ``sum(|target - current|) / 2``. If the proposed trade
    exceeds the budget, the *entire* trade vector is shrunk uniformly toward the
    current weights by the ratio ``budget / turnover`` — a partial move along the
    straight line to the target, which keeps the trade direction and the relative
    sizing intact. ``max_turnover >= 1`` (or non-positive turnover) is a no-op.
    NaN-safe; mismatched lengths are zero-padded to the longer vector.
    """
    target = _clean(target_w)
    current = _clean(current_w)
    if target.shape != current.shape:  # align by zero-padding the shorter side
        n = max(target.size, current.size)
        t = np.zeros(n)
        c = np.zeros(n)
        t[: target.size] = target
        c[: current.size] = current
        target, current = t, c
    max_turnover = float(max_turnover)
    trade = target - current
    turnover = 0.5 * float(np.abs(trade).sum())
    if max_turnover >= 1.0 or turnover <= max_turnover or turnover == 0.0:
        return target
    scale = max_turnover / turnover
    return current + scale * trade


def scale_exposure(
    w: np.ndarray,
    gross: float = 1.0,
    net: float | None = None,
    long_short: bool = False,
) -> np.ndarray:
    """Rescale weights to hit the target gross (and, for L/S, net) exposure.

    Long-only: divide by ``sum(w)`` so weights sum to ``gross`` (fully invested
    when ``gross == 1``); an all-zero vector is returned unchanged. Long/short:
    scale the long and short legs *independently* so ``sum(|w|) == gross`` and
    ``sum(w) == net`` — i.e. long leg sums to ``(gross + net) / 2`` and short leg
    to ``-(gross - net) / 2``. If only one leg is present the achievable net is
    that leg's signed sum. NaN-safe.
    """
    w = _clean(w)
    gross = float(gross)
    if not long_short:
        tot = float(w.sum())
        if tot == 0.0:
            return w
        return w * (gross / tot)
    net = 0.0 if net is None else float(net)
    longs = np.clip(w, 0.0, None)
    shorts = np.clip(w, None, 0.0)
    long_sum = float(longs.sum())
    short_sum = float(-shorts.sum())  # positive magnitude of the short leg
    target_long = (gross + net) / 2.0
    target_short = (gross - net) / 2.0
    out = np.zeros_like(w)
    if long_sum > 0.0 and target_long > 0.0:
        out += longs * (target_long / long_sum)
    if short_sum > 0.0 and target_short > 0.0:
        out += shorts * (target_short / short_sum)
    return out


def sector_neutral(
    w: np.ndarray,
    sectors: np.ndarray | None,
    benchmark_w: np.ndarray | None = None,
) -> np.ndarray:
    """Match per-sector total weight to the benchmark (design-doc 2.5), NaN-safe.

    For each sector, rescale its member weights so the sector's total equals the
    benchmark's sector total (or an equal split across sectors when
    ``benchmark_w`` is ``None``), preserving within-sector relative weights.

    **No-op when ``sectors`` is ``None``** (returns ``w`` unchanged) — the common
    case for Assay's US-equity data, which carries no sector classification. The
    function never invents sector labels; see the module docstring.
    """
    w = _clean(w)
    if sectors is None:
        return w  # no sector classification available — documented no-op
    labels = np.asarray(sectors)
    if labels.shape[0] != w.shape[0]:
        raise ValueError(
            f"sectors has {labels.shape[0]} labels but the weights have {w.shape[0]} symbols"
        )
    bench = None if benchmark_w is None else _clean(benchmark_w)
    uniq = np.unique(labels)
    out = np.zeros_like(w)
    gross = float(np.abs(w).sum())
    for label in uniq:
        cols = np.flatnonzero(labels == label)
        leg = w[cols]
        leg_sum = float(leg.sum())
        if bench is not None:
            target = float(bench[cols].sum())
        else:  # equal sector weights, preserving total gross
            target = gross / uniq.size
        if leg_sum != 0.0:
            out[cols] = leg * (target / leg_sum)
        elif np.any(leg != 0.0):  # dollar-neutral sector: scale by magnitude
            mag = float(np.abs(leg).sum())
            out[cols] = leg * (target / mag) if mag != 0.0 else leg
    return out


def apply_constraints(
    target_w: np.ndarray,
    current_w: np.ndarray,
    config: PortfolioBacktestConfig,
    sectors: np.ndarray | None = None,
) -> np.ndarray:
    """Orchestrate the constraint stack into one feasible weight vector.

    Order (design-doc 1.1 ConstraintApplicator): (1) sector-neutral (no-op without
    ``sectors``); (2) drop dust / sign-fix via position limits with the cap *open*;
    (3) holding-count cap (top-by-|w|); (4) scale to the gross/net exposure target;
    (5) enforce the per-stock cap while preserving gross (water-filling, so capping
    does not re-inflate other names past the cap); (6) turnover cap, shrinking the
    trade back toward ``current_w`` if it breaches ``max_turnover_per_period``.

    The cap is applied *after* exposure scaling (the binding order — limit then
    re-scale would violate the cap) so the returned vector satisfies both
    ``max_single_weight`` and the gross target whenever the two are jointly
    feasible (``gross <= n_held * max_single``). Robust to all-zero / empty / NaN
    inputs: a zero or empty target returns a zero vector of the right length.
    """
    target = _clean(target_w)
    if target.size == 0 or not np.any(target != 0.0):
        return target  # nothing to allocate — feasible by construction

    w = sector_neutral(target, sectors, benchmark_w=None)
    # Drop dust and fix the sign domain (long-only -> non-negative) with the cap
    # left open; the binding per-stock cap is enforced after exposure scaling.
    w = apply_position_limits(
        w,
        max_single=np.inf,
        min_single=config.min_single_weight,
        long_short=config.long_short,
    )
    w = cap_stock_count(w, config.min_stock_count, config.max_stock_count)
    w = scale_exposure(
        w,
        gross=config.gross_exposure,
        net=config.net_exposure,
        long_short=config.long_short,
    )
    # Enforce the per-stock cap while holding the gross fixed (water-filling).
    w = _enforce_caps_preserving_gross(
        w,
        max_single=config.max_single_weight,
        gross=config.gross_exposure,
        net=config.net_exposure,
        long_short=config.long_short,
    )
    w = cap_turnover(w, current_w, config.max_turnover_per_period)
    return w
