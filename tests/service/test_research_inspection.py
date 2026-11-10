"""Agent inspection is bounded while explicit pages retain persisted evidence."""
import copy
import json
import pytest
from assay.research_inspection import inspect_experiment


def test_summary_excludes_prices_and_security_identifiers_and_preserves_metrics():
    a=dict(id='exp_a',status='completed',report=dict(rank_ic=.1,portfolio=dict(total_return=.02,
        cost_drag=.001,trade_log=[{'symbol':'PRIVATE_TICKER','price':12345}]*5000,
        nav_series=[1.]*5000,nav_dates=['2024-01-01']*5000)),panel_report={'prices':[12345]*5000})
    original=copy.deepcopy(a)
    summary=inspect_experiment(a)
    assert summary['report']['portfolio']['total_return']==.02
    assert summary['report']['portfolio']['cost_drag']==.001
    assert len(json.dumps(summary))<1000 and 'PRIVATE_TICKER' not in json.dumps(summary)
    assert a==original
    page=inspect_experiment(a,section='trades',offset=15,limit=10)
    assert page['total']==5000 and len(page['items'])==10 and page['next_offset']==25
    assert page['items'][0]==a['report']['portfolio']['trade_log'][15]


@pytest.mark.parametrize('kw',[dict(limit=0),dict(limit=101),dict(offset=-1),dict(offset=True),dict(section='full')])
def test_invalid_inspection_bounds_are_rejected(kw):
    with pytest.raises(ValueError):
        inspect_experiment(dict(id='exp_a',status='failed',report=None),**kw)


def test_nav_and_ic_pages_preserve_date_alignment():
    a=dict(id='exp_a',status='completed',report=dict(dates=['a','b'],ic_series=[.1,.2],rank_ic_series=[.2,.3],
        portfolio=dict(nav_dates=['a','b'],nav_series=[1.,1.1])))
    assert inspect_experiment(a,section='nav',offset=1,limit=1)['items']==[dict(date='b',nav=1.1)]
    assert inspect_experiment(a,section='ic',offset=1,limit=1)['items']==[dict(date='b',ic=.2,rank_ic=.3)]


def test_failure_diagnostics_and_nested_metadata_stay_bounded():
    a=dict(id='exp_a',status='failed',error={'details':['large'*2000]*1000},report=dict(
        failure_mode='SYNTAX_ERROR',diagnostics=dict(ok=False,stage_reached='parse',
            errors=[dict(code='UNKNOWN_OP',severity='error',message='bad operator',
                suggestion='use ts_mean',details=['large'*2000]*1000)]*1000,
            stats={'nested':{'deep':{'rows':['large'*2000]*1000}}})))
    summary=inspect_experiment(a)
    diag=summary['report']['diagnostics']
    assert not diag['ok'] and diag['stage_reached']=='parse'
    assert diag['errors']['items'][0]['code']=='UNKNOWN_OP'
    assert diag['errors']['items'][0]['suggestion']=='use ts_mean'
    assert len(json.dumps(summary))<20000
    page=inspect_experiment(a,section='diagnostics',offset=2,limit=2)
    assert page['items'][0]['key']=='errors'
    assert len(json.dumps(page))<30000


def test_misaligned_time_series_is_explicit():
    a=dict(id='exp_a',status='completed',report=dict(dates=['a'],ic_series=[],rank_ic_series=[.1],
        portfolio=dict(nav_dates=['a'],nav_series=[])))
    for section in ('nav','ic'):
        with pytest.raises(ValueError,match='misaligned'):
            inspect_experiment(a,section=section)


def test_factor_history_pages_attempts_and_decisions_without_full_reports():
    from assay.research_inspection import inspect_factor_history
    a=dict(id='exp',status='completed',report=dict(rank_ic=.1,
        portfolio=dict(total_return=.02,trade_log=[{'symbol':'SECRET'}]*5000)))
    history=dict(factor_id='factor',experiments=[a]*200,decisions=[dict(reason='reviewed')]*3)
    page=inspect_factor_history(history,offset=1,limit=2)
    assert len(page['experiments'])==2 and len(page['decisions'])==2
    assert 'SECRET' not in json.dumps(page)
    assert page['history_page']['experiment_total']==200
    assert page['history_page']['decision_total']==3 and page['history_page']['next_offset']==3
    end=inspect_factor_history(history,offset=200,limit=2)
    assert end['experiments']==end['decisions']==[] and not end['history_page']['has_more']
    assert len(a['report']['portfolio']['trade_log'])==5000
