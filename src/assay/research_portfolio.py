"""Freeze portfolio settings alongside the research dataset specification."""
from __future__ import annotations

from dataclasses import fields


def accounting_protocol(attempt, dataset):
    """Identify the accounting used by an attempt without changing old records."""
    portfolio = dataset.get('portfolio')
    if not isinstance(portfolio, dict) or not portfolio or portfolio.get('enabled') is False:
        return None
    declared = attempt.get('portfolio_accounting_protocol')
    if declared:
        return declared
    report = attempt.get('panel_report') or attempt.get('report') or {}
    evidence = report.get('portfolio') if isinstance(report, dict) else None
    lineage = evidence.get('lineage') if isinstance(evidence, dict) else None
    protocol = lineage.get('accounting_protocol') if isinstance(lineage, dict) else None
    return protocol or dataset.get('portfolio_accounting_protocol') or 'legacy-unversioned'


def normalize_portfolio(dataset, options, market):
    from assay.portfolio import PortfolioBacktestConfig

    if not isinstance(options, dict):
        raise ValueError('portfolio must be an object')
    if options == {'enabled': False}:
        return options
    if dataset.get('score_period'):
        raise ValueError('portfolio scoring with explicit score windows is not supported; use portfolio={"enabled": false}')
    if dataset['execution'] not in {'next_open', 'next_close'}:
        raise ValueError('portfolio requires next_open or next_close execution')
    fixed = dict(universe=dataset['universe'], market='A' if market == 'CN' else market,
                 period_start=dataset['period'][0], period_end=dataset['period'][1],
                 as_of_date=dataset['as_of'], execution_price=dataset['execution'],
                 adjustment=dataset['adj'])
    unknown = set(options) - {f.name for f in fields(PortfolioBacktestConfig)}
    if unknown:
        raise ValueError(f'unknown portfolio options: {sorted(unknown)}')
    if any(k in options and options[k] != v for k, v in fixed.items()):
        raise ValueError('portfolio universe, period, as_of, execution and adjustment must match dataset')
    if options.get('execution_offset_days', 1) != 1:
        raise ValueError('portfolio execution_offset_days must match next-day dataset execution')
    # Missing market-specific data is not synthesized. The default cost is the
    # market preset commission; impact requires explicit participation inputs.
    params = {**options, **fixed}
    params.pop('market')
    config = PortfolioBacktestConfig.preset(fixed['market'], **params)
    return config.to_dict()


def evaluate_portfolio(service, expr, dataset, *, engine=None):
    from assay.portfolio import PortfolioBacktestConfig

    config = PortfolioBacktestConfig.from_dict(dataset['portfolio'])
    report = service.backtest_portfolio(expr, config, as_of=dataset['as_of'], engine=engine).to_dict()
    report['status'] = 'completed' if report.get('n_trading_days', 0) >= 2 and report.get('nav_series') else 'failed'
    report['spec_hash'] = dataset['spec_hash']
    report['cost_drag_definition'] = ('gross_return minus total_return; positive values represent return lost to costs. '
                                      'Gross NAV follows the realized net portfolio weights.')
    report['cost_assumptions'] = dict(commission_rate=config.commission_rate, commission_min=config.commission_min,
        slippage_model=config.slippage_model, impact_participation_supplied=False,
        minimum_commission_capital_supplied=config.initial_capital is not None,
        initial_capital=config.initial_capital)
    return report
