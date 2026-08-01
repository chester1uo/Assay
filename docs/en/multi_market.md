# Multi-market support (China A-share + Hong Kong)

Assay's data layer and portfolio backtester are market-agnostic: prices,
corporate actions and index membership live in canonical stores partitioned by
`market=...`, and the execution simulator already models the A-share
microstructure rules. This doc covers running the framework on **Chinese
A-shares** end-to-end, using the Tushare mirror (see [tushare.md](tushare.md)).

## 1. Ingest: raw Tushare → canonical CN stores

`assay.data.tushare.ingest.prepare_cn` reads the raw mirror
(`$TUSHARE_DATA_DIR`, default `/data/tushare_data`) and writes the same canonical
stores the US pipeline produces, under `market=CN`:

| store | source | notes |
|-------|--------|-------|
| `price_raw` | `cn/daily` | unadjusted OHLCV (`volume` in 手/lots) |
| `adj_events` | `cn/dividend` | 送转 → forward `split_ratio = 1 + stk_div`; pre-tax cash (`cash_div_tax`) → `dividend_cash`; only *implemented* (实施) dividends |
| `universe_snapshots` | `cn/index_weight` | CSI300/500/1000 point-in-time membership (composition-change snapshots) |
| `trade_status` | `cn/stk_limit` + `cn/daily` | raw 涨停价/跌停价 bands + locked flags (A-share only) |
| `security_groups` | `cn/meta/stock_basic` | per-symbol industry label for sector-neutralisation (current snapshot) |

```bash
# writes market=CN partitions under $ASSAY_DATA_DIR
python scripts/prepare_cn.py 2010-01-01 2026-06-27
```

Adjustment is recomputed from primitive events at read time (the same forward
machinery as US equities), so `adj="split"` adjusts for 送转 only and
`adj="total"` adds cash dividends.

## 2. Trading constraints in the portfolio backtest

The execution layer (`assay.portfolio.execution.ExecutionSimulator`) enforces the
A-share rules whenever `PortfolioBacktestConfig.market == "A"` and the data is
present. All three are now wired from the canonical stores:

- **Daily price limits (涨跌停).** A buy into a name trading at its ceiling and a
  sell into one at its floor get **zero fill** (`blocked_reason` `limit_up` /
  `limit_down`). The backtester auto-loads the **real per-board bands** from
  `trade_status` (10% main board, 20% STAR/ChiNext, 30% BSE, 5% ST) rather than a
  flat percentage. Limit prices are raw while the price panel is adjusted, so each
  band is rebased into the panel basis by `adj_close / raw_close` before the
  comparison — see `PortfolioBacktester._cn_limit_matrices`.
- **T+1 settlement.** Shares bought on a rebalance cannot be sold the same step
  (`blocked_reason` `t_plus_1`). Active for `market == "A"` via the preset.
- **Suspension (停牌).** A halted name simply has no `price_raw` bar, so its price
  is NaN and the executor treats it as untradable (`blocked_reason` `suspended`).
  No extra data needed.

The `A`-preset also applies the A-share **cost model**: **0.05% stamp duty**
(sell; cut from 0.1% on 2023-08-28), 0.03% commission (min ¥5), 0.002% transfer
fee, √-impact `k=0.20`.

Other market-correctness defaults (preset `'A'`):

- **Long-only enforced.** `market=='A'` with `long_short=True` raises — A-share
  短 selling (融券) is restricted/unavailable, so a short book isn't executable.
- **Total-return basis.** The engine's adjustment basis is derived from `market`
  (A/HK → `total` = dividends reinvested, the correct basis for alpha P&L; US →
  `split`), so it never silently aliases two runs in the evaluation cache.
- **Sector-neutralisation** uses real industry labels: set `sector_neutral=True`
  and the backtester auto-loads `security_groups` from the store (Tushare
  `industry`); both the signal neutraliser and the sector-weight constraint then
  engage. Without it, runs are never silently sector-balanced.

## 3. Running a CN backtest

