"""The :class:`PortfolioBacktestConfig` contract — design-doc section 2.

A single config object parameterises an entire portfolio backtest: universe and
market (2.1), evaluation period (2.2), rebalance schedule (2.3), weight method
(2.4), constraints (2.5), A-share regulatory rules (2.6), execution model (2.7),
benchmark/attribution (2.8) and output (2.9). Every field below carries the
spec's default and valid range; :meth:`__post_init__` enforces those ranges and
raises :class:`ValueError` on violation (lenient where the spec permits ``None``).

**Grounding to reality.** The only data Assay actually has is US equities
(NASDAQ-100 OHLCV + transactions). So ``market`` defaults to ``'US'``. The full
A-share field set (section 2.6) is kept verbatim for forward-compatibility, but
those fields are *inert without A-share inputs the DataStore does not provide*:
``st_filter``, ``suspend_handling``, ``rebalance_around_index``,
``inclusion_anticipation``, ``northbound_flow_filter`` and ``sz_sh_connect_only``
all depend on ST/suspended/index-reconstitution/northbound/Connect data that does
not exist for US equities; the rebalancer/execution layers treat them as no-ops
and document that. The *price-mechanical* A-share rules — ``t_plus_1`` settlement,
``price_limit_pct``/``enforce_limit_price`` (computable from OHLC), and the
stamp-duty/commission/transfer-fee cost model — activate whenever their inputs
are present. :meth:`preset` applies the section-6 market table (US/A/HK) cost and
limit defaults.

Serialisation (:meth:`to_dict`/:meth:`from_dict`) round-trips JSON-safely;
:meth:`config_hash` is the stable SHA-256[:12] over the sorted field dict that the
:class:`~assay.portfolio.report.PortfolioReport` folds into its ``run_id``.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field, fields
from typing import Any

# Frozen config-identity allowlist (minute-backtesting design §10). These are the
# fields that existed when the daily ``config_hash``/``run_id``/library-key
# contract was frozen; :meth:`PortfolioBacktestConfig.config_hash` hashes ONLY
# these so additive fields (future intraday knobs, serialization metadata) can
# never change an existing daily hash. NEVER auto-derive this from ``fields()`` —
# that would defeat the freeze. Adding a field here is a deliberate, breaking act.
_HASH_FIELDS_V1: frozenset[str] = frozenset({
    "adv_window", "as_of_date", "attribution_factors", "attribution_model",
    "benchmark", "benchmark_symbol", "benchmark_tracking_err", "bl_tau",
    "bootstrap_sharpe", "capacity_adv_limit", "commission_min", "commission_rate",
    "compute_attribution", "cov_method", "cov_window", "custom_symbols",
    "enforce_limit_price", "execution_offset_days", "execution_price",
    "gross_exposure", "include_bid_ask", "include_delisted", "inclusion_anticipation",
    "ipo_lockout_days", "long_short", "market", "market_neutral", "max_adv_fraction",
    "max_annual_turnover", "max_sector_weight", "max_single_weight", "max_stock_count",
    "max_turnover_per_period", "min_rebalance_interval", "min_single_weight",
    "min_stock_count", "mv_risk_aversion", "n_bootstrap", "net_exposure",
    "new_listing_lockout_days", "northbound_flow_filter", "oos_split_date",
    "output_frequency", "partial_fill_handling", "period_end", "period_start",
    "price_limit_pct", "quintile_long_n", "quintile_short_n", "rebalance_around_index",
    "rebalance_day", "rebalance_type", "risk_free_rate", "save_position_log",
    "save_trade_log", "sector_neutral", "signal_autocorr_floor", "signal_transform",
    "slippage_k", "slippage_model", "st_filter", "stamp_duty_rate", "star_chinext_limit",
    "suspend_handling", "sz_sh_connect_only", "t_plus_1", "threshold_rank_shift",
    "threshold_weight_drift", "transfer_fee_rate", "universe", "warmup_days",
    "weight_method",
})

# Enumerated valid-value sets (design-doc section 2). Kept module-level so the
# validator and any UI/CLI can introspect them.
_UNIVERSES = {"CSI300", "CSI500", "CSI1000", "CSI800", "SP500", "NASDAQ100", "HSI", "CUSTOM"}
_MARKETS = {"A", "US", "HK"}
_REBALANCE_TYPES = {"daily", "weekly", "monthly", "quarterly", "threshold", "signal"}
_WEIGHT_METHODS = {"equal", "signal_prop", "mv", "risk_parity", "quintile", "decile", "bl"}
_SIGNAL_TRANSFORMS = {"rank", "zscore", "raw"}
_COV_METHODS = {"sample", "ledoit_wolf", "factor_model"}
_SUSPEND_HANDLING = {"skip", "close_prev"}
_EXECUTION_PRICES = {"next_open", "next_close", "vwap", "arrival"}
_SLIPPAGE_MODELS = {"sqrt", "linear", "zero", "almgren_chriss"}
_PARTIAL_FILL = {"defer", "cancel", "force"}
_BENCHMARKS = {"index", "cash", "custom", "none"}
_ATTRIBUTION_MODELS = {"brinson", "factor", "simple"}
_OUTPUT_FREQUENCIES = {"daily", "weekly", "monthly"}


@dataclass
class PortfolioBacktestConfig:
    """Every parameter of one portfolio backtest (design-doc section 2).

    Fields are grouped by the spec's subsections; ``A`` in the spec marks A-share
    rules (section 2.6) that only bind when ``market == 'A'`` *and* the required
    data is present (see module docstring). Construct directly with explicit
    values, or via :meth:`preset` for the section-6 market cost/limit defaults.
    """

    # === 2.2  Evaluation period (required — placed first) ====================
    period_start: str = ""  # YYYY-MM-DD; first signal-generation date (required)
    period_end: str = ""    # YYYY-MM-DD; inclusive backtest end (required)
    oos_split_date: str | None = None  # before = in-sample, after = OOS; null disables
    warmup_days: int = 0    # 0–252 trading days to warm caches (excluded from returns)

    # === 2.1  Universe & market =============================================
    universe: str = "NASDAQ100"  # spec default CSI300; grounded to the only data we have
    market: str = "US"           # A, US, HK — sets calendar/cost model/settlement
    custom_symbols: list[str] | None = None  # used when universe == 'CUSTOM'
    as_of_date: str | None = None  # PIT: only use data known by this date (default period_end)
    include_delisted: bool = True  # survivorship-bias control

    # === 2.3  Rebalance parameters ==========================================
    rebalance_type: str = "monthly"  # daily|weekly|monthly|quarterly|threshold|signal
    rebalance_day: str = "last"      # first|last|Wed (weekday) — calendar types
    threshold_rank_shift: int = 2    # 1–5 deciles; threshold type
    threshold_weight_drift: float = 0.02  # 0.005–0.20; threshold type
    signal_autocorr_floor: float = 0.70   # 0.40–0.95; signal type
    min_rebalance_interval: int = 5  # 1–63 trading days between rebalances
    execution_offset_days: int = 1   # 1–3; signal→execution lag (1 = T+1 open; <1 is look-ahead)

    # === 2.4  Weight construction ===========================================
    weight_method: str = "signal_prop"  # equal|signal_prop|mv|risk_parity|quintile|decile|bl
    long_short: bool = False    # true: long top half / short bottom half; false: long-only
    gross_exposure: float = 1.0  # 0.5–2.0; total absolute weight sum
    net_exposure: float = 1.0    # -1.0–1.0; net weight sum (0.0 = dollar-neutral)
    signal_transform: str = "rank"  # rank|zscore|raw
    quintile_long_n: int = 1     # 1–3; top N quintiles long
    quintile_short_n: int = 1    # 1–3; bottom N quintiles short
    mv_risk_aversion: float = 1.0  # 0.1–10.0; mean-variance lambda
    cov_window: int = 252        # 60–504 trading days for covariance estimation
    cov_method: str = "ledoit_wolf"  # sample|ledoit_wolf|factor_model
    bl_tau: float = 0.05         # 0.01–0.5; Black-Litterman prior uncertainty

    # === 2.5  Portfolio constraints =========================================
    max_single_weight: float = 0.05  # 0.01–0.30; max weight per stock
    min_single_weight: float = 0.0   # 0.0–0.01; min non-zero weight (anti-dust)
    max_sector_weight: float = 0.30  # 0.10–1.0; max weight per sector (needs sector data)
    sector_neutral: bool = False     # match benchmark sector weights (needs sector data)
    market_neutral: bool = False     # target beta 0 via short index futures
    max_turnover_per_period: float = 1.0  # 0.10–2.0; max two-way turnover per rebalance
    max_annual_turnover: float | None = None  # 0.5–10.0; annualised cap; null = no cap
    min_stock_count: int = 10        # 5–500; minimum holdings
    max_stock_count: int | None = None  # 10–500; null = no cap
    benchmark_tracking_err: float | None = None  # 0.01–0.20; max ex-ante TE; null = off
    capacity_adv_limit: float = 0.10  # 0.01–0.50; max order as fraction of 20-day ADV

    # === 2.6  A-share specific constraints (bind only when market == 'A') ====
    # Price-mechanical rules (activate when their OHLC inputs are present):
    t_plus_1: bool = True            # T+1 settlement: same-day-bought shares not sellable
    price_limit_pct: float | None = 0.10  # daily limit; 0.10 main, 0.20 STAR, null disables
    star_chinext_limit: float | None = 0.20  # STAR/ChiNext limit; null disables
    enforce_limit_price: bool = True  # block buys at limit-up / sells at limit-down
    # Cost model (always computable):
    stamp_duty_rate: float = 0.001   # 0.0–0.003; sell side only (印花税)
    commission_rate: float = 0.0003  # 0.0001–0.001; both sides (佣金)
    commission_min: float = 5.0      # 0.0–10.0; min commission per trade (CNY)
    transfer_fee_rate: float = 0.00002  # 0.00001–0.0001; both sides, SSE only (过户费)
    # Data-dependent filters (INERT without A-share data — no-op gracefully):
    st_filter: bool = True           # exclude ST/*ST stocks (needs ST flags)
    new_listing_lockout_days: int = 60  # 0–252; exclude recent IPOs (needs listing dates)
    ipo_lockout_days: int = 60       # alias of new_listing_lockout_days
    suspend_handling: str = "skip"   # skip|close_prev (needs suspension flags)
    rebalance_around_index: bool = True  # avoid rebal near index recon (needs recon dates)
    inclusion_anticipation: int = 0  # 0–5; pre-buy days before index add (needs recon dates)
    northbound_flow_filter: bool = False  # exclude high northbound concentration (needs flow)
    sz_sh_connect_only: bool = False  # restrict to Stock-Connect names (needs Connect list)

    # === 2.7  Execution model ===============================================
    execution_price: str = "next_open"  # next_open|next_close|vwap|arrival
    slippage_model: str = "sqrt"     # sqrt|linear|zero|almgren_chriss
    slippage_k: float = 0.20         # 0.05–0.50; impact coefficient (see market table)
    adv_window: int = 20             # 5–60 trading days for ADV
    max_adv_fraction: float = 0.10   # 0.01–0.50; max order as fraction of ADV
    partial_fill_handling: str = "defer"  # defer|cancel|force
    include_bid_ask: bool = False    # add half-spread to cost (needs bid-ask data; inert)

    # === 2.8  Benchmark and attribution =====================================
    benchmark: str = "index"         # index|cash|custom|none
    benchmark_symbol: str | None = None  # used when benchmark == 'custom'
    risk_free_rate: float = 0.015    # 0.0–0.05; annual R_f for Sharpe
    attribution_model: str = "brinson"  # brinson|factor|simple
    attribution_factors: list[str] = field(default_factory=list)  # extra factor_ids

    # === 2.9  Output configuration ==========================================
    output_frequency: str = "daily"  # daily|weekly|monthly NAV/position snapshots
    save_trade_log: bool = True      # store every trade in the report
    save_position_log: bool = False  # store daily position snapshots (large)
    compute_attribution: bool = False  # compute return attribution (costs time)
    bootstrap_sharpe: bool = False   # bootstrap CI for Sharpe
    n_bootstrap: int = 1000          # bootstrap samples for Sharpe CI

    # -- validation --------------------------------------------------------
    def __post_init__(self) -> None:
        """Enforce the section-2 valid ranges; raise ``ValueError`` on violation.

        Lenient on ``None``-valued optionals (the spec permits null for
        ``max_annual_turnover``, ``max_stock_count``, ``benchmark_tracking_err``,
        ``price_limit_pct``, ``star_chinext_limit``, ``oos_split_date``,
        ``as_of_date``, ``benchmark_symbol``, ``custom_symbols``). ``ipo_lockout_days``
        is an alias of ``new_listing_lockout_days``; the two are reconciled here.
        """
        # ipo_lockout_days is documented as an alias — keep them consistent (the
        # non-default one wins; both default => no-op).
        if self.ipo_lockout_days != 60 and self.new_listing_lockout_days == 60:
            self.new_listing_lockout_days = self.ipo_lockout_days
        else:
            self.ipo_lockout_days = self.new_listing_lockout_days

        def _enum(name: str, val: str, allowed: set[str]) -> None:
            if val not in allowed:
                raise ValueError(
                    f"{name}={val!r} invalid; must be one of {sorted(allowed)}"
                )

        def _rng(name: str, val: float, lo: float, hi: float) -> None:
            if val is None:
                return
            if not (lo <= val <= hi):
                raise ValueError(f"{name}={val!r} out of range [{lo}, {hi}]")

        # 2.1 universe & market
        _enum("universe", self.universe, _UNIVERSES)
        _enum("market", self.market, _MARKETS)
        if self.universe == "CUSTOM" and not self.custom_symbols:
            raise ValueError("universe='CUSTOM' requires non-empty custom_symbols")
        # A-share is long-only: 融券 (securities lending) is restricted to a small
        # eligible list, costly, and often unavailable — a short A-share book is
        # not realistically executable, so the engine refuses it outright (§2.6).
        if self.market == "A" and self.long_short:
            raise ValueError(
                "market='A' is long-only: A-share short selling (融券) is highly "
                "restricted and not modelled — set long_short=False."
            )

        # 2.2 period
        if not self.period_start or not self.period_end:
            raise ValueError("period_start and period_end are required (YYYY-MM-DD)")
        if self.period_start > self.period_end:
            raise ValueError(
                f"period_start {self.period_start!r} must be <= period_end {self.period_end!r}"
            )
        _rng("warmup_days", self.warmup_days, 0, 252)

        # 2.3 rebalance
        _enum("rebalance_type", self.rebalance_type, _REBALANCE_TYPES)
        _rng("threshold_rank_shift", self.threshold_rank_shift, 1, 5)
        _rng("threshold_weight_drift", self.threshold_weight_drift, 0.005, 0.20)
        _rng("signal_autocorr_floor", self.signal_autocorr_floor, 0.40, 0.95)
        _rng("min_rebalance_interval", self.min_rebalance_interval, 1, 63)
        if not (1 <= self.execution_offset_days <= 3):
            raise ValueError(
                f"execution_offset_days={self.execution_offset_days} out of [1, 3] "
                "(0 executes at the signal price — look-ahead bias)"
            )

        # 2.4 weights
        _enum("weight_method", self.weight_method, _WEIGHT_METHODS)
        _rng("gross_exposure", self.gross_exposure, 0.5, 2.0)
        _rng("net_exposure", self.net_exposure, -1.0, 1.0)
        _enum("signal_transform", self.signal_transform, _SIGNAL_TRANSFORMS)
        _rng("quintile_long_n", self.quintile_long_n, 1, 3)
        _rng("quintile_short_n", self.quintile_short_n, 1, 3)
        _rng("mv_risk_aversion", self.mv_risk_aversion, 0.1, 10.0)
        _rng("cov_window", self.cov_window, 60, 504)
        _enum("cov_method", self.cov_method, _COV_METHODS)
        _rng("bl_tau", self.bl_tau, 0.01, 0.5)

        # 2.5 constraints
        _rng("max_single_weight", self.max_single_weight, 0.01, 0.30)
        _rng("min_single_weight", self.min_single_weight, 0.0, 0.01)
        _rng("max_sector_weight", self.max_sector_weight, 0.10, 1.0)
        _rng("max_turnover_per_period", self.max_turnover_per_period, 0.10, 2.0)
        _rng("max_annual_turnover", self.max_annual_turnover, 0.5, 10.0)
        _rng("min_stock_count", self.min_stock_count, 5, 500)
        _rng("max_stock_count", self.max_stock_count, 10, 500)
        _rng("benchmark_tracking_err", self.benchmark_tracking_err, 0.01, 0.20)
        _rng("capacity_adv_limit", self.capacity_adv_limit, 0.01, 0.50)
        if self.min_single_weight > self.max_single_weight:
            raise ValueError("min_single_weight must be <= max_single_weight")
        if self.max_stock_count is not None and self.max_stock_count < self.min_stock_count:
            raise ValueError("max_stock_count must be >= min_stock_count")

        # 2.6 A-share
        if self.price_limit_pct is not None and self.price_limit_pct not in (0.10, 0.20):
            raise ValueError("price_limit_pct must be 0.10, 0.20, or None")
        if self.star_chinext_limit is not None and self.star_chinext_limit != 0.20:
            raise ValueError("star_chinext_limit must be 0.20 or None")
        _rng("new_listing_lockout_days", self.new_listing_lockout_days, 0, 252)
        _rng("ipo_lockout_days", self.ipo_lockout_days, 0, 252)
        _rng("stamp_duty_rate", self.stamp_duty_rate, 0.0, 0.003)
        _rng("commission_rate", self.commission_rate, 0.0001, 0.001)
        _rng("commission_min", self.commission_min, 0.0, 10.0)
        _rng("transfer_fee_rate", self.transfer_fee_rate, 0.00001, 0.0001)
        _enum("suspend_handling", self.suspend_handling, _SUSPEND_HANDLING)
        _rng("inclusion_anticipation", self.inclusion_anticipation, 0, 5)

        # 2.7 execution
        _enum("execution_price", self.execution_price, _EXECUTION_PRICES)
        _enum("slippage_model", self.slippage_model, _SLIPPAGE_MODELS)
        _rng("slippage_k", self.slippage_k, 0.05, 0.50)
        _rng("adv_window", self.adv_window, 5, 60)
        _rng("max_adv_fraction", self.max_adv_fraction, 0.01, 0.50)
        _enum("partial_fill_handling", self.partial_fill_handling, _PARTIAL_FILL)

        # 2.8 benchmark
        _enum("benchmark", self.benchmark, _BENCHMARKS)
        if self.benchmark == "custom" and not self.benchmark_symbol:
            raise ValueError("benchmark='custom' requires benchmark_symbol")
        _rng("risk_free_rate", self.risk_free_rate, 0.0, 0.05)
        _enum("attribution_model", self.attribution_model, _ATTRIBUTION_MODELS)

        # 2.9 output
        _enum("output_frequency", self.output_frequency, _OUTPUT_FREQUENCIES)
        if self.n_bootstrap < 1:
            raise ValueError("n_bootstrap must be >= 1")

    # -- presets -----------------------------------------------------------
    @classmethod
    def preset(cls, market: str, **overrides: Any) -> "PortfolioBacktestConfig":
        """Build a config with the section-6 market table cost/limit defaults.

        ``'US'`` — no price limit, no stamp duty, no transfer fee, commission
        ~0.0005 both sides, impact ``k=0.10``, GICS sectors, T+2 (no T+1 lock).
        ``'A'`` — total-return basis, T+1, ±10% price limit, 0.0005 stamp duty
        (sell), 0.0003 commission (min CNY 5), 0.00002 transfer fee, ``k=0.20``.
        ``'HK'`` — no price limit, 0.0013 stamp duty *both* sides, no transfer fee,
        commission ~0.0004, ``k=0.15``, T+2.

        ``period_start``/``period_end`` have no sensible market default, so when
        not supplied via ``overrides`` the preset fills harmless placeholders
        (``'1970-01-01'``..``'1970-01-02'``) to yield a valid object — real
        callers always override them. Extra keyword overrides apply on top of the
        preset.
        """
        m = market.upper()
        if m == "US":
            base: dict[str, Any] = dict(
                market="US",
                universe="NASDAQ100",
                t_plus_1=False,
                price_limit_pct=None,
                star_chinext_limit=None,
                enforce_limit_price=False,
                stamp_duty_rate=0.0,
                commission_rate=0.0005,
                commission_min=0.0,
                transfer_fee_rate=0.00001,  # floor of valid range; effectively nil
                st_filter=False,
                new_listing_lockout_days=0,
                rebalance_around_index=False,
                slippage_k=0.10,
                risk_free_rate=0.015,
            )
        elif m == "A":
            base = dict(
                market="A",
                universe="CSI300",
                t_plus_1=True,
                price_limit_pct=0.10,
                star_chinext_limit=0.20,
                enforce_limit_price=True,
                stamp_duty_rate=0.0005,  # 印花税 0.05% sell-side (cut from 0.1% on 2023-08-28)
                commission_rate=0.0003,
                commission_min=5.0,
                transfer_fee_rate=0.00002,
                st_filter=True,
                new_listing_lockout_days=60,
                rebalance_around_index=True,
                slippage_k=0.20,
                risk_free_rate=0.015,
            )
        elif m == "HK":
            base = dict(
                market="HK",
                universe="HSI",
                t_plus_1=False,
                price_limit_pct=None,
                star_chinext_limit=None,
                enforce_limit_price=False,
                stamp_duty_rate=0.0013,  # both sides — see note below
                commission_rate=0.0004,
                commission_min=0.0,
                transfer_fee_rate=0.00003,
                st_filter=False,
                new_listing_lockout_days=0,
                rebalance_around_index=False,
                slippage_k=0.15,
                risk_free_rate=0.015,
            )
        else:
            raise ValueError(f"unknown market {market!r}; expected one of US, A, HK")
        # Period has no market default; placeholder keeps preset() construction valid.
        base.setdefault("period_start", "1970-01-01")
        base.setdefault("period_end", "1970-01-02")
        base.update(overrides)
        return cls(**base)

    # -- hashing -----------------------------------------------------------
    def config_hash(self) -> str:
        """Stable SHA-256[:12] over the V1 field allowlist (config identity).

        Drives :meth:`PortfolioReport.compute_run_id`. Computed from the JSON-safe
        dict with sorted keys so equal configs hash identically regardless of field
        declaration order.

        The preimage is restricted to :data:`_HASH_FIELDS_V1` (every field that
        existed when the daily contract was frozen) so that **additive** fields —
        future intraday knobs, serialization metadata — can never perturb an
        existing daily hash / run_id / library key. Today every field is in V1, so
        this is byte-identical to hashing the full dict; the minute-backtesting
        milestone that adds intraday fields will hash the full dict only for
        intraday configs (see docs/design/minute-backtesting.md §10).
        """
        d = self.to_dict()
        preimage = {k: v for k, v in d.items() if k in _HASH_FIELDS_V1}
        payload = json.dumps(preimage, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]

    # -- serialisation -----------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        """JSON-safe dict of every field (lists copied, None preserved)."""
        d = asdict(self)
        # asdict already deep-copies lists/dicts; ensure JSON-roundtrippable types.
        return {k: (list(v) if isinstance(v, tuple) else v) for k, v in d.items()}

    def to_json(self, **kwargs: Any) -> str:
        return json.dumps(self.to_dict(), **kwargs)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "PortfolioBacktestConfig":
        """Rebuild a config from a :meth:`to_dict` payload (ignores unknown keys)."""
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})
