"""Cash and settled-share invariants preserved from the deployed market engine."""
import numpy as np
import pytest
from assay.portfolio.config import PortfolioBacktestConfig
from assay.portfolio.execution import ExecutionSimulator


def config():
    return PortfolioBacktestConfig.preset('A',period_start='2024-01-01',period_end='2024-12-31')


def test_unknown_a_share_limits_block_orders():
    result=ExecutionSimulator(config()).execute(np.array([.3]),np.array([0.]),exec_price=np.array([10.]))
    assert result.executed_weights[0]==0 and result.total_cost==0
    assert result.diag['blocked_limit_data']==1
    assert result.trades[0].blocked_reason=='limit_data_missing'


def test_blocked_sale_does_not_fund_purchase():
    result=ExecutionSimulator(config()).execute(np.array([0.,1.]),np.array([1.,0.]),
        exec_price=np.array([10.,10.]),limit_up=np.array([11.,11.]),limit_down=np.array([10.,9.]))
    np.testing.assert_array_equal(result.executed_weights,[1.,0.])
    assert result.diag['blocked_cash']==1 and result.total_cost==0


def test_same_day_only_newly_acquired_shares_are_locked():
    sim=ExecutionSimulator(config()); inventory={}
    bought=sim.execute(np.array([.5]),np.array([.3]),exec_price=np.array([10.]),prev_close=np.array([10.]),
        acquired_units=inventory,portfolio_value=100.,date='2024-01-02')
    sold=sim.execute(np.array([0.]),bought.executed_weights,exec_price=np.array([10.]),prev_close=np.array([10.]),
        acquired_units=inventory,portfolio_value=100.,date='2024-01-02')
    assert sold.executed_weights[0]==pytest.approx(.2)
    assert sold.trades[0].qty_frac==pytest.approx(-.3)
    assert sold.trades[0].deferred_reason=='t_plus_1'
    settled=sim.execute(np.array([0.]),sold.executed_weights,exec_price=np.array([10.]),prev_close=np.array([10.]),
        acquired_units=inventory,portfolio_value=100.,date='2024-01-03')
    assert settled.executed_weights[0]==pytest.approx(0.)


def test_cash_equity_including_cost_never_borrows():
    result=ExecutionSimulator(config()).execute(np.array([1.]),np.array([0.]),exec_price=np.array([10.]),prev_close=np.array([10.]))
    assert result.total_cost>0
    assert result.executed_weights.sum()+result.total_cost<=1.+1e-12


def test_minimum_fee_cannot_make_cash_negative_on_sale():
    from assay.portfolio.costs import TransactionCostModel
    cfg=PortfolioBacktestConfig.preset('US',commission_min=5.,commission_rate=0.,
        transfer_fee_rate=0.,stamp_duty_rate=0.,slippage_model='zero')
    sim=ExecutionSimulator(cfg,TransactionCostModel(cfg,nav_capital=100.))
    result=sim.execute(np.array([.98]),np.array([.99]),exec_price=np.array([1.]))
    assert result.executed_weights[0]==pytest.approx(.99)
    assert result.total_cost==0
    assert result.trades[0].blocked_reason=='cash'
    assert result.residual_targets[0]==pytest.approx(.98)


def test_cash_generating_sales_can_fund_minimum_fee_on_small_sales():
    from assay.portfolio.costs import TransactionCostModel
    cfg=PortfolioBacktestConfig.preset('US',commission_min=5.,commission_rate=0.,
        transfer_fee_rate=0.,stamp_duty_rate=0.,slippage_model='zero')
    sim=ExecutionSimulator(cfg,TransactionCostModel(cfg,nav_capital=100.))
    result=sim.execute(np.array([.48,0.]),np.array([.49,.51]),exec_price=np.array([1.,1.]))
    np.testing.assert_allclose(result.executed_weights,[.48,0.])
    assert result.total_cost==pytest.approx(.1)
    assert result.executed_weights.sum()+result.total_cost<=1.


def test_cash_blocked_sale_preserves_dependent_buy_for_defer():
    from assay.portfolio.costs import TransactionCostModel
    cfg=PortfolioBacktestConfig.preset('US',commission_min=5.,commission_rate=0.,
        transfer_fee_rate=0.,stamp_duty_rate=0.,slippage_model='zero',partial_fill_handling='defer')
    sim=ExecutionSimulator(cfg,TransactionCostModel(cfg,nav_capital=100.))
    first=sim.execute(np.array([.98,.01]),np.array([.99,0.]),exec_price=np.array([1.,1.]))
    np.testing.assert_allclose(first.residual_targets,[.98,.01])
    assert [t.blocked_reason for t in first.trades]==['cash','cash']
    # A subsequent portfolio with enough monetary capital can execute both intents.
    sim.cost_model.nav_capital=1000.
    later=sim.execute(first.residual_targets,first.executed_weights,exec_price=np.array([1.,1.]))
    np.testing.assert_allclose(later.executed_weights,[.98,.01],atol=1e-12)
    assert later.executed_weights.sum()+later.total_cost<=1.+1e-12


@pytest.mark.parametrize('market', ['US', 'HK', 'A'])
def test_costs_and_capacity_against_currency_ledger(market):
    from assay.portfolio.costs import TransactionCostModel
    cfg=PortfolioBacktestConfig.preset(market,commission_min=5.,commission_rate=.0003,
        transfer_fee_rate=.00002,stamp_duty_rate=.0005,slippage_model='sqrt',
        slippage_k=.2,max_adv_fraction=.1,partial_fill_handling='defer')
    sim=ExecutionSimulator(cfg,TransactionCostModel(cfg,nav_capital=1000.))
    result=sim.execute(np.array([.1,.5,.2]),np.array([.3,.3,.3]),
        exec_price=np.array([10.,10.,10.]),prev_close=np.array([10.,10.,10.]),
        tradable_mask=np.array([True,True,False]),adv_fraction=np.array([.05,.5,.1]))
    # $200 sale and $40 capacity-limited buy; third holding is suspended.
    # Commission floors are $5 per trade; impact uses filled ADV participation.
    sell_fee=5 + 200*(.00002 + .0005 + .2*np.sqrt(.05))
    buy_stamp=.0005 if market=='HK' else 0.
    buy_fee=5 + 40*(.00002 + buy_stamp + .2*np.sqrt(.1))
    np.testing.assert_allclose(result.executed_weights,[.1,.34,.3],atol=1e-13)
    assert result.total_cost==pytest.approx((sell_fee+buy_fee)/1000.,abs=1e-13)
    assert result.diag['blocked_suspended']==1
    assert result.residual_targets[1]==pytest.approx(.5)
    assert result.residual_targets[2]==pytest.approx(.2)
