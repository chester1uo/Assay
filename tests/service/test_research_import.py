"""Migration must retain history/budget and reject damaged or conflicting state."""
import copy
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from assay.research import ResearchWorkspace


class Backend:
    def evaluate(self, expr, dataset):
        return {"rank_ic": .1} if expr == "close" else {"failure_mode": "SYNTAX_ERROR"}


def seal(manifest):
    result = copy.deepcopy(manifest)
    result['manifest_hash'] = hashlib.sha256(json.dumps(
        {k: v for k, v in result.items() if k != 'manifest_hash'}, sort_keys=True).encode()).hexdigest()
    return result


@pytest.fixture
def migration(tmp_path):
    source = ResearchWorkspace(tmp_path / 'source.sqlite3', {'test': Backend()})
    ds = source.register_dataset('daily', 'TEST', ['2024-01-01', '2024-06-30'], backend='test')
    sid = source.create_session(ds['id'], 'migrate', max_experiments=4)['id']
    first = source.evaluate(sid, 'close', 'hypothesis', request_id='first')
    source.evaluate(sid, 'invalid', 'failed hypothesis', request_id='failed', parent_id=first['id'])
    source.replay_experiment(first['id'], sid, request_id='replay')
    source.add_note(sid, 'Continue in another workspace')
    target = ResearchWorkspace(tmp_path / 'target.sqlite3')
    return source, target, sid, source.export_session(sid)


def test_migration_restores_exact_history_and_can_continue(migration):
    source, target, sid, manifest = migration
    assert target.import_session(manifest) == source.get_session(sid)
    assert target.export_session(sid) == manifest
    assert target.import_session(manifest) == source.get_session(sid)
    assert not target.research_context(sid)['backend']['registered']
    with pytest.raises(ValueError, match='register backend'):
        target.evaluate(sid, 'close', 'continue', request_id='next')
    assert target.export_session(sid) == manifest
    target.register_backend('test', Backend())
    target.evaluate(sid, 'close', 'continue', request_id='next')
    assert target.get_session(sid)['remaining_experiments'] == 0
    with pytest.raises(ValueError, match='budget exhausted'):
        target.evaluate(sid, 'close', 'more', request_id='over-budget')
    with pytest.raises(ValueError, match='conflicts'):
        target.import_session(manifest)
    assert source.export_session(sid) == manifest


def test_concurrent_import_is_idempotent(migration):
    _, target, sid, manifest = migration
    other = ResearchWorkspace(target.path)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda ws: ws.import_session(manifest), [target, other]))
    assert results[0] == results[1]
    assert len(target.get_session(sid)['experiments']) == 3
    assert len(target.get_session(sid)['notes']) == 1


@pytest.mark.parametrize('change', [
    lambda s: s.update(remaining_experiments=4),
    lambda s: s.update(budget=True),
    lambda s: s['dataset'].update(universe='CHANGED'),
    lambda s: s['experiments'][0].update(status='running', finished_at=None),
    lambda s: s['experiments'][0].update(report=None),
    lambda s: s['experiments'][1].update(request_id='first'),
    lambda s: s['experiments'][1].update(parent_id='missing'),
    lambda s: s['experiments'][2].update(expr='open'),
    lambda s: s['experiments'][0].update(replay_of=s['experiments'][2]['id']),
    lambda s: s['experiments'][1].update(session_id='wrong'),
    lambda s: s['notes'].append({'text': 'missing fields'}),
    lambda s: s.update(experiments={}),
])
def test_rehashed_but_invalid_records_are_rejected_atomically(migration, change):
    _, target, _, manifest = migration
    changed = copy.deepcopy(manifest)
    change(changed['state'])
    with pytest.raises(ValueError):
        target.import_session(seal(changed))
    assert target.list_sessions() == [] and target.list_datasets() == []


def test_bad_integrity_rejected_before_writes(migration):
    _, target, _, manifest = migration
    manifest['state']['goal'] = 'tampered'
    with pytest.raises(ValueError, match='integrity'):
        target.import_session(manifest)
    assert target.list_datasets() == []


def test_identity_conflict_never_overwrites_existing_data(migration):
    _, target, sid, manifest = migration
    target.import_session(manifest)
    different = copy.deepcopy(manifest)
    different['state']['id'] = 'new-session'
    for attempt in different['state']['experiments']:
        attempt['session_id'] = 'new-session'
    with pytest.raises(ValueError, match='experiment identity'):
        target.import_session(seal(different))
    assert len(target.list_sessions()) == 1 and target.export_session(sid) == manifest
    different['state']['experiments'] = []
    different['state']['remaining_experiments'] = 4
    different['state']['dataset']['description'] = 'conflicting dataset'
    with pytest.raises(ValueError, match='dataset identity'):
        target.import_session(seal(different))
    assert target.export_session(sid) == manifest


def test_external_replay_sources_are_validated_in_both_import_orders(migration):
    source, target, sid, manifest = migration
    original = manifest['state']['experiments'][0]
    other_sid = source.create_session(manifest['state']['dataset_id'], 'cross-session')['id']
    source.replay_experiment(original['id'], other_sid, request_id='cross-replay')
    other_manifest = source.export_session(other_sid)
    target.import_session(manifest)
    assert target.import_session(other_manifest) == source.get_session(other_sid)
    corrupted = copy.deepcopy(other_manifest)
    corrupted['state']['experiments'][0]['expr'] = 'open'
    isolated = ResearchWorkspace(target.path.parent / 'external.sqlite3')
    isolated.import_session(manifest)
    with pytest.raises(ValueError, match='replay source conflicts'):
        isolated.import_session(seal(corrupted))
    assert len(isolated.list_sessions()) == 1
    # A source may be absent, but once supplied its meaning must match the link.
    reverse = ResearchWorkspace(target.path.parent / 'reverse.sqlite3')
    reverse.import_session(seal(corrupted))
    with pytest.raises(ValueError, match='replay source conflicts'):
        reverse.import_session(manifest)
    assert len(reverse.list_sessions()) == 1
    valid_reverse = ResearchWorkspace(target.path.parent / 'valid-reverse.sqlite3')
    valid_reverse.import_session(other_manifest)
    assert valid_reverse.import_session(manifest) == source.get_session(sid)


def test_cross_session_replay_cycle_is_rejected(migration):
    source, target, _, manifest = migration
    original = manifest['state']['experiments'][0]
    other_sid = source.create_session(manifest['state']['dataset_id'], 'cross-session')['id']
    replay = source.replay_experiment(original['id'], other_sid, request_id='cross-replay')
    changed = copy.deepcopy(manifest)
    changed['state']['experiments'][0]['replay_of'] = replay['id']
    target.import_session(seal(changed))  # unresolved reference is retained
    with pytest.raises(ValueError, match='cyclic replay'):
        target.import_session(source.export_session(other_sid))
    assert len(target.list_sessions()) == 1
