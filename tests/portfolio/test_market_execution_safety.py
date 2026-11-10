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
