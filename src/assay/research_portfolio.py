"""Freeze portfolio settings alongside the research dataset specification."""
from __future__ import annotations

from dataclasses import fields


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
    report['cost_assumptions'] = dict(commission_rate=config.commission_rate,
        slippage_model=config.slippage_model, impact_participation_supplied=False,
        minimum_commission_capital_supplied=False)
    if config.slippage_model != 'zero':
        report.setdefault('warnings', []).append('No order participation inputs supplied; market impact is zero.')
    if config.commission_min:
        report.setdefault('warnings', []).append('No capital scale supplied; the minimum commission floor is not applied.')
    return report
