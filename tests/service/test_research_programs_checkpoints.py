"""Persistence, concurrency, and cross-window provenance for search agents."""
import copy
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from assay.research import ResearchWorkspace


class Backend:
    def __init__(self):
        self.calls = []

    def evaluate(self, expr, dataset):
        self.calls.append((expr, dataset['period']))
        return {'ic': .02, 'rank_ic': .03 if dataset['period'][0].startswith('2023') else .01}


@pytest.fixture
def workspace(tmp_path):
    backend = Backend()
    ws = ResearchWorkspace(tmp_path / 'research.sqlite', {'test': backend})
    d1 = ws.register_dataset('old', 'TEST', ['2023-01-01', '2023-12-31'], backend='test')
    d2 = ws.register_dataset('new', 'TEST', ['2024-01-01', '2024-12-31'], backend='test')
    s1 = ws.create_session(d1['id'], 'Mine', max_experiments=6)['id']
    s2 = ws.create_session(d2['id'], 'Reassess', max_experiments=6)['id']
    return ws, backend, s1, s2


def test_checkpoint_retry_conflict_staleness_and_atomic_manifest(workspace, tmp_path):
    ws, _, sid, _ = workspace
    first = ws.evaluate(sid, 'close', 'original', request_id='first')
    revision = ws.research_context(sid)['state_revision']
    tree = {'nodes': [{'factor': first['factor_id'], 'visits': 3, 'value': .2}], 'rng': [1, 2]}
    args = dict(request_id='save-1', expected_revision=0, expected_state_revision=revision)
    saved = ws.save_checkpoint(sid, 'search', 'mcts', tree, **args)
    assert ws.save_checkpoint(sid, 'search', 'mcts', tree, **args) == saved
    assert not ws.get_checkpoint(sid, 'search')['stale']
    assert ws.research_context(sid)['checkpoints'][0]['revision'] == 1
    with pytest.raises(ValueError, match='different inputs'):
        ws.save_checkpoint(sid, 'search', 'mcts', {'nodes': []}, **args)
    with pytest.raises(ValueError, match='revision conflict'):
        ws.save_checkpoint(sid, 'search', 'mcts', tree, request_id='other', expected_revision=0)
    second = ws.evaluate(sid, 'open', 'revision', request_id='second')
    assert ws.get_checkpoint(sid, 'search')['newer_attempt_ids'] == [second['id']]
    with pytest.raises(ValueError, match='research state changed'):
        ws.save_checkpoint(sid, 'search', 'mcts', tree, request_id='save-2', expected_revision=1,
                           expected_state_revision=revision)
    saved2 = ws.save_checkpoint(sid, 'search', 'mcts', {'nodes': [first['id'], second['id']]},
                                request_id='save-2', expected_revision=1)
    assert saved2['revision'] == 2 and not ws.get_checkpoint(sid, 'search')['stale']
    assert ws.get_checkpoint(sid, 'search', revision=1)['stale']
    manifest = ws.export_session(sid)
    assert len(manifest['state']['checkpoints']) == 2
    assert ws.audit_session(sid, expected_manifest_hash=manifest['manifest_hash'])['manifest_hash'] == manifest['manifest_hash']
    restored = ResearchWorkspace(tmp_path / 'restored.sqlite')
    assert restored.import_session(manifest) == manifest['state']
    assert restored.import_session(manifest) == manifest['state']
    assert restored.export_session(sid) == manifest
    assert restored.get_checkpoint(sid, 'search')['checkpoint'] == saved2
    tampered = copy.deepcopy(manifest)
    tampered['state']['checkpoints'][0]['state']['rng'] = [9]
    payload = {k: v for k, v in tampered.items() if k != 'manifest_hash'}
    tampered['manifest_hash'] = hashlib.sha256(json.dumps(payload, sort_keys=True, allow_nan=False).encode()).hexdigest()
    with pytest.raises(ValueError, match='checkpoint state hash'):
        ResearchWorkspace(tmp_path / 'invalid.sqlite').import_session(tampered)


