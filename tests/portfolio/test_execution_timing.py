"""Independent share ledger checks execution-day P&L, gaps and switching books."""
import numpy as np
import pytest

from assay.portfolio.accounting import PortfolioAccountant
from assay.portfolio.config import PortfolioBacktestConfig


def share_ledger(open_, close, targets, commission):
    """Cash + shares oracle, without production returns/accounting helpers."""
    shares = np.zeros(open_.shape[1])
    cash = 1.0
    nav = []
    for day in range(len(open_)):
        if day in targets:
            capital = cash + sum(shares * open_[day])
            old_weights = shares * open_[day] / capital
            target = targets[day]
            fee = commission * sum(abs(target - old_weights)) * capital
            capital -= fee
            shares = capital * target / open_[day]
            cash = capital * (1 - sum(target))
        nav.append(cash + sum(shares * close[day]))
    return np.array(nav)


@pytest.mark.parametrize('commission', [0., .001])
def test_open_fills_earn_intraday_but_do_not_earn_prior_gap(commission):
    open_ = np.array([[100., 50.], [120., 60.], [90., 80.], [110., 75.]])
    close = np.array([[110., 55.], [132., 54.], [99., 88.], [121., 90.]])
    schedule = {1: np.array([.6, .4]), 2: np.array([.2, .8]), 3: np.array([0., 0.])}
    previous = np.vstack([close[:1], close[:-1]])
    config = PortfolioBacktestConfig.preset('US', commission_rate=commission,
        slippage_model='zero', stamp_duty_rate=0., transfer_fee_rate=0.)
    accountant = PortfolioAccountant(config)
    got = accountant.run(close / previous - 1, schedule, exec_prices=open_,
        pre_trade_returns=open_ / previous - 1, post_trade_returns=close / open_ - 1)
    expected = share_ledger(open_, close, schedule, commission)
    np.testing.assert_allclose(got.nav_series, expected, rtol=1e-13, atol=1e-13)
    assert got.nav_series[1] == pytest.approx((1 - commission) * 1.02)


def test_return_legs_require_matching_arrays():
    accountant = PortfolioAccountant(PortfolioBacktestConfig.preset('US'))
    with pytest.raises(ValueError, match='both'):
        accountant.run(np.zeros((3, 2)), {}, pre_trade_returns=np.zeros((3, 2)))


def test_missing_bar_marks_resume_from_last_available_quote():
    from assay.portfolio import PortfolioBacktester
    close=np.array([[100.],[np.nan],[121.],[np.nan]])
    open_=np.array([[100.],[np.nan],[110.],[122.]])
    marks=PortfolioBacktester._mark_prices(close,open_)
    np.testing.assert_allclose(marks[:,0],[100.,100.,121.,122.])
    returns=PortfolioBacktester._daily_returns(marks)
    assert returns[2,0] == pytest.approx(.21)


def test_next_close_labels_enter_after_signal_and_old_scores_have_distinct_key():
    from assay.evaluator import forward_returns
    from assay.factordb.registry import spec_key
    import hashlib
    import json
    close=np.array([[100.],[110.],[121.],[100.]])
    returns=forward_returns(close,None,[1],execution='next_close')[1][:,0]
    np.testing.assert_allclose(returns[:2],[.1,100/121-1])
    assert np.isnan(returns[2:]).all()
    legacy=json.dumps(['NASDAQ100','2024-01-01','2024-02-01',[1],'next_close'])
    assert spec_key('NASDAQ100','2024-01-01','2024-02-01',[1],'next_close') != hashlib.blake2b(legacy.encode(),digest_size=8).hexdigest()


def test_adjustment_changes_explicit_config_identity_and_roundtrips():
    config=PortfolioBacktestConfig.preset('US',adjustment='total')
    assert PortfolioBacktestConfig.from_dict(config.to_dict()).config_hash() == config.config_hash()
    assert config.config_hash() != PortfolioBacktestConfig.preset('US',adjustment='split').config_hash()
