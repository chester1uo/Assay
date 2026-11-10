"""Validate the durable research loop and benchmark a prepared real market store.

Reads the source store only. All databases, caches and reports go under --output.
The reference cash/share ledger does not import production accounting helpers.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import date, timedelta
import hashlib
import json
from pathlib import Path
import time

import numpy as np

from assay.config import AssayConfig
from assay.data.store import DataStore
from assay.engine import FactorEngine
from assay.cache import L2FactorCache
from assay.research import ResearchWorkspace
from assay.service import AssayService


def ledger(open_, close, dates, symbols, trades, execution):
    """Independently mark cash/shares around fills, including proportional fees.

    Target weights follow the recorded fills. This isolates execution timing and
    accounting correctness; it is not an independent test of portfolio selection.
    """
    by_day = defaultdict(list)
    for trade in trades:
        by_day[trade['date']].append(trade)
    ix = {str(s): i for i, s in enumerate(symbols)}
    shares = np.zeros(len(symbols))
    last_mark = np.zeros(len(symbols))
    cash, nav = 1., []
    for day, label in enumerate(dates):
        raw_price = open_[day] if execution == 'next_open' else close[day]
        price = np.where(np.isfinite(raw_price) & (raw_price > 0), raw_price, last_mark)
        rows = by_day[str(label)]
        if rows:
            capital = cash + np.nansum(shares * price)
            weights = shares * price / capital
            weights = np.nan_to_num(weights)
            for trade in rows:
                weights[ix[trade['symbol']]] = trade['exec_w']
            capital *= 1. - sum(t['cost'] for t in rows)
            shares = np.divide(capital * weights, price, out=np.zeros_like(price), where=np.isfinite(price) & (price > 0))
            cash = capital * (1 - sum(weights))
        last_mark = np.where(np.isfinite(close[day]) & (close[day] > 0), close[day],
                             np.where(np.isfinite(open_[day]) & (open_[day] > 0), open_[day], last_mark))
        nav.append(cash + np.nansum(shares * last_mark))
    return np.asarray(nav)


def timed(fn):
    start = time.perf_counter()
    value = fn()
    return value, time.perf_counter() - start


def validate(source, output, period, count=24, repeats=3):
    source, output = source.resolve(), output.resolve()
    if count < 2 or repeats < 1:
        raise ValueError('require at least two factors and one repeat')
    if output == source or source in output.parents:
        raise ValueError('output must be outside the read-only source store')
    if output.exists() and any(output.iterdir()):
        raise ValueError('output must be new or empty; retain each validation run separately')
    output.mkdir(parents=True, exist_ok=True)
    cfg = AssayConfig(data_dir=output / 'runtime', market_dirs={'US': source})
    service = AssayService(cfg)
    store = DataStore(AssayConfig(data_dir=source, market='US'))
    service._stores['US'] = store
    build = lambda: FactorEngine.from_store(store, 'NASDAQ100', tuple(period), as_of=period[1], adj='split')
    panel = build()
    assert len(panel.dates) > 20 and len(panel.symbols) >= 10, 'insufficient real panel'
    source_files = [p for p in source.rglob('*.parquet') if
        'universe_snapshots' in p.parts or 'adj_events' in p.parts or
        ('price_raw' in p.parts and any(f'year={y}' in p.parts for y in range(int(period[0][:4])-1, int(period[1][:4])+1)))]
    provenance = {'source': str(source), 'panel_content_hash': panel.content_fingerprint(),
        'shape': list(panel._shape), 'period': period,
        'source_files': {str(p.relative_to(source)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(source_files)}}
    repo = Path(__file__).resolve().parents[1]
    provenance['implementation_hashes'] = {str(p.relative_to(repo)):hashlib.sha256(p.read_bytes()).hexdigest()
        for p in [Path(__file__).resolve(), *sorted((repo/'src'/'assay').rglob('*.py'))]}
    (output / 'provenance.json').write_text(json.dumps(provenance, indent=2))
    ws = service.research
    executions, checks = {}, {}
    for execution in ('next_open', 'next_close'):
        ds = ws.register_dataset('real-' + execution, 'NASDAQ100', period, execution=execution,
            horizons=[1], portfolio=dict(rebalance_type='daily', min_rebalance_interval=1,
                weight_method='quintile', min_stock_count=5, max_single_weight=.3,
                slippage_model='zero', commission_rate=.001, transfer_fee_rate=0., stamp_duty_rate=0.))
        sid = ws.create_session(ds['id'], 'Check net-return accounting and durable factor selection', max_experiments=4)['id']
        first, first_seconds = timed(lambda: ws.evaluate(sid, 'ts_returns(close,5)', 'short-term momentum', request_id='first'))
        assert first['status'] == 'completed', first
        report = first['report']['portfolio']
        observed = ledger(panel.field_matrix('open'), panel.field_matrix('close'), panel.dates, panel.symbols,
                          report['trade_log'], execution)
        np.testing.assert_allclose(observed, report['nav_series'], rtol=2e-10, atol=2e-10)
        failed = ws.evaluate(sid, 'not_a_real_operator(close)', 'retain invalid attempt', request_id='invalid')
        assert failed['status'] == 'failed'
        revised, revised_seconds = timed(lambda: ws.evaluate(sid, 'ts_returns(close,3)', 'shorter lookback', request_id='revision', parent_id=first['id']))
        assert revised['status'] == 'completed'
        selected = ws.decide_candidate(revised['id'], 'accepted', 'selected for further research after accounting checks', request_id='select')
        ws.decide_candidate(first['id'], 'archived', 'retain baseline and continue shorter lookback', request_id='archive')
        resumed = ResearchWorkspace(ws.path, ws.backends)
        assert resumed.evaluate(sid, revised['expr'], revised['hypothesis'], request_id='revision', parent_id=first['id']) == revised
        assert resumed.list_candidates(sid)[1]['decision'] == 'accepted'
        replay, replay_seconds = timed(lambda: resumed.replay_experiment(revised['id'], sid, request_id='replay'))
        assert replay['status'] == 'completed'
        np.testing.assert_allclose(replay['report']['portfolio']['nav_series'], revised['report']['portfolio']['nav_series'])
        assert replay['report']['panel_content_hash'] == revised['report']['panel_content_hash']
        manifest = resumed.export_session(sid)
        target = ResearchWorkspace(output / ('restored-' + execution + '.sqlite3'))
        assert target.import_session(manifest) == resumed.get_session(sid)
        assert target.factor_history(revised['factor_id'])['decisions'] == [selected]
        (output / (execution + '-manifest.json')).write_text(json.dumps(manifest, indent=2))
        executions[execution] = dict(session_id=sid, max_nav_difference=float(np.max(abs(observed - report['nav_series']))),
            total_return=report['total_return'], n_trades=len(report['trade_log']),
            statuses=[a['status'] for a in manifest['state']['experiments']],
            selection_roundtrip=True, replay_same_panel=True,
            complete_attempt_seconds=[first_seconds,revised_seconds,replay_seconds],
            complete_attempts_per_second=3/(first_seconds+revised_seconds+replay_seconds),
            warnings=report.get('warnings',[]))
    # Fresh formulas, shared panel and exact cache hits are separate workloads.
    exprs = [f'add(ts_returns(close,{n+2}),mul(ts_mean(volume,20),0.000001))' for n in range(count)]
    timings = defaultdict(list)
    cache = L2FactorCache(output / 'value-cache')
    keys = [hashlib.sha256((provenance['panel_content_hash'] + e).encode()).hexdigest() for e in exprs]
    baseline = None
    for repeat in range(repeats):
        cold, elapsed = timed(lambda: [build().evaluate(e).values for e in exprs])
        timings['fresh_panel_per_factor'].append(elapsed)
        batch, elapsed = timed(lambda: build().evaluate_many(exprs))
        batch = [r.values for r in batch]
        timings['fresh_batch_shared_panel_cse'].append(elapsed)
        if baseline is None:
            baseline = cold
        for a, b, c in zip(baseline, cold, batch):
            np.testing.assert_allclose(a, b, equal_nan=True, rtol=1e-12, atol=1e-12)
            np.testing.assert_allclose(a, c, equal_nan=True, rtol=1e-12, atol=1e-12)
        if repeat == 0:
            _, write_s = timed(lambda: [cache.put(k, a) for k, a in zip(keys, baseline)])
            checks['cache_write_seconds'] = write_s
        cached, elapsed = timed(lambda: [cache.get(k) for k in keys])
        timings['exact_result_cache_hit'].append(elapsed)
        for a, b in zip(baseline, cached):
            np.testing.assert_allclose(a, b, equal_nan=True, rtol=0, atol=0)
    benchmark = {name: dict(seconds=values, median_seconds=float(np.median(values)),
                  factors_per_second=count / float(np.median(values))) for name, values in timings.items()}
    checks.update(engineering_loop=True, execution_ledger_match=True, factor_values_match=True,
                  real_data=True, factors=count, repeats=repeats, provider_calls=0,
                  reference_scope='Cash/share NAV oracle follows recorded execution weights; selection and PIT vendor completeness are separate.',
                  source_read_only=True, executions=executions, benchmark=benchmark)
    checks['source_hashes_unchanged'] = all(hashlib.sha256((source/p).read_bytes()).hexdigest()==h
                                         for p,h in provenance['source_files'].items())
    assert checks['source_hashes_unchanged']
    (output / 'checks.json').write_text(json.dumps(checks, indent=2))
    (output / 'expressions.json').write_text(json.dumps(exprs, indent=2))
    return checks


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-root', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--period', nargs=2, default=['2023-01-01', '2023-12-31'])
    parser.add_argument('--factors', type=int, default=24)
    parser.add_argument('--repeats', type=int, default=3)
    args = parser.parse_args()
    print(json.dumps(validate(args.data_root, args.output, args.period, args.factors, args.repeats), indent=2))


if __name__ == '__main__':
    main()