def test_checkpoints_validate_json_and_fence_concurrent_writers(workspace):
    ws, _, sid, _ = workspace
    for state in ({'bad': float('nan')}, {1: 'not json keys'}, {'huge': 'x' * (1024 * 1024)}):
        with pytest.raises(ValueError):
            ws.save_checkpoint(sid, 'search', 'ea', state, request_id='invalid', expected_revision=0)
    def write(i):
        try:
            return ws.save_checkpoint(sid, 'search', 'ea', {'generation': i},
                                      request_id=f'writer-{i}', expected_revision=0)
        except ValueError as exc:
            return str(exc)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(write, [1, 2]))
    assert sum(isinstance(r, dict) for r in results) == 1
    assert any(isinstance(r, str) and 'revision conflict' in r for r in results)
    assert len(ws.list_checkpoints(sid)) == 1


def test_checkpoint_detects_status_change_of_interrupted_attempt(workspace):
    ws, backend, sid, _ = workspace
    def interrupted(expr, dataset):
        raise KeyboardInterrupt()
    backend.evaluate = interrupted
    with pytest.raises(KeyboardInterrupt):
        ws.evaluate(sid, 'close', 'interrupted', request_id='call')
    running = ws.get_session(sid)['experiments'][0]
    saved = ws.save_checkpoint(sid, 'search', 'mcts', {'pending': running['id']},
                               request_id='cp', expected_revision=0)
    assert saved['attempt_watermark'][0]['status'] == 'running'
    ws.abandon_experiment(running['id'], 'worker stopped')
    loaded = ws.get_checkpoint(sid, 'search')
    assert loaded['stale'] and loaded['changed_attempt_ids'] == [running['id']]
    restored = ResearchWorkspace(ws.path.parent / 'after-interruption.sqlite')
    restored.import_session(ws.export_session(sid))
    assert restored.get_checkpoint(sid, 'search')['changed_attempt_ids'] == [running['id']]


def test_checkpoint_detects_notes_and_decisions_without_new_attempts(workspace):
    ws, _, sid, _ = workspace
    attempt = ws.evaluate(sid, 'close', 'test', request_id='attempt')
    ws.save_checkpoint(sid, 'search', 'ea', {'pool': [attempt['id']]}, request_id='cp', expected_revision=0)
    ws.decide_candidate(attempt['id'], 'accepted', 'Retain', request_id='decision')
    result = ws.get_checkpoint(sid, 'search')
    assert result['stale'] and result['state_changed']
    assert result['newer_attempt_ids'] == result['changed_attempt_ids'] == []


def test_program_reassessment_links_windows_and_retries_without_duplicate_budget(workspace):
    ws, backend, old, new = workspace
    program = ws.create_program('alpha', 'Continue research', request_id='program-1')
    assert ws.create_program('alpha', 'Continue research', request_id='program-1') == program
    with pytest.raises(ValueError, match='different inputs'):
        ws.create_program('other', 'Continue research', request_id='program-1')
    pid = program['id']
    ws.attach_program_session(pid, old, label='train')
    ws.attach_program_session(pid, new, label='later')
    assert len(ws.list_programs()) == 1
    assert len(ResearchWorkspace(ws.path).get_program(pid)['sessions']) == 2
    source = ws.evaluate(old, 'close', 'original hypothesis', request_id='first')
    ws.decide_candidate(source['id'], 'accepted', 'Retain for the next window', request_id='decision-1')
    replay = ws.reassess_program_factor(pid, source['id'], new, request_id='later-1')
    assert ws.reassess_program_factor(pid, source['id'], new, request_id='later-1') == replay
    assert len(backend.calls) == 2 and ws.get_session(new)['remaining_experiments'] == 5
    history = ws.program_factor_history(pid, source['factor_id'])
    assert [x['period'][0] for x in history['attempts']] == ['2023-01-01', '2024-01-01']
    assert history['attempts'][1]['reassessment_of'] == source['id']
    assert history['settings_match']
    assert history['attempts'][0]['metrics']['rank_ic'] == .03
    assert history['attempts'][1]['metrics']['rank_ic'] == .01
    pool = ws.program_candidates(pid)
    assert pool['total'] == 1 and pool['factors'][0]['attempt_count'] == 2
    decisions = {s['session_id']: s['decision'] for s in pool['factors'][0]['sessions']}
    assert decisions == {old: 'accepted', new: 'candidate'}
    comparison = ws.compare_program_windows(pid, source['id'], replay['attempt']['id'], drop_threshold=.015)
    assert comparison['drop_flag'] and comparison['oriented_change'] == pytest.approx(-.02)
    assert comparison['label_availability_verified'] is False
    assert comparison['point_in_time_data_verified'] is False
    assert comparison['metric_definition_status'] == 'unknown'
    with pytest.raises(ValueError, match='ordered, non-overlapping'):
        ws.compare_program_windows(pid, replay['attempt']['id'], source['id'], drop_threshold=.015)
    with pytest.raises(ValueError, match='positive finite'):
        ws.compare_program_windows(pid, source['id'], replay['attempt']['id'], drop_threshold=float('nan'))
    with pytest.raises(ValueError, match='different target'):
        ws.reassess_program_factor(pid, source['id'], old, request_id='same-window')
    outside = ws.create_session(ws.get_session(new)['dataset_id'], 'Outside')['id']
    with pytest.raises(ValueError, match='not in the research program'):
        ws.reassess_program_factor(pid, source['id'], outside, request_id='outside')


