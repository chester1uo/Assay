"""Versioned, JSON-only algorithm checkpoints attached to a research session."""
from __future__ import annotations

import hashlib
import json

MAX_STATE_BYTES = 1024 * 1024
FIELDS = {'id', 'session_id', 'name', 'algorithm', 'revision', 'base_revision',
          'request_id', 'state', 'state_hash', 'dataset_spec_hash', 'attempt_watermark', 'created_at',
          'session_state_revision', 'expected_state_revision'}


def _encode(value):
    try:
        return json.dumps(value, sort_keys=True, allow_nan=False, separators=(',', ':'))
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError('checkpoint state must be finite JSON') from exc


def _text(value, name, limit=200):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f'{name} must be a nonempty string of at most {limit} characters')
    return value


def _validate_payload(state):
    if not isinstance(state, dict):
        raise ValueError('checkpoint state must be a JSON object')
    encoded = _encode(state)
    if len(encoded.encode('utf-8')) > MAX_STATE_BYTES:
        raise ValueError('checkpoint state exceeds 1 MiB')
    # Round-trip rejects implicit non-string keys and makes stored data portable.
    copied = json.loads(encoded)
    if copied != state:
        raise ValueError('checkpoint state must use JSON object keys')
    return copied, hashlib.sha256(encoded.encode()).hexdigest()


def _watermark(db, session_id):
    rows = db.execute('SELECT payload FROM experiments WHERE session_id=? ORDER BY rowid', (session_id,))
    return [{'id': a['id'], 'status': a['status']} for a in (json.loads(r[0]) for r in rows)]


def save(ws, session_id, name, algorithm, state, request_id, expected_revision, expected_state_revision=None):
    from assay.research import _id, _json, _now
    _text(session_id, 'session_id'); _text(name, 'name', 100)
    _text(algorithm, 'algorithm', 100); _text(request_id, 'request_id')
    if type(expected_revision) is not int or expected_revision < 0:
        raise ValueError('expected_revision must be a nonnegative integer')
    if expected_state_revision is not None and (not isinstance(expected_state_revision, str)
            or len(expected_state_revision) != 64 or any(c not in '0123456789abcdef' for c in expected_state_revision)):
        raise ValueError('expected_state_revision must be a lowercase SHA-256 digest')
    state, digest = _validate_payload(state)
    with ws._db() as db:
        db.execute('BEGIN IMMEDIATE')
        session = db.execute('SELECT dataset_id FROM sessions WHERE id=?', (session_id,)).fetchone()
        if session is None:
            raise ValueError(f'unknown research session: {session_id}')
        previous = db.execute('SELECT payload FROM research_checkpoints WHERE session_id=? AND name=? AND request_id=?',
                              (session_id, name, request_id)).fetchone()
        if previous:
            record = json.loads(previous[0])
            if (record['algorithm'], record['state'], record['base_revision'], record['expected_state_revision']) != (
                    algorithm, state, expected_revision, expected_state_revision):
                raise ValueError('checkpoint request_id was already used with different inputs')
            return record
        latest = db.execute('SELECT payload FROM research_checkpoints WHERE session_id=? AND name=? '
                            'ORDER BY revision DESC LIMIT 1', (session_id, name)).fetchone()
        current = json.loads(latest[0]) if latest else None
        if current and current['algorithm'] != algorithm:
            raise ValueError('checkpoint name is already bound to another algorithm')
        actual = current['revision'] if current else 0
        if expected_revision != actual:
            raise ValueError(f'checkpoint revision conflict: expected {expected_revision}, current {actual}')
        state_revision = hashlib.sha256(_json(ws._session_state(db, session_id)).encode()).hexdigest()
        if expected_state_revision is not None and expected_state_revision != state_revision:
            raise ValueError('research state changed since the expected revision')
        dataset = json.loads(db.execute('SELECT payload FROM datasets WHERE id=?', (session['dataset_id'],)).fetchone()[0])
        record = dict(id=_id('checkpoint_'), session_id=session_id, name=name, algorithm=algorithm,
                      revision=actual + 1, base_revision=actual, request_id=request_id, state=state,
                      state_hash=digest, dataset_spec_hash=dataset['spec_hash'],
                      attempt_watermark=_watermark(db, session_id), created_at=_now(),
                      session_state_revision=state_revision, expected_state_revision=expected_state_revision)
        db.execute('INSERT INTO research_checkpoints VALUES (?,?,?,?,?,?)',
                   (record['id'], session_id, name, record['revision'], request_id, _json(record)))
    return record


