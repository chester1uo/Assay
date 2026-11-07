"""Installable, offline research demo using generated OHLCV (no vendor credentials)."""
from __future__ import annotations

import argparse
from datetime import date, timedelta
import html
import json
from pathlib import Path

import numpy as np
import polars as pl

import assay
from assay.config import AssayConfig
from assay.data.schemas import PRICE_RAW_SCHEMA, UNIVERSE_SNAPSHOTS_SCHEMA, price_partition_path, universe_snapshots_path


def prepare_demo(root: Path, *, seed=7):
    """Prepare a new reproducible synthetic store and return the fixed dataset spec."""
    root.mkdir(parents=True, exist_ok=False)
    rng = np.random.default_rng(seed)
    n, t = 24, 120
    days = []
    day = date(2024, 1, 2)
    while len(days) < t:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    symbols = [f'S{i:03d}' for i in range(n)]
    close = 50 * np.exp(np.cumsum(rng.normal(.0002, .018, size=(t, n)), axis=0))
    spread = np.abs(rng.normal(0, .01, size=(t, n)))
    high, low = close * (1 + spread), close * (1 - spread)
    open_ = low + (high - low) * rng.random((t, n))
    volume = np.abs(rng.normal(2e6, 4e5, size=(t, n))) + 1
    panel = pl.DataFrame(dict(date=np.repeat(days, n).tolist(), symbol=symbols * t,
        open=open_.ravel(), high=high.ravel(), low=low.ravel(), close=close.ravel(),
        volume=volume.ravel(), vwap=((open_ + high + low + close) / 4).ravel()))
    raw = panel.with_columns(pl.col('date').alias('as_of_date'),
        pl.lit(1).alias('transactions'), pl.lit('assay-synthetic-demo').alias('source_id'))
    for year, month in sorted({(d.year, d.month) for d in days}):
        path = price_partition_path(root, 'US', year, month)
        path.parent.mkdir(parents=True, exist_ok=True)
        raw.filter((pl.col('date').dt.year() == year) & (pl.col('date').dt.month() == month)).select(
            [pl.col(k).cast(v) for k, v in PRICE_RAW_SCHEMA.items()]).write_parquet(path)
    path = universe_snapshots_path(root, 'US')
    path.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame([dict(index_id='NASDAQ100', effective_date=days[0], as_of_date=days[0], symbols=symbols)],
                 schema=UNIVERSE_SNAPSHOTS_SCHEMA).write_parquet(path)
    service = assay.init(AssayConfig.for_tests(root))
    dataset = service.research.register_dataset('synthetic-demo', 'NASDAQ100', [str(days[0]), str(days[-1])], horizons=[1])
    (root / 'demo-dataset.json').write_text(json.dumps(dict(seed=seed, synthetic=True, dataset=dataset), indent=2))
    return service, dataset


def write_report(manifest, path: Path):
    """Portable read-only evidence view; values are escaped and no scripts are used."""
    state = manifest['state']
    rows = ''.join('<tr>' + ''.join('<td>' + html.escape(str(value)) + '</td>' for value in (
        a['id'], a['hypothesis'], a['expr'], a['status'], (a.get('report') or {}).get('rank_ic'),
        (a.get('report') or {}).get('failure_mode') or a.get('error') or '', a.get('parent_id') or a.get('replay_of') or '')) + '</tr>'
        for a in state['experiments'])
    path.write_text('''<!doctype html><html lang="en"><meta charset="utf-8"><title>Assay research record</title>
<style>body{font:16px system-ui;margin:3em;color:#172337}table{border-collapse:collapse;width:100%}td,th{padding:12px;border:1px solid #ccd3dd;text-align:left;overflow-wrap:anywhere}pre{white-space:pre-wrap}small{color:#546273}</style>
<h1>Assay research record</h1>''' + '<p>' + html.escape(state['goal']) + '</p>' +
        f'<p>Attempts: {len(state["experiments"])} · Remaining budget: {state["remaining_experiments"]}</p>' +
        '<table><tr><th>ID</th><th>Hypothesis</th><th>Expression</th><th>Status</th><th>RankIC</th><th>Failure</th><th>Source</th></tr>' + rows +
        '</table><h2>Research notes</h2><pre>' + html.escape(json.dumps(state['notes'], indent=2)) +
        '</pre><details><summary>Dataset configuration and integrity hash</summary><pre>' +
        html.escape(json.dumps(state['dataset'], indent=2)) + '</pre><small>' + html.escape(manifest['manifest_hash']) +
        '</small></details><p>Evaluation completion does not establish investment performance. This page displays saved records.</p></html>', encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True, help='New workspace directory')
    parser.add_argument('--seed', type=int, default=7)
    parser.add_argument('--prepare-only', action='store_true', help='Prepare data for an external MCP agent')
    args = parser.parse_args()
    service, dataset = prepare_demo(args.output, seed=args.seed)
    if args.prepare_only:
        print(json.dumps(dataset, indent=2))
        return
    ws = service.research
    sid = ws.create_session(dataset['id'], 'Offline synthetic lifecycle demonstration', max_experiments=3)['id']
    first = ws.evaluate(sid, '-cs_rank(ts_returns(close,5))', 'Short-horizon reversal', request_id='first')
    ws.evaluate(sid, 'unknown_operator(close)', 'Retain a diagnostic failure', request_id='failure')
    ws.add_note(sid, 'Keep the failure record; compare a shorter lookback after restart.')
    assay.init(AssayConfig.for_tests(args.output))
    ws = assay.research_workspace()
    ws.evaluate(sid, '-cs_rank(ts_returns(close,3))', 'Compare shorter lookback', request_id='revision', parent_id=first['id'])
    manifest = ws.export_session(sid)
    (args.output / 'manifest.json').write_text(json.dumps(manifest, indent=2))
    write_report(manifest, args.output / 'report.html')
    print(json.dumps(dict(synthetic=True, provider_calls=0, session_id=sid,
                         statuses=[a['status'] for a in manifest['state']['experiments']],
                         report=str((args.output / 'report.html').resolve())), indent=2))


if __name__ == '__main__':
    main()
