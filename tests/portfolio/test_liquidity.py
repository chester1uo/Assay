"""Trailing liquidity must be causal and orders must use current monetary NAV."""
import numpy as np
import polars as pl
import pytest
from datetime import date

from assay.portfolio.accounting import PortfolioAccountant
from assay.portfolio.config import PortfolioBacktestConfig
from assay.portfolio.liquidity import trailing_notional


class RawStore:
    def __init__(self, frame): self.frame = frame
    def get_panel(self, fields, symbols, start, end, as_of, adj):
        assert fields == ['close', 'volume'] and adj == 'none'
        return self.frame.filter(pl.col('date').is_between(pl.lit(start).str.to_date(), pl.lit(end).str.to_date()))


def test_trailing_notional_uses_raw_prior_days_and_blocks_incomplete_windows():
    from assay.data.calendar import trading_days
    days = np.array(trading_days(date(2024,1,2),date(2024,1,16)),dtype='datetime64[D]')
    frame = pl.DataFrame({'date':pl.Series(days).cast(pl.Date), 'symbol':['A']*len(days),
                         'close':[10.]*len(days), 'volume':[100.]*5+[1_000_000.]*(len(days)-5)})
    out = trailing_notional(RawStore(frame),days,['A'],as_of=str(days[-1]),window=5,volume_multiplier=100)
    assert np.isnan(out[:5]).all()
    assert out[5,0] == 100000.  # current-day volume spike is excluded
    assert out[6,0] == 200_080_000.
    broken = frame.with_columns(pl.when(pl.col('date') == pl.date(2024,1,3)).then(None)
                                .otherwise(pl.col('volume')).alias('volume'))
    assert np.isnan(trailing_notional(RawStore(broken),days,['A'],as_of=str(days[-1]),window=5,volume_multiplier=100)[5,0])
    missing_day = frame.filter(pl.col('date') != pl.date(2024,1,3))
    assert np.isnan(trailing_notional(RawStore(missing_day),days,['A'],as_of=str(days[-1]),window=5,volume_multiplier=100)[5,0])
    duplicated = pl.concat([frame, frame.slice(1, 1)])
    with pytest.raises(ValueError, match='duplicate liquidity observations'):
        trailing_notional(RawStore(duplicated.reverse()),days,['A'],as_of=str(days[-1]),window=5,volume_multiplier=100)
    with pytest.raises(ValueError, match='unique, and sorted'):
        trailing_notional(RawStore(frame),days,['B','A'],as_of=str(days[-1]),window=5,volume_multiplier=100)


def test_actual_order_size_controls_capacity_impact_and_missing_data():
    cfg=PortfolioBacktestConfig.preset('US', universe='NASDAQ100', period_start='2024-01-01',period_end='2024-01-03',
        initial_capital=1000, liquidity_model='trailing_notional_v1', liquidity_volume_multiplier=1,
        commission_rate=0, commission_min=0, transfer_fee_rate=0, slippage_model='linear', slippage_k=.1, max_adv_fraction=.1,
        partial_fill_handling='cancel')
    ledger=PortfolioAccountant(cfg)
    target=np.array([.5])
    output=ledger.run(np.zeros((3,1)), {0:target,1:target}, adv_notional=np.array([[np.nan],[1000.],[1000.]]))
    assert output.weights[0,0] == 0
    assert output.diag['blocked_liquidity_data'] == 1
    assert output.trades[0].blocked_reason == 'liquidity_data_missing'
    assert output.trades[1].qty_frac == pytest.approx(.1)  # 10% of 1000 turnover / 1000 NAV
    assert output.trades[1].cost == pytest.approx(.001)  # 0.1 traded * (0.1 slope * 0.1 participation)
    assert output.trades[1].participation == pytest.approx(.1)
    assert output.nav_series[1] == pytest.approx(.999)


def test_historical_calendar_covers_2003_and_rejects_unsupported_cn_future():
    days = np.array(['2003-09-08', '2003-09-09', '2003-09-10'], dtype='datetime64[D]')
    frame = pl.DataFrame({'date': pl.Series(days).cast(pl.Date), 'symbol': ['A'] * 3,
                         'close': [10.] * 3, 'volume': [100.] * 3})
    out = trailing_notional(RawStore(frame), days, ['A'], as_of=str(days[-1]),
                            window=2, volume_multiplier=1)
    assert out[-1, 0] == 1000.
    future = np.array(['2027-01-04'], dtype='datetime64[D]')
    future_frame = pl.DataFrame({'date': pl.Series(future).cast(pl.Date), 'symbol': ['A'],
                                 'close': [10.], 'volume': [100.]})
    with pytest.raises(ValueError, match='XSHG calendar does not cover'):
        trailing_notional(RawStore(future_frame), future, ['A'], as_of=str(future[-1]),
                          window=2, volume_multiplier=1, calendar='XSHG')


def test_liquidity_config_requires_capital_and_affects_identity():
    args=dict(universe='NASDAQ100',period_start='2024-01-01',period_end='2024-02-01')
    with pytest.raises(ValueError,match='requires initial_capital'):
        PortfolioBacktestConfig.preset('US',**args,liquidity_model='trailing_notional_v1',liquidity_volume_multiplier=1)
    off=PortfolioBacktestConfig.preset('US',**args,initial_capital=10000)
    on=PortfolioBacktestConfig.preset('US',**args,initial_capital=10000,liquidity_model='trailing_notional_v1',liquidity_volume_multiplier=1)
    assert off.config_hash()!=on.config_hash()
    assert PortfolioBacktestConfig.from_dict(on.to_dict()).to_dict()==on.to_dict()