def get(ws, session_id, name, revision=None):
    from assay.research import _json
    _text(session_id, 'session_id'); _text(name, 'name', 100)
    if revision is not None and (type(revision) is not int or revision < 1):
        raise ValueError('revision must be a positive integer')
    with ws._db() as db:
        db.execute('BEGIN')
        if not db.execute('SELECT 1 FROM sessions WHERE id=?', (session_id,)).fetchone():
            raise ValueError(f'unknown research session: {session_id}')
        row = db.execute('SELECT payload FROM research_checkpoints WHERE session_id=? AND name=? '
                         + ('AND revision=?' if revision is not None else 'ORDER BY revision DESC LIMIT 1'),
                         (session_id, name, revision) if revision is not None else (session_id, name)).fetchone()
        if not row:
            raise ValueError('unknown checkpoint')
        record = json.loads(row[0])
        now = _watermark(db, session_id)
        state_revision = hashlib.sha256(_json(ws._session_state(db, session_id)).encode()).hexdigest()
    old = record['attempt_watermark']
    changed = [a['id'] for a, b in zip(now, old) if a != b]
    return dict(checkpoint=record, newer_attempt_ids=[r['id'] for r in now[len(old):]],
                changed_attempt_ids=changed, current_attempt_count=len(now),
                current_state_revision=state_revision,
                state_changed=state_revision != record['session_state_revision'],
                stale=(state_revision != record['session_state_revision']))


def list_latest(ws, session_id):
    with ws._db() as db:
        if not db.execute('SELECT 1 FROM sessions WHERE id=?', (session_id,)).fetchone():
            raise ValueError(f'unknown research session: {session_id}')
        rows = db.execute('SELECT c.payload FROM research_checkpoints c JOIN '
                          '(SELECT name, MAX(revision) revision FROM research_checkpoints '
                          'WHERE session_id=? GROUP BY name) latest '
                          'ON c.name=latest.name AND c.revision=latest.revision '
                          'WHERE c.session_id=? ORDER BY c.name', (session_id, session_id)).fetchall()
    return [{k: v for k, v in json.loads(row[0]).items() if k not in ('state', 'attempt_watermark')}
            for row in rows]


def add_to_state(db, state):
    rows = db.execute('SELECT payload FROM research_checkpoints WHERE session_id=? ORDER BY rowid',
                      (state['id'],)).fetchall()
    if rows:
        state['checkpoints'] = [json.loads(r[0]) for r in rows]
    return state


def validate_checkpoints(state):
    from datetime import datetime
    rows = state.get('checkpoints', [])
    if not isinstance(rows, list) or ('checkpoints' in state and not rows):
        raise ValueError('invalid session manifest: checkpoint records')
    streams, ids, requests = {}, set(), set()
    previous_watermark = []
    attempts = state['experiments']
    for row in rows:
        if not isinstance(row, dict) or set(row) != FIELDS:
            raise ValueError('invalid session manifest: checkpoint fields')
        for key, limit in [('id', 200), ('name', 100), ('algorithm', 100), ('request_id', 200)]:
            _text(row[key], key, limit)
        if row['session_id'] != state['id'] or row['dataset_spec_hash'] != state['dataset']['spec_hash']:
            raise ValueError('invalid session manifest: checkpoint session or dataset')
        for key in ('session_state_revision', 'expected_state_revision'):
            value = row[key]
            if value is None and key == 'expected_state_revision':
                continue
            if not isinstance(value, str) or len(value) != 64 or any(c not in '0123456789abcdef' for c in value):
                raise ValueError('invalid session manifest: checkpoint session state revision')
        if type(row['revision']) is not int or type(row['base_revision']) is not int:
            raise ValueError('invalid session manifest: checkpoint revision')
        old_rev, old_algorithm = streams.get(row['name'], (0, row['algorithm']))
        if row['base_revision'] != old_rev or row['revision'] != old_rev + 1 or row['algorithm'] != old_algorithm:
            raise ValueError('invalid session manifest: checkpoint stream')
        key = (row['name'], row['request_id'])
        if row['id'] in ids or key in requests:
            raise ValueError('invalid session manifest: duplicate checkpoint identity')
        ids.add(row['id']); requests.add(key)
        streams[row['name']] = (row['revision'], row['algorithm'])
        _, digest = _validate_payload(row['state'])
        if row['state_hash'] != digest:
            raise ValueError('invalid session manifest: checkpoint state hash')
        if not isinstance(row['created_at'], str):
            raise ValueError('invalid session manifest: checkpoint timestamp')
        datetime.fromisoformat(row['created_at'])
        watermark = row['attempt_watermark']
        if not isinstance(watermark, list) or len(watermark) > len(attempts):
            raise ValueError('invalid session manifest: checkpoint watermark')
        for observed, attempt in zip(watermark, attempts):
            if (not isinstance(observed, dict) or set(observed) != {'id', 'status'}
                    or observed['id'] != attempt['id']
                    or observed['status'] not in ('running', 'completed', 'failed', 'cancelled', 'abandoned')
                    or (observed['status'] != 'running' and observed['status'] != attempt['status'])):
                raise ValueError('invalid session manifest: checkpoint attempt watermark')
        if len(watermark) < len(previous_watermark):
            raise ValueError('invalid session manifest: checkpoint watermark regressed')
        for previous, current in zip(previous_watermark, watermark):
            if previous['id'] != current['id'] or (previous['status'] != 'running' and previous != current):
                raise ValueError('invalid session manifest: checkpoint watermark changed terminal attempt')
        previous_watermark = watermark