def test_scoped_reports_preserve_and_compare_rank_ic_definition(tmp_path):
    class ChangingDefinition:
        def evaluate(self, expr, dataset):
            days = dataset['score_dates']
            return {'dates': days, 'ic_series': [.02] * len(days), 'rank_ic_series': [.03] * len(days),
                    'rank_ic_definition': 'definition-' + dataset['period'][0][:4]}
    ws = ResearchWorkspace(tmp_path / 'definition.sqlite', {'test': ChangingDefinition()})
    ds1 = ws.register_dataset('one', 'TEST', ['2023-01-01', '2023-01-31'], backend='test', horizons=[1],
        score_period=['2023-01-02', '2023-01-03'], score_dates=['2023-01-02', '2023-01-03'])
    ds2 = ws.register_dataset('two', 'TEST', ['2024-01-01', '2024-01-31'], backend='test', horizons=[1],
        score_period=['2024-01-02', '2024-01-03'], score_dates=['2024-01-02', '2024-01-03'])
    s1, s2 = [ws.create_session(d['id'], 'definition')['id'] for d in (ds1, ds2)]
    pid = ws.create_program('definition', 'compare', request_id='p')['id']
    ws.attach_program_session(pid, s1); ws.attach_program_session(pid, s2)
    old = ws.evaluate(s1, 'close', 'h', request_id='a')
    new = ws.evaluate(s2, 'close', 'h', request_id='b')
    assert old['report']['rank_ic_definition'] == 'definition-2023'
    assert new['report']['rank_ic_definition'] == 'definition-2024'
    with pytest.raises(ValueError, match='definitions differ'):
        ws.compare_program_windows(pid, old['id'], new['id'], drop_threshold=.01)
    # Historical scoped reports may lack the copied field; the original panel is retained.
    with ws._db() as db:
        for attempt in (old, new):
            attempt['report'].pop('rank_ic_definition')
            db.execute('UPDATE experiments SET payload=? WHERE id=?', (json.dumps(attempt), attempt['id']))
    with pytest.raises(ValueError, match='definitions differ'):
        ws.compare_program_windows(pid, old['id'], new['id'], drop_threshold=.01)


def test_portfolio_dates_do_not_make_windows_incompatible(tmp_path):
    from assay.demo import prepare_demo
    service, _ = prepare_demo(tmp_path / 'market')
    ws = service.research
    ds1 = ws.register_dataset('one', 'NASDAQ100', ['2024-01-02', '2024-03-15'], horizons=[1],
                              portfolio={'rebalance_type': 'weekly'})
    ds2 = ws.register_dataset('two', 'NASDAQ100', ['2024-03-18', '2024-06-14'], horizons=[1],
                              portfolio={'rebalance_type': 'weekly'})
    s1, s2 = [ws.create_session(d['id'], 'compare')['id'] for d in (ds1, ds2)]
    pid = ws.create_program('portfolio', 'compare', request_id='p')['id']
    ws.attach_program_session(pid, s1); ws.attach_program_session(pid, s2)
    old = ws.evaluate(s1, 'ts_returns(close,3)', 'h', request_id='a')
    new = ws.evaluate(s2, 'ts_returns(close,3)', 'h', request_id='b')
    assert old['status'] == new['status'] == 'completed'
    comparison = ws.compare_program_windows(pid, old['id'], new['id'], drop_threshold=.01)
    assert comparison['metric_definition_status'] == 'matched'
    assert ws.program_factor_history(pid, old['factor_id'])['settings_match']


