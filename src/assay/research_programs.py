"""Persistent research programs joining sessions across evaluation windows."""
from __future__ import annotations

import hashlib
import json
import math

from assay.research_checkpoints import _text


def _program(db, program_id):
    row = db.execute('SELECT payload FROM research_programs WHERE id=?', (program_id,)).fetchone()
    if row is None:
        raise ValueError(f'unknown research program: {program_id}')
    return json.loads(row[0])


def create(ws, name, goal, request_id):
    from assay.research import _id, _json, _now
    _text(name, 'name', 200); _text(goal, 'goal', 4000); _text(request_id, 'request_id')
    with ws._db() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT payload FROM research_programs WHERE request_id=?', (request_id,)).fetchone()
        if row:
            result = json.loads(row[0])
            if (result['name'], result['goal']) != (name, goal):
                raise ValueError('program request_id was already used with different inputs')
            return result
        result = dict(id=_id('program_'), name=name, goal=goal, request_id=request_id, created_at=_now())
        db.execute('INSERT INTO research_programs VALUES (?,?,?)', (result['id'], request_id, _json(result)))
    return result


def attach(ws, program_id, session_id, label=''):
    from assay.research import _now
    _text(program_id, 'program_id'); _text(session_id, 'session_id')
    if not isinstance(label, str) or len(label) > 200:
        raise ValueError('label must be a string of at most 200 characters')
    with ws._db() as db:
        db.execute('BEGIN IMMEDIATE')
        _program(db, program_id)
        if not db.execute('SELECT 1 FROM sessions WHERE id=?', (session_id,)).fetchone():
            raise ValueError(f'unknown research session: {session_id}')
        row = db.execute('SELECT label,linked_at FROM research_program_sessions WHERE program_id=? AND session_id=?',
                         (program_id, session_id)).fetchone()
        if row:
            if row['label'] != label:
                raise ValueError('session is already attached with a different label')
            linked_at = row['linked_at']
        else:
            linked_at = _now()
            db.execute('INSERT INTO research_program_sessions VALUES (?,?,?,?)',
                       (program_id, session_id, label, linked_at))
    return dict(program_id=program_id, session_id=session_id, label=label, linked_at=linked_at)


def get(ws, program_id):
    with ws._db() as db:
        db.execute('BEGIN')
        program = _program(db, program_id)
        rows = db.execute('SELECT p.session_id,p.label,p.linked_at,s.goal,s.dataset_id,d.payload '
                          'FROM research_program_sessions p JOIN sessions s ON s.id=p.session_id '
                          'JOIN datasets d ON d.id=s.dataset_id WHERE p.program_id=? ORDER BY p.rowid',
                          (program_id,)).fetchall()
    sessions = []
    for row in rows:
        item = dict(row)
        item['dataset'] = json.loads(item.pop('payload'))
        sessions.append(item)
    return dict(program=program, sessions=sessions)


def list_programs(ws):
    with ws._db() as db:
        rows = db.execute('SELECT payload FROM research_programs ORDER BY rowid').fetchall()
    return [json.loads(row[0]) for row in rows]


def _internal_request(program_id, target_session_id, request_id):
    from assay.research import _json
    return 'reassess:' + hashlib.sha256(_json([program_id, target_session_id, request_id]).encode()).hexdigest()


def _comparison_settings(dataset):
    keys = ('backend', 'universe', 'horizons', 'execution', 'adj', 'portfolio', 'evaluation_protocol',
            'portfolio_accounting_protocol', 'backend_options', 'expression_policy')
    result = {key: dataset.get(key) for key in keys}
    if isinstance(result['portfolio'], dict):
        result['portfolio'] = {k: v for k, v in result['portfolio'].items()
                               if k not in ('period_start', 'period_end', 'as_of_date')}
    return result