```python
import datetime as dt
from assay.config import AssayConfig
from assay.data.store.datastore import DataStore
from assay.portfolio import PortfolioBacktestConfig, PortfolioBacktester

# A CN DataStore reads market=CN partitions; the 'A' preset drives trading rules.
store = DataStore(AssayConfig(market="CN", data_dir=AssayConfig.from_env().data_dir))
bt = PortfolioBacktester(store=store)

cfg = PortfolioBacktestConfig.preset(
    "A", universe="CSI300",
    period_start="2022-01-01", period_end="2023-12-31",
    rebalance_type="monthly", weight_method="signal_prop",
)
report = bt.run("ts_delta(close, 20) / delay(close, 20)", cfg)   # a momentum factor
print(report.total_return, report.sharpe, report.a_share_metrics)
```

Two market knobs, intentionally separate:

- `AssayConfig.market="CN"` selects which **data partition** the store reads.
- `PortfolioBacktestConfig.market="A"` selects the **trading-rule regime**
  (limits / T+1 / cost model). `report.a_share_metrics` reports `n_limit_hits`,
  `n_forced_holds` (T+1), `n_blocked_suspended`, and the corresponding rates.

Universes available for CN: `CSI300`, `CSI500`, `CSI1000` (point-in-time,
survivorship-free).

## 4. Hong Kong

HK rides the same canonical stores under `market=HK`, fed by
`assay.data.tushare.ingest.prepare_hk` (CLI: `scripts/prepare_hk.py`). It needs
an entitled Tushare endpoint — see [tushare.md](tushare.md).

| store | source | notes |
|-------|--------|-------|
| `price_raw` | `hk/daily` | unadjusted OHLCV (`volume` in **shares**, not lots) |
| `adj_events` | `hk/adj_factor` | reconstructed from `cum_adjfactor` steps, corroborated against `pre_close` |
| `universe_snapshots` | `hk/daily` turnover + `hk_basic` ISIN | **HK100** and **HSHARE100** point-in-time membership |
| `trade_status` | — | not written: HKEX has no daily price-limit bands |
| `security_groups` | — | not written: Tushare exposes no HK industry classification |

```bash
# writes market=HK partitions under $ASSAY_DATA_DIR_HK (default ./data_hk)
python scripts/prepare_hk.py 2015-01-01 2026-07-27
```

**HK100** is the whole-board universe. It ranks every name that actually traded
by trailing three-month turnover at each month end and takes the top 100 effective
on the *next* month's first trading day — so a snapshot can never see the returns
it would be scored on.

**HSHARE100** applies exactly the same ranking to **H-shares** only —
mainland-China-incorporated issuers listed on HKEX, as opposed to the red chips
(Cayman/Bermuda holding companies) and local Hong Kong issuers that make up the
rest of the board. Membership is decided by the ISIN country prefix (`CNE`),
which is a property of where the issuer is registered and therefore never changes;
because the listings table is downloaded with delisted names included, the pool is
survivorship-free. The two universes overlap by roughly half — H-shares that are
also among the most-traded names on the board.

Use `HSHARE100` when the thesis is about mainland corporates (A/H pairs, mainland
policy exposure, the Connect flow) and `HK100` when it is about the Hong Kong
market as a whole. `HSI` and `HSTECH` are also registered but are vendored
**current** snapshots and therefore survivorship-biased.

The `HK` preset differs from `A` in every respect that matters:

- **No price limits, no T+1.** HKEX has neither, so `price_limit_pct=None` and
  `t_plus_1=False`; the limit and settlement gates simply do not engage.
- **Shorting allowed.** Unlike `A`, `long_short=True` is accepted.
- **Stamp duty on both sides.** HKEX levies 0.10% on buys as well as sells, so
  `TransactionCostModel` charges it symmetrically when `market == "HK"` — the
  A-share 印花税 stays sell-side only. Per side the preset totals 0.1484%:
  brokerage 0.04% + stamp duty 0.10% + HKEX trading fee 0.00565% + SFC levy
  0.0027% + FRC levy 0.00015%.
- **Total-return basis**, same as `A`, and now genuinely backed by ingested
  `adj_events`.

```python
store = DataStore(AssayConfig(market="HK", data_dir=Path(os.environ["ASSAY_DATA_DIR_HK"])))
cfg = PortfolioBacktestConfig.preset(
    "HK", universe="HK100",       # or "HSHARE100" for H-shares only
    period_start="2022-01-01", period_end="2023-12-31",
)
```

Two caveats specific to HK: **sector-neutralisation is unavailable** (no group
labels), and NAV is denominated in **HKD** with no FX book, so an HK run is not
directly comparable to a US or CN one.
