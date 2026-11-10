"""Canonical factor identities and append-only research selection decisions.

The factor registry shares the workspace SQLite transaction. Backend scores are
views of current results; experiment payloads retain every evaluation attempt.
"""
from __future__ import annotations

import json
import math


def validate_report(report):
    """Retain malformed backend evidence as a terminal diagnostic report."""
    scalars = ('rank_ic', 'rank_icir', 'ic', 'icir', 'turnover_1d', 'decay_halflife_days')
    invalid = [key for key in scalars if report.get(key) is not None and
               (type(report[key]) not in (int, float) or not math.isfinite(report[key]))]
    if invalid:
        return dict(report, failure_mode='INVALID_BACKEND_REPORT',
                    suggestion='Expected finite numeric or null metrics: ' + ', '.join(invalid))
    return report


def identity(expr):
    from assay.engine import parse
    from assay.factordb.canonical import canonicalize, factor_id

    try:
        node = canonicalize(parse(expr))
        return factor_id(node), str(node)
    except (ValueError, TypeError, SyntaxError):
        return None, None


def register_attempt(db, attempt):
    """Register identity and attempt link in the caller's transaction."""
    fid, canonical = identity(attempt['expr'])
    if fid is None:
        return None
    import datetime

    created = datetime.datetime.fromisoformat(attempt['started_at']).timestamp()
    db.execute('INSERT OR IGNORE INTO factors (id,canonical,expr,owner,created) VALUES (?,?,?,?,?)',
               (fid, canonical, attempt['expr'], attempt['agent'], created))
    db.execute('INSERT OR IGNORE INTO research_factor_attempts VALUES (?,?)', (attempt['id'], fid))
    db.execute('UPDATE factors SET submits=(SELECT COUNT(*) FROM research_factor_attempts WHERE factor_id=?) WHERE id=?', (fid, fid))
    if attempt.get('parent_id'):
        parent = db.execute('SELECT factor_id FROM research_factor_attempts WHERE experiment_id=?', (attempt['parent_id'],)).fetchone()
        if parent and parent[0] != fid:
            parents = json.loads(db.execute('SELECT parents FROM factors WHERE id=?', (fid,)).fetchone()[0])
            if parent[0] not in parents:
                db.execute('UPDATE factors SET parents=? WHERE id=?', (json.dumps([*parents, parent[0]]), fid))
    dataset = json.loads(db.execute('SELECT payload FROM datasets WHERE id=?', (attempt['dataset_id'],)).fetchone()[0])
    index_attempt(db, attempt, dataset, fid)
    return fid


def index_attempt(db, attempt, dataset, fid=None):
    """Update registry discovery views; the experiment remains the trial history."""
    from datetime import datetime
    from assay.factordb.registry import _METRIC_COLS

    fid = fid or identity(attempt['expr'])[0]
    if fid is None:
        return
    spec = dataset['spec_hash']
    db.execute('INSERT OR IGNORE INTO specs VALUES (?,?,?,?,?,?)',
               (spec, dataset['universe'], *dataset['period'], json.dumps(dataset['horizons']), dataset['execution']))
    terminal = attempt['status'] != 'running'
    status = 'done' if attempt['status'] == 'completed' else attempt['status']
    db.execute('INSERT OR IGNORE INTO jobs (id,owner,spec,status,total,created,factor_ids,exprs,label) '
               'VALUES (?,?,?,?,1,?,?,?,?)', (attempt['id'], attempt['agent'], spec, status,
               datetime.fromisoformat(attempt['started_at']).timestamp(), json.dumps([fid]),
               json.dumps([attempt['expr']]), 'session:' + attempt['session_id']))
    db.execute('UPDATE jobs SET status=?,done=?,failed=?,finished=? WHERE id=?',
               (status, int(status == 'done'), int(status == 'failed'),
                datetime.fromisoformat(attempt['finished_at']).timestamp() if terminal else None, attempt['id']))
    report = attempt.get('report')
    if not terminal or report is None:
        return
    version = (attempt.get('execution') or {}).get('data_version') or dataset.get('snapshot_id') or 'mutable:' + dataset['as_of']
    metrics = dict(report, turnover=report.get('turnover_1d'), coverage=report.get('coverage'),
                   long_short=report.get('long_short'), decay_halflife=report.get('decay_halflife_days'))
    # A late import of an older attempt must not replace a newer discovery score.
    created = datetime.fromisoformat(attempt['finished_at']).timestamp()
    previous = db.execute('SELECT created FROM scores WHERE factor_id=? AND spec=? AND version=?', (fid, spec, version)).fetchone()
    if previous and previous[0] > created:
        return
    db.execute('INSERT OR REPLACE INTO scores (factor_id,spec,version,owner,' + ','.join(_METRIC_COLS) +
               ',metrics,created) VALUES (' + ','.join('?' for _ in range(14)) + ')',
               (fid, spec, version, attempt['agent'], *(v if type(v) in (int, float) and math.isfinite(v) else None
                                                      for v in (metrics.get(k) for k in _METRIC_COLS)),
                json.dumps(metrics, allow_nan=False), created))