def reassess(ws, program_id, source_experiment_id, target_session_id, request_id, agent):
    _text(program_id, 'program_id'); _text(source_experiment_id, 'source_experiment_id')
    _text(target_session_id, 'target_session_id'); _text(request_id, 'request_id')
    _text(agent, 'agent')
    with ws._db() as db:
        db.execute('BEGIN IMMEDIATE')
        _program(db, program_id)
        row = db.execute('SELECT e.payload FROM experiments e JOIN research_program_sessions p '
                         'ON p.session_id=e.session_id WHERE p.program_id=? AND e.id=?',
                         (program_id, source_experiment_id)).fetchone()
        if not row:
            raise ValueError('source experiment is not in the research program')
        source = json.loads(row[0])
        if source['status'] != 'completed' or not source.get('factor_id'):
            raise ValueError('reassessment requires a completed source factor')
        if not db.execute('SELECT 1 FROM research_program_sessions WHERE program_id=? AND session_id=?',
                          (program_id, target_session_id)).fetchone():
            raise ValueError('target session is not in the research program')
        if source['session_id'] == target_session_id:
            raise ValueError('reassessment requires a different target session')
        row = db.execute('SELECT program_id,source_experiment_id FROM research_reassessments '
                         'WHERE target_session_id=? AND request_id=?', (target_session_id, request_id)).fetchone()
        if row and (row['program_id'], row['source_experiment_id']) != (program_id, source_experiment_id):
            raise ValueError('reassessment request_id was already used with different inputs')
        if not row:
            db.execute('INSERT INTO research_reassessments VALUES (?,?,?,?)',
                       (program_id, target_session_id, request_id, source_experiment_id))
    attempt = ws.evaluate(target_session_id, source['expr'], source['hypothesis'],
                          request_id=_internal_request(program_id, target_session_id, request_id), agent=agent)
    return dict(program_id=program_id, source_experiment_id=source_experiment_id, attempt=attempt)


def factor_history(ws, program_id, factor_id, offset=0, limit=20):
    _text(factor_id, 'factor_id')
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError('require offset >= 0 and 1 <= limit <= 100')
    with ws._db() as db:
        db.execute('BEGIN')
        _program(db, program_id)
        rows = db.execute('SELECT e.payload,d.payload AS dataset,p.label FROM research_program_sessions p '
                          'JOIN sessions s ON s.id=p.session_id JOIN datasets d ON d.id=s.dataset_id '
                          'JOIN experiments e ON e.session_id=s.id '
                          'JOIN research_factor_attempts f ON f.experiment_id=e.id '
                          'WHERE p.program_id=? AND f.factor_id=? ORDER BY e.rowid', (program_id, factor_id)).fetchall()
        links = db.execute('SELECT target_session_id,request_id,source_experiment_id FROM research_reassessments '
                           'WHERE program_id=?', (program_id,)).fetchall()
    sources = {(r['target_session_id'], _internal_request(program_id, r['target_session_id'], r['request_id'])):
               r['source_experiment_id'] for r in links}
    history, specs = [], []
    for row in rows:
        attempt, dataset = json.loads(row['payload']), json.loads(row['dataset'])
        report = attempt.get('report') or {}
        comparison_spec = _comparison_settings(dataset)
        specs.append(comparison_spec)
        history.append(dict(experiment_id=attempt['id'], session_id=attempt['session_id'],
                            session_label=row['label'], expr=attempt['expr'], status=attempt['status'],
                            period=dataset['period'], score_period=dataset.get('score_period'),
                            score_dates=dataset.get('score_dates'), as_of=dataset['as_of'],
                            snapshot_id=dataset['snapshot_id'], spec_hash=dataset['spec_hash'],
                            settings=comparison_spec, started_at=attempt['started_at'],
                            metrics={k: report.get(k) for k in ('ic', 'icir', 'rank_ic', 'rank_icir',
                                                                 'turnover_1d', 'decay_halflife_days')},
                            reassessment_of=sources.get((attempt['session_id'], attempt['request_id']))))
    history.sort(key=lambda r: (r['period'][0], r['period'][1], r['started_at'], r['experiment_id']))
    total = len(history)
    return dict(program_id=program_id, factor_id=factor_id, attempts=history[offset:offset + limit],
                offset=offset, limit=limit, total=total,
                next_offset=offset + limit if offset + limit < total else None,
                settings_match=bool(specs) and all(s == specs[0] for s in specs),
                caveat='Windows and market data may overlap or change. Matching settings do not establish '
                       'point-in-time data, statistical significance, or factor decay.')


