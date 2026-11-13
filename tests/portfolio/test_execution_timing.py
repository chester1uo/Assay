"""Independent share ledger checks execution-day P&L, gaps and switching books."""
import numpy as np
import pytest

from assay.portfolio.accounting import PortfolioAccountant
from assay.portfolio.config import PortfolioBacktestConfig


def share_ledger(open_, close, targets, commission):
    """Cash + shares oracle; targets use pre-trade NAV and fees come from cash."""
    shares = np.zeros(open_.shape[1])
    cash = 1.0
    nav = []
    for day in range(len(open_)):
        if day in targets:
            capital = cash + sum(shares * open_[day])
            desired = targets[day] * capital / open_[day]
            delta = desired - shares
            sell = np.minimum(delta, 0.)
            cash -= np.dot(sell, open_[day]) + commission * np.dot(-sell, open_[day])
            shares += sell
            buy = np.maximum(delta, 0.)
            required = np.dot(buy, open_[day]) * (1 + commission)
            if required > cash:
                buy *= cash / required
            cash -= np.dot(buy, open_[day]) * (1 + commission)
            shares += buy
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
    assert got.nav_series[1] == pytest.approx(1.02 / (1 + commission))


@pytest.mark.parametrize('market', ['US', 'HK'])
def test_commission_keeps_filled_shares_and_charges_cash(market):
    cfg = PortfolioBacktestConfig.preset(market, commission_rate=.001, commission_min=0.,
        transfer_fee_rate=0., stamp_duty_rate=0., slippage_model='zero')
    returns = np.zeros((3, 1))
    result = PortfolioAccountant(cfg).run(returns, {0: np.array([.1]), 2: np.array([0.])},
        exec_prices=np.full((3, 1), 10.), dates=['2024-01-01', '2024-01-02', '2024-01-03'])
    np.testing.assert_allclose(result.nav_series, [0.9999, 0.9999, 0.9998], atol=1e-12)
    holdings = result.weights[:, 0] * result.nav_series
    np.testing.assert_allclose(holdings, [.1, .1, 0.], atol=1e-12)
    assert result.trades[0].qty_frac == pytest.approx(.1)
    assert result.trades[1].qty_frac * result.nav_series[1] == pytest.approx(-.1)


@pytest.mark.parametrize('market', ['US', 'HK'])
def test_minimum_commission_uses_current_currency_nav(market):
    cfg = PortfolioBacktestConfig.preset(market, initial_capital=1000., commission_min=5.,
        commission_rate=0., transfer_fee_rate=0., stamp_duty_rate=0., slippage_model='zero')
    result = PortfolioAccountant(cfg).run(np.array([[0.], [1.]]),
        {0: np.array([.1]), 1: np.array([0.])}, exec_prices=np.array([[10.], [20.]]))
    # Independent cash ledger: 1000 - 100 - 5 + 200 - 5 = 1090.
    np.testing.assert_allclose(result.nav_series, [.995, 1.09], atol=1e-13)
    assert result.trades[0].cost == pytest.approx(.005)
    assert result.trades[1].cost * 1.095 == pytest.approx(.005)


def test_cash_funded_full_allocation_reserves_commission():
    cfg = PortfolioBacktestConfig.preset('US', commission_rate=.001, commission_min=0.,
        transfer_fee_rate=0., stamp_duty_rate=0., slippage_model='zero')
    result = PortfolioAccountant(cfg).run(np.zeros((2, 1)), {0: np.array([1.])},
        exec_prices=np.full((2, 1), 10.))
    assert result.nav_series[0] == pytest.approx(1 / 1.001, abs=1e-12)
    assert result.weights[0, 0] == pytest.approx(1., abs=1e-12)
    assert result.diag['blocked_cash'] == 1


@pytest.mark.parametrize('market', ['US', 'HK'])
def test_proportional_fee_fast_path_matches_iterative_cash_funding(market):
    from assay.portfolio.costs import TransactionCostModel
    from assay.portfolio.execution import ExecutionSimulator

    class CustomCostModel(TransactionCostModel):
        pass  # Subclasses retain the general iterative path.

    cfg = PortfolioBacktestConfig.preset(market, commission_rate=.001, commission_min=0.,
        transfer_fee_rate=.00002, stamp_duty_rate=.0004, slippage_model='zero')
    args = (np.array([.25, .75]), np.array([.8, .1]), np.array([10., 20.]))
    fast = ExecutionSimulator(cfg, TransactionCostModel(cfg)).execute(args[0], args[1], exec_price=args[2])
    iterative = ExecutionSimulator(cfg, CustomCostModel(cfg)).execute(args[0], args[1], exec_price=args[2])
    np.testing.assert_allclose(fast.executed_weights, iterative.executed_weights, atol=3e-14)
    assert fast.total_cost == pytest.approx(iterative.total_cost, abs=3e-14)
    assert fast.executed_weights.sum() + fast.total_cost <= 1.0 + 1e-14