def factor_history(workspace, factor_id, session_id=None):
    with workspace._db() as db:
        db.execute('BEGIN')
        factor = db.execute('SELECT * FROM factors WHERE id=?', (factor_id,)).fetchone()
        if factor is None:
            raise ValueError(f'unknown research factor: {factor_id}')
        sql = ('SELECT e.payload FROM experiments e JOIN research_factor_attempts a '
               'ON a.experiment_id=e.id WHERE a.factor_id=?')
        args = [factor_id]
        if session_id is not None:
            workspace._session_state(db, session_id)
            sql += ' AND e.session_id=?'
            args.append(session_id)
        attempts = [json.loads(r[0]) for r in db.execute(sql + ' ORDER BY e.rowid', args)]
        decisions = [json.loads(r[0]) for r in db.execute(
            'SELECT payload FROM research_decisions WHERE factor_id=?' +
            (' AND session_id=?' if session_id is not None else '') + ' ORDER BY rowid', args)]
    return dict(factor_id=factor_id, canonical=factor['canonical'], expr=factor['expr'],
                experiments=attempts, decisions=decisions)


def decide(workspace, experiment_id, decision, reason, request_id, agent):
    """Decisions are per candidate per session, with a durable evidence pointer."""
    from assay.research import _json, _now, _id

    if decision not in {'accepted', 'rejected', 'archived', 'candidate'}:
        raise ValueError('decision must be accepted, rejected, archived or candidate')
    if not all(isinstance(v, str) and v.strip() for v in (reason, request_id, agent)):
        raise ValueError('reason, request_id and agent are required')
    with workspace._db() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT payload FROM experiments WHERE id=?', (experiment_id,)).fetchone()
        if row is None:
            raise ValueError(f'unknown experiment: {experiment_id}')
        attempt = json.loads(row[0])
        previous = db.execute('SELECT payload FROM research_decisions WHERE session_id=? AND request_id=?',
                              (attempt['session_id'], request_id)).fetchone()
        if previous:
            event = json.loads(previous[0])
            if any(event[k] != v for k, v in dict(experiment_id=experiment_id, decision=decision,
                                                  reason=reason, agent=agent).items()):
                raise ValueError('decision request_id belongs to different inputs')
            return event
        if attempt['status'] == 'running':
            raise ValueError('wait for a terminal experiment before deciding')
        if decision == 'accepted' and attempt['status'] != 'completed':
            raise ValueError('only completed experiments can be accepted')
        fid = register_attempt(db, attempt)
        if fid is None:
            raise ValueError('experiment has no valid canonical factor identity')
        event = dict(id=_id('decision_'), session_id=attempt['session_id'], factor_id=fid,
                     experiment_id=experiment_id, decision=decision, reason=reason,
                     request_id=request_id, agent=agent, created_at=_now())
        db.execute('INSERT INTO research_decisions VALUES (?,?,?,?,?,?)',
                   (event['id'], event['session_id'], fid, experiment_id, request_id, _json(event)))
    return event


def candidates(workspace, session_id):
    state = workspace.get_session(session_id)
    by_factor = {}
    for attempt in state['experiments']:
        fid, canonical = identity(attempt['expr'])
        if fid is None:
            continue
        item = by_factor.setdefault(fid, dict(factor_id=fid, canonical=canonical, decision='candidate',
                                             experiment_ids=[], evidence_experiment_id=None))
        item['experiment_ids'].append(attempt['id'])
    for event in state.get('decisions', []):
        item = by_factor[event['factor_id']]
        item.update(decision=event['decision'], reason=event['reason'],
                    evidence_experiment_id=event['experiment_id'], decision_id=event['id'])
    return list(by_factor.values())