def candidates(ws, program_id, offset=0, limit=20):
    """Factor pool grouped by canonical identity, with session-scoped decisions."""
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError('require offset >= 0 and 1 <= limit <= 100')
    with ws._db() as db:
        db.execute('BEGIN')
        _program(db, program_id)
        attempts = db.execute('SELECT e.payload,f.factor_id,r.canonical,p.label FROM research_program_sessions p '
                              'JOIN experiments e ON e.session_id=p.session_id '
                              'JOIN research_factor_attempts f ON f.experiment_id=e.id '
                              'JOIN factors r ON r.id=f.factor_id WHERE p.program_id=? ORDER BY e.rowid',
                              (program_id,)).fetchall()
        decisions = db.execute('SELECT d.payload FROM research_program_sessions p '
                               'JOIN research_decisions d ON d.session_id=p.session_id '
                               'WHERE p.program_id=? ORDER BY d.rowid', (program_id,)).fetchall()
    pool = {}
    for row in attempts:
        attempt = json.loads(row['payload'])
        factor = pool.setdefault(row['factor_id'], dict(factor_id=row['factor_id'],
                                 canonical=row['canonical'], attempt_count=0, sessions={}))
        factor['attempt_count'] += 1
        session = factor['sessions'].setdefault(attempt['session_id'],
                    dict(session_id=attempt['session_id'], label=row['label'], decision='candidate',
                         experiment_ids=[], evidence_experiment_id=None))
        session['experiment_ids'].append(attempt['id'])
    for row in decisions:
        event = json.loads(row['payload'])
        factor = pool.get(event['factor_id'])
        session = factor and factor['sessions'].get(event['session_id'])
        if session:
            session.update(decision=event['decision'], evidence_experiment_id=event['experiment_id'],
                           decision_id=event['id'], reason=event['reason'])
    result = []
    for fid in sorted(pool):
        factor = pool[fid]
        factor['sessions'] = list(factor['sessions'].values())
        result.append(factor)
    total = len(result)
    return dict(program_id=program_id, factors=result[offset:offset + limit], offset=offset, limit=limit,
                total=total, next_offset=offset + limit if offset + limit < total else None,
                caveat='Decisions belong to individual sessions; later windows do not automatically '
                       'accept, reject or retire a factor in other sessions.')


def compare_windows(ws, program_id, baseline_id, target_id, drop_threshold):
    """Compare signed RankIC in explicitly selected, non-overlapping windows."""
    if type(drop_threshold) not in (int, float) or not math.isfinite(drop_threshold) or drop_threshold <= 0:
        raise ValueError('drop_threshold must be a positive finite RankIC difference')
    if baseline_id == target_id:
        raise ValueError('select two different experiments')
    with ws._db() as db:
        db.execute('BEGIN')
        _program(db, program_id)
        values = []
        for experiment_id in (baseline_id, target_id):
            row = db.execute('SELECT e.payload,d.payload AS dataset FROM experiments e '
                             'JOIN sessions s ON s.id=e.session_id JOIN datasets d ON d.id=s.dataset_id '
                             'JOIN research_program_sessions p ON p.session_id=s.id '
                             'WHERE p.program_id=? AND e.id=?', (program_id, experiment_id)).fetchone()
            if row is None:
                raise ValueError('experiment is not in the research program')
            values.append((json.loads(row['payload']), json.loads(row['dataset'])))
    (old, old_ds), (new, new_ds) = values
    if (old['status'] != 'completed' or new['status'] != 'completed' or not old.get('factor_id')
            or old['factor_id'] != new.get('factor_id')):
        raise ValueError('comparison needs completed evaluations of the same factor')
    old_settings, new_settings = _comparison_settings(old_ds), _comparison_settings(new_ds)
    mismatches = [key for key in old_settings if old_settings[key] != new_settings[key]]
    if mismatches:
        raise ValueError('incompatible evaluation settings: ' + ', '.join(mismatches))
    old_window = old_ds.get('score_period') or old_ds['period']
    new_window = new_ds.get('score_period') or new_ds['period']
    if old_window[1] >= new_window[0]:
        raise ValueError('comparison requires ordered, non-overlapping scoring windows')
    reports = [old.get('report') or {}, new.get('report') or {}]
    definitions = [(a.get('report') or {}).get('rank_ic_definition') or
                   (a.get('panel_report') or {}).get('rank_ic_definition') for a in (old, new)]
    if all(isinstance(d, str) and d.strip() for d in definitions) and definitions[0] != definitions[1]:
        raise ValueError('rank IC definitions differ')
    definition_status = 'matched' if all(isinstance(d, str) and d.strip() for d in definitions) else 'unknown'
    baseline, target = [r.get('rank_ic') for r in reports]
    if any(type(x) not in (int, float) or not math.isfinite(x) for x in (baseline, target)):
        raise ValueError('both experiments need finite RankIC')
    direction = 1 if baseline > 0 else -1 if baseline < 0 else None
    change = direction * (target - baseline) if direction else None
    return dict(program_id=program_id, factor_id=old['factor_id'], baseline_experiment_id=baseline_id,
                target_experiment_id=target_id, baseline_window=old_window, target_window=new_window,
                baseline_rank_ic=baseline, target_rank_ic=target, baseline_direction=direction,
                oriented_change=change, drop_threshold=drop_threshold,
                drop_flag=(change <= -drop_threshold) if change is not None else None,
                metric_definition_status=definition_status,
                label_availability_verified=False, point_in_time_data_verified=False,
                caveat='A descriptive difference with a caller-selected threshold, not a statistical decay '
                       'test. Non-overlapping score windows do not establish forward-label availability or '
                       'a trading-session embargo. Verify point-in-time data, selection, timing and uncertainty.')
