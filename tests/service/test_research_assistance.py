"""ID recovery and reports must be precise, bounded and read-only."""
import pytest

from assay.research import ResearchWorkspace


class Backend:
    def evaluate(self, expr, dataset):
        if expr == 'bad':
            return {'failure_mode': 'SYNTAX_ERROR', 'suggestion': 'repair'}
        return {'rank_ic': .125, 'ic': .1, 'rank_ic_series': [1, 2, 3]}


@pytest.fixture
def workspace(tmp_path):
    ws = ResearchWorkspace(tmp_path / 'research.sqlite', {'test': Backend()})
    dataset = ws.register_dataset('data', 'TEST', ['2024-01-01', '2024-06-30'], backend='test')
    sid = ws.create_session(dataset['id'], 'research goal', max_experiments=4)['id']
    return ws, dataset, sid


def test_lookup_suggests_but_never_silently_corrects_or_writes(workspace):
    ws, dataset, sid = workspace
    other = ws.create_session(dataset['id'], 'other research')['id']
    original = ws.export_session(sid)
    typo = sid[:-1] + ('0' if sid[-1] != '0' else '1')
    suggestion = ws.resolve_research_id('session', typo)
    assert suggestion['status'] == 'suggestions' and sid in suggestion['candidates']
    assert suggestion['resolved_id'] is None
    assert ws.resolve_research_id('session', sid)['resolved_id'] == sid
    assert ws.resolve_research_id('session', sid[:-2])['resolved_id'] == sid
    ambiguous = ws.resolve_research_id('session', 'research_')
    assert ambiguous['status'] == 'ambiguous_prefix' and set(ambiguous['candidates']) == {sid, other}
    assert ambiguous['resolved_id'] is None
    with pytest.raises(ValueError, match='unknown research session'):
        ws.add_note(typo, 'must not write to a guessed ID')
    assert ws.export_session(sid) == original


def test_lookup_experiments_is_scoped_to_exact_session(workspace):
    ws, dataset, sid = workspace
    one = ws.evaluate(sid, 'close', 'first', request_id='one')
    other = ws.create_session(dataset['id'], 'other session')['id']
    secret = ws.evaluate(other, 'open', 'different session', request_id='one')
    assert ws.resolve_research_id('experiment', one['id'][:-2], session_id=sid)['resolved_id'] == one['id']
    assert secret['id'] not in ws.resolve_research_id('experiment', secret['id'], session_id=sid)['candidates']
    with pytest.raises(ValueError, match='exact session_id'):
        ws.resolve_research_id('experiment', one['id'])
    with pytest.raises(ValueError, match='between 8 and 100'):
        ws.resolve_research_id('session', 'short')


def test_report_uses_saved_exact_ids_including_failures_and_is_bounded(workspace):
    ws, dataset, sid = workspace
    first = ws.evaluate(sid, 'close', 'hypothesis', request_id='one')
    failed = ws.evaluate(sid, 'bad', 'invalid hypothesis', request_id='two')
    other = ws.create_session(dataset['id'], 'other')['id']
    foreign = ws.evaluate(other, 'open', 'other', request_id='one')
    before = ws.export_session(sid)
    report = ws.research_report(sid, experiment_ids=[failed['id'], first['id']])
    assert report['experiment_ids'] == [failed['id'], first['id']]
    assert report['experiments'][0]['status'] == 'failed'
    assert report['experiments'][1]['report']['rank_ic'] == .125
    assert 'rank_ic_series' not in report['experiments'][1]['report']
    latest = ws.research_report(sid, limit=1)
    assert latest['experiment_ids'] == [failed['id']] and latest['truncated']
    with pytest.raises(ValueError, match='exact IDs from this session'):
        ws.research_report(sid, experiment_ids=[foreign['id']])
    with pytest.raises(ValueError, match='exact IDs from this session'):
        ws.research_report(sid, experiment_ids=[first['id'][:-3]])
    with pytest.raises(ValueError, match='unique'):
        ws.research_report(sid, experiment_ids=[first['id'], first['id']])
    assert ws.export_session(sid) == before