def test_program_manifest_roundtrip_and_atomic_rejection(workspace, tmp_path):
    ws, _, old, new = workspace
    pid = ws.create_program('portable', 'Continue research', request_id='program')['id']
    ws.attach_program_session(pid, old, label='earlier')
    ws.attach_program_session(pid, new, label='later')
    source = ws.evaluate(old, 'close', 'hypothesis', request_id='source')
    ws.decide_candidate(source['id'], 'accepted', 'Keep', request_id='decision')
    ws.save_checkpoint(old, 'mcts', 'tree', {'nodes': [source['factor_id']]},
                       request_id='cp', expected_revision=0)
    target = ws.reassess_program_factor(pid, source['id'], new, request_id='reassess')['attempt']
    manifest = ws.export_program(pid)
    restored = ResearchWorkspace(tmp_path / 'portable.sqlite', {'test': Backend()})
    assert restored.import_program(manifest) == manifest
    assert restored.import_program(manifest) == manifest
    assert restored.export_program(pid) == manifest
    assert restored.program_candidates(pid) == ws.program_candidates(pid)
    assert restored.program_factor_history(pid, source['factor_id']) == ws.program_factor_history(pid, source['factor_id'])
    assert restored.get_checkpoint(old, 'mcts')['checkpoint']['state']['nodes'] == [source['factor_id']]
    assert restored.reassess_program_factor(pid, source['id'], new, request_id='reassess')['attempt']['id'] == target['id']
    assert restored.get_session(new)['remaining_experiments'] == 5

    tampered = copy.deepcopy(manifest)
    tampered['reassessments'][0]['source_experiment_id'] = 'missing'
    payload = {k: v for k, v in tampered.items() if k != 'manifest_hash'}
    tampered['manifest_hash'] = hashlib.sha256(json.dumps(payload, sort_keys=True, allow_nan=False).encode()).hexdigest()
    empty = ResearchWorkspace(tmp_path / 'atomic.sqlite')
    with pytest.raises(ValueError, match='source'):
        empty.import_program(tampered)
    assert empty.list_sessions() == [] and empty.list_programs() == [] and empty.list_datasets() == []
    bad_hash = copy.deepcopy(manifest)
    bad_hash['program']['name'] = 'tampered'
    with pytest.raises(ValueError, match='integrity'):
        empty.import_program(bad_hash)

    ws.add_note(new, 'changed after export')
    with pytest.raises(ValueError, match='conflicts'):
        ws.import_program(manifest)


def test_import_never_overwrites_existing_factordb_job(workspace, tmp_path):
    ws, _, sid, _ = workspace
    attempt = ws.evaluate(sid, 'close', 'hypothesis', request_id='source')
    pid = ws.create_program('portable', 'Continue', request_id='program')['id']
    ws.attach_program_session(pid, sid)
    session_manifest, program_manifest = ws.export_session(sid), ws.export_program(pid)
    for kind, manifest in [('session', session_manifest), ('program', program_manifest)]:
        restored = ResearchWorkspace(tmp_path / (kind + '-job.sqlite'))
        with restored._db() as db:
            db.execute("INSERT INTO jobs(id,owner,spec,status,total,created,label) VALUES (?,?,?,?,?,?,?)",
                       (attempt['id'], 'other', 'independent', 'running', 9, 1., 'external'))
            before = dict(db.execute('SELECT * FROM jobs WHERE id=?', (attempt['id'],)).fetchone())
        with pytest.raises(ValueError, match='existing FactorDB job'):
            getattr(restored, 'import_' + kind)(manifest)
        with restored._db() as db:
            after = dict(db.execute('SELECT * FROM jobs WHERE id=?', (attempt['id'],)).fetchone())
        assert before == after
        assert restored.list_sessions() == [] and restored.list_programs() == []
