"""Consistent, atomic migration of a program and all linked research records."""
import hashlib
import json
from datetime import datetime

from assay.research import _json
from assay.research_checkpoints import _text, add_to_state
from assay.research_programs import _internal_request, _program


def _digest(payload):
    return hashlib.sha256(_json(payload).encode()).hexdigest()


def _export(db, ws, program_id):
    program = _program(db, program_id)
    links = [dict(row) for row in db.execute(
        'SELECT session_id,label,linked_at FROM research_program_sessions WHERE program_id=? ORDER BY rowid',
        (program_id,))]
    states = [add_to_state(db, ws._session_state(db, row['session_id'])) for row in links]
    reassessments = [dict(row) for row in db.execute(
        'SELECT target_session_id,request_id,source_experiment_id FROM research_reassessments '
        'WHERE program_id=? ORDER BY rowid', (program_id,))]
    payload = dict(schema_version=1, kind='assay_research_program_manifest', program=program,
                   links=links, sessions=states, reassessments=reassessments)
    return dict(payload, manifest_hash=_digest(payload))


def export(ws, program_id):
    with ws._db() as db:
        db.execute('BEGIN')
        return _export(db, ws, program_id)


def restore(ws, manifest):
    try:
        manifest = json.loads(_json(manifest))
        fields = {'schema_version', 'kind', 'program', 'links', 'sessions', 'reassessments', 'manifest_hash'}
        if (not isinstance(manifest, dict) or set(manifest) != fields or manifest['schema_version'] != 1
                or manifest['kind'] != 'assay_research_program_manifest'
                or manifest['manifest_hash'] != _digest({k: v for k, v in manifest.items() if k != 'manifest_hash'})):
            raise ValueError('invalid program manifest integrity or version')
        program = manifest['program']
        if not isinstance(program, dict) or set(program) != {'id', 'name', 'goal', 'request_id', 'created_at'}:
            raise ValueError('invalid program fields')
        for key, size in [('id', 200), ('name', 200), ('goal', 4000), ('request_id', 200)]:
            _text(program[key], key, size)
        datetime.fromisoformat(program['created_at'])
        links, states, reassessments = (manifest[k] for k in ('links', 'sessions', 'reassessments'))
        if not all(isinstance(x, list) for x in (links, states, reassessments)) or len(links) != len(states):
            raise ValueError('invalid program records')
        ids = []
        for link, state in zip(links, states):
            if not isinstance(link, dict) or set(link) != {'session_id', 'label', 'linked_at'}:
                raise ValueError('invalid program session link')
            _text(link['session_id'], 'session_id')
            if not isinstance(link['label'], str) or len(link['label']) > 200:
                raise ValueError('invalid program session label')
            datetime.fromisoformat(link['linked_at'])
            if not isinstance(state, dict) or state.get('id') != link['session_id']:
                raise ValueError('program session identity mismatch')
            ids.append(link['session_id'])
        if len(ids) != len(set(ids)):
            raise ValueError('duplicate program session')
        keys = set()
        for link in reassessments:
            if not isinstance(link, dict) or set(link) != {'target_session_id', 'request_id', 'source_experiment_id'}:
                raise ValueError('invalid reassessment link')
            for k in link:
                _text(link[k], k)
            key = (link['target_session_id'], link['request_id'])
            if key in keys or link['target_session_id'] not in ids:
                raise ValueError('duplicate or unlinked reassessment')
            keys.add(key)
    except (KeyError, TypeError, OverflowError) as exc:
        raise ValueError('invalid program manifest structure') from exc
    with ws._db() as db:
        db.execute('BEGIN IMMEDIATE')
        existing = db.execute('SELECT id FROM research_programs WHERE id=? OR request_id=?',
                              (program['id'], program['request_id'])).fetchall()
        if existing:
            if len(existing) != 1 or existing[0]['id'] != program['id'] or _export(db, ws, program['id']) != manifest:
                raise ValueError('program identity conflicts with existing state')
            return manifest
        for state in states:
            payload = dict(schema_version=1, kind='assay_research_session_manifest', state=state)
            ws._import_session_transaction(db, dict(payload, manifest_hash=_digest(payload)))
        # Reassessment links can precede evaluation after an interrupted request.
        # When an attempt exists it must identify the same source expression.
        for link in reassessments:
            row = db.execute('SELECT payload FROM experiments WHERE id=?', (link['source_experiment_id'],)).fetchone()
            source = json.loads(row[0]) if row else None
            if (not source or source['session_id'] not in ids or source['session_id'] == link['target_session_id']
                    or source['status'] != 'completed' or not source.get('factor_id')):
                raise ValueError('reassessment source is not a completed program factor')
            target_request = _internal_request(program['id'], link['target_session_id'], link['request_id'])
            row = db.execute('SELECT payload FROM experiments WHERE session_id=? AND request_id=?',
                             (link['target_session_id'], target_request)).fetchone()
            if row:
                target = json.loads(row[0])
                if any(target[k] != source[k] for k in ('expr', 'hypothesis', 'factor_id')):
                    raise ValueError('reassessment target conflicts with source')
            row = db.execute('SELECT 1 FROM research_reassessments WHERE target_session_id=? AND request_id=?',
                             (link['target_session_id'], link['request_id'])).fetchone()
            if row:
                raise ValueError('reassessment identity conflicts with existing state')
        db.execute('INSERT INTO research_programs VALUES (?,?,?)',
                   (program['id'], program['request_id'], _json(program)))
        for link in links:
            db.execute('INSERT INTO research_program_sessions VALUES (?,?,?,?)',
                       (program['id'], link['session_id'], link['label'], link['linked_at']))
        for link in reassessments:
            db.execute('INSERT INTO research_reassessments VALUES (?,?,?,?)',
                       (program['id'], link['target_session_id'], link['request_id'], link['source_experiment_id']))
        if _export(db, ws, program['id']) != manifest:
            raise ValueError('program restore did not reproduce the manifest')
        return manifest