def test_proportional_cash_funding_many_assets():
    from assay.portfolio.costs import TransactionCostModel
    from assay.portfolio.execution import ExecutionSimulator

    class IterativeCostModel(TransactionCostModel):
        pass

    cfg = PortfolioBacktestConfig.preset('US', commission_rate=.001, commission_min=0.,
        transfer_fee_rate=0., stamp_duty_rate=0., slippage_model='zero')
    fast = ExecutionSimulator(cfg, TransactionCostModel(cfg))
    reference = ExecutionSimulator(cfg, IterativeCostModel(cfg))
    rng = np.random.default_rng(297)
    for n in (10, 100, 1000):
        for _ in range(4):
            current = rng.dirichlet(np.ones(n)) * .99
            target = rng.dirichlet(np.ones(n))
            price = np.full(n, 10.)
            got = fast.execute(target, current, exec_price=price, record_trades=False)
            want = reference.execute(target, current, exec_price=price, record_trades=False)
            np.testing.assert_allclose(got.executed_weights, want.executed_weights, atol=3e-14)
            assert got.total_cost == pytest.approx(want.total_cost, abs=3e-14)
            assert got.executed_weights.sum() + got.total_cost <= 1.0 + 1e-12


def test_long_short_fees_preserve_long_and_short_quantities():
    cfg = PortfolioBacktestConfig.preset('US', long_short=True, net_exposure=0.,
        commission_rate=.001, commission_min=0., transfer_fee_rate=0.,
        stamp_duty_rate=0., slippage_model='zero')
    result = PortfolioAccountant(cfg).run(np.zeros((2, 2)),
        {0: np.array([.5, -.5])}, exec_prices=np.full((2, 2), 10.))
    np.testing.assert_allclose(result.nav_series, [.999, .999], atol=1e-12)
    np.testing.assert_allclose(result.weights * result.nav_series[:, None],
                               [[.5, -.5], [.5, -.5]], atol=1e-12)


@pytest.mark.parametrize('handling,expected_rebalances', [('defer', [0, 1, 2]), ('cancel', [0, 1])])
def test_cash_limited_buy_follows_blocked_sale_policy(handling, expected_rebalances):
    cfg = PortfolioBacktestConfig.preset('US', partial_fill_handling=handling,
        commission_rate=.001, commission_min=0., transfer_fee_rate=0.,
        stamp_duty_rate=0., slippage_model='zero')
    result = PortfolioAccountant(cfg).run(np.zeros((3, 2)),
        {0: np.array([.8, 0.]), 1: np.array([0., .8])},
        exec_prices=np.full((3, 2), 10.),
        tradable_masks=np.array([[True, True], [False, True], [True, True]]))
    assert result.diag['blocked_cash'] > 0
    assert result.rebalance_dates_idx == expected_rebalances
    assert np.all(result.weights.sum(axis=1) <= 1 + 1e-12)
    if handling == 'defer':
        assert result.weights[-1, 0] == pytest.approx(0.)
    else:
        assert result.weights[-1, 0] > 0.


def test_explicit_long_short_configuration_can_borrow_for_fees():
    cfg = PortfolioBacktestConfig.preset('US', long_short=True, commission_rate=.001,
        commission_min=0., transfer_fee_rate=0., stamp_duty_rate=0., slippage_model='zero')
    result = PortfolioAccountant(cfg).run(np.zeros((1, 1)), {0: np.array([1.])},
        exec_prices=np.full((1, 1), 10.))
    assert result.nav_series[0] == pytest.approx(.999)
    assert result.weights[0, 0] * result.nav_series[0] == pytest.approx(1.)
    assert 1 - result.weights[0].sum() < 0


def test_long_only_configuration_rejects_short_targets():
    cfg = PortfolioBacktestConfig.preset('US', long_short=False)
    with pytest.raises(ValueError, match='long-only'):
        PortfolioAccountant(cfg).run(np.zeros((1, 1)), {0: np.array([-.5])},
            exec_prices=np.full((1, 1), 10.))


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
