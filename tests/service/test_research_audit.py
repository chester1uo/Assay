"""Audit catches contradictory evidence without judging alpha quality or executing jobs."""
import copy
import hashlib
import json

import pytest

from assay.research import ResearchWorkspace
from assay.research_audit import audit_manifest


class Backend:
    calls=0
    def evaluate(self,expr,dataset):
        self.calls+=1
        return dict(expr=expr,ic=.2,rank_ic=-.1,icir=2.,rank_icir=-1.,
            dates=['2024-01-02','2024-01-03'],ic_series=[.1,.3],rank_ic_series=[-.2,0.],
            execution=dataset['execution'],universe_id=dataset['universe'])


@pytest.fixture
def research(tmp_path):
    backend=Backend()
    ws=ResearchWorkspace(tmp_path/'research.sqlite3',{'test':backend})
    ds=ws.register_dataset('audit-fixture','NASDAQ100',['2024-01-02','2024-01-05'],
                           backend='test',snapshot_id='fixture-v1')
    sid=ws.create_session(ds['id'],'Review the research evidence',max_experiments=5)['id']
    attempt=ws.evaluate(sid,'ts_mean(close,3)','Test a trend hypothesis',request_id='one')
    return ws,sid,attempt,backend


def rehash(manifest):
    payload={k:v for k,v in manifest.items() if k!='manifest_hash'}
    manifest['manifest_hash']=hashlib.sha256(json.dumps(payload,sort_keys=True,allow_nan=False).encode()).hexdigest()
    return manifest


def codes(result):
    return result['finding_counts']['by_code']


def test_live_audit_is_readonly_deterministic_and_does_not_call_backend(research):
    ws,sid,_,backend=research
    before=ws.export_session(sid)
    with ws._db() as db:
        before_dump=list(db.iterdump())
    result=ws.audit_session(sid)
    assert result==ws.audit_session(sid)
    assert result['status']=='passed'
    assert result['record_integrity']=='passed'
    assert result['assessment']['registry_checked']
    assert result['assessment']['goal_achievement']=='requires_judge'
    assert result['assessment']['profitability']=='not_assessed'
    assert not result['assessment']['execution_replayed'] and not result['assessment']['author_authenticated']
    assert result['summary']['budget']==dict(limit=5,spent=1,remaining=4)
    assert backend.calls==1 and ws.export_session(sid)==before
    with ws._db() as db:
        assert list(db.iterdump())==before_dump
    body={k:v for k,v in result.items() if k!='audit_hash'}
    assert result['audit_hash']==hashlib.sha256(json.dumps(body,sort_keys=True,allow_nan=False).encode()).hexdigest()


@pytest.mark.parametrize('field,value,code',[
    ('ic',.8,'METRIC_SERIES_MISMATCH'),
    ('ic',True,'INVALID_METRIC'),
    ('ic',10**1000,'INVALID_METRIC'),
    ('expr',{'bad':1},'REPORT_EXPRESSION_MISMATCH'),
    ('portfolio',['bad'],'MALFORMED_PORTFOLIO'),
    ('ic_series',[1.1,-.7],'IC_SERIES_OUT_OF_RANGE'),
    ('ic_series',[None,None],'NO_VALID_IC_VALUES'),
    ('dates',['2099-01-02','2099-01-03'],'SERIES_OUTSIDE_SPEC'),
    ('dates',['2024-01-03','2024-01-02'],'SERIES_DATES_INVALID'),
    ('rank_ic_series',[.1],'SERIES_LENGTH_MISMATCH'),
    ('lookahead_detected',True,'LOOKAHEAD_FLAGGED'),
    ('scoring_complete',False,'INCOMPLETE_DIAGNOSTICS'),
])
def test_contradictory_report_evidence_is_rejected_even_after_rehash(research,field,value,code):
    ws,sid,_,_=research
    manifest=copy.deepcopy(ws.export_session(sid))
    manifest['state']['experiments'][0]['report'][field]=value
    result=audit_manifest(rehash(manifest))
    assert result['record_integrity']=='failed'
    assert code in codes(result)
    assert all(row['evidence'] for row in result['findings'])
    json.dumps(result,allow_nan=False)


@pytest.mark.parametrize('nav,total,extra,expected',[
    ([0.,0.],100.,{},'NAV_EVIDENCE_INVALID'),
    ([None,None],100.,{},'NAV_EVIDENCE_INVALID'),
    ([1.,1.1],100.,{},'METRIC_SERIES_MISMATCH'),
    ([1.,1.1],.1,{'sharpe':'excellent'},'INVALID_PORTFOLIO_METRIC'),
])
def test_portfolio_claims_need_valid_saved_nav_and_metrics(research,nav,total,extra,expected):
    ws,sid,_,_=research
    manifest=ws.export_session(sid)
    manifest['state']['experiments'][0]['report']['portfolio']=dict(status='completed',
        nav_dates=['2024-01-02','2024-01-03'],nav_series=nav,total_return=total,**extra)
    result=audit_manifest(rehash(manifest))
    assert result['record_integrity']=='failed' and expected in codes(result)


def test_mutation_hash_budget_and_pinned_manifest(research):
    ws,sid,_,_=research
    original=ws.export_session(sid)
    manifest=copy.deepcopy(original)
    manifest['state']['remaining_experiments']=999
    result=audit_manifest(manifest)
    assert {'MANIFEST_INTEGRITY','BUDGET_MISMATCH'}<=set(codes(result))
    manifest=copy.deepcopy(original)
    manifest['state']['goal']='A different goal'
    result=audit_manifest(rehash(manifest),expected_manifest_hash=original['manifest_hash'])
    assert 'EXPECTED_MANIFEST_MISMATCH' in codes(result)
    assert ws.audit_session(sid,expected_manifest_hash=original['manifest_hash'])['record_integrity']=='passed'


def test_running_can_be_audited_but_cannot_be_imported(research,tmp_path):
    ws,sid,_,_=research
    manifest=ws.export_session(sid)
    manifest['state']['experiments'][0].update(status='running',finished_at=None,report=None,error=None)
    result=audit_manifest(rehash(manifest))
    assert result['record_integrity']=='passed'
    assert result['summary']['completion']=='in_progress'
    assert 'EXPERIMENT_RUNNING' in codes(result)
    with pytest.raises(ValueError,match='running attempts'):
        ResearchWorkspace(tmp_path/'import.sqlite3').import_session(manifest)


def test_failed_attempt_is_retained_without_a_false_audit_failure(research):
    ws,sid,_,_=research
    manifest=ws.export_session(sid)
    manifest['state']['experiments'][0].update(status='failed',report=None,error={'type':'BackendError'})
    result=audit_manifest(rehash(manifest))
    assert result['record_integrity']=='passed' and result['summary']['attempts']['statuses']=={'failed':1}


def test_registry_corruption_is_detected_and_not_repaired(research):
    ws,sid,attempt,_=research
    with ws._db() as db:
        db.execute('DELETE FROM research_factor_attempts WHERE experiment_id=?',(attempt['id'],))
        db.execute('UPDATE experiments SET request_id=? WHERE id=?',('wrong-index',attempt['id']))
    result=ws.audit_session(sid)
    assert {'REGISTRY_LINK_MISMATCH','STORAGE_IDENTITY_MISMATCH'}<=set(codes(result))
    with ws._db() as db:
        assert db.execute('SELECT COUNT(*) FROM research_factor_attempts').fetchone()[0]==0


def test_decision_evidence_and_legacy_factor_identity(research):
    ws,sid,attempt,_=research
    ws.decide_candidate(attempt['id'],'accepted','Inspect saved evidence',request_id='accept')
    manifest=ws.export_session(sid)
    del manifest['state']['experiments'][0]['factor_id']
    result=audit_manifest(rehash(manifest))
    assert result['summary']['candidates'][0]['decision']=='accepted'
    assert result['summary']['unique_factors']==1
    manifest['state']['decisions'][0]['created_at']='2000-01-01T00:00:00+00:00'
    assert 'DECISION_PRECEDES_EVIDENCE' in codes(audit_manifest(rehash(manifest)))


def test_cross_session_replay_resolves_live_but_remains_external_offline(research):
    ws,sid,attempt,_=research
    target=ws.create_session(ws.get_session(sid)['dataset_id'],'Replay review',max_experiments=1)['id']
    ws.replay_experiment(attempt['id'],target,request_id='replay')
    assert 'EXTERNAL_REPLAY_SOURCE' not in codes(ws.audit_session(target))
    assert 'EXTERNAL_REPLAY_SOURCE' in codes(audit_manifest(ws.export_session(target)))


def test_replay_and_parent_time_order_and_running_source(research):
    from datetime import datetime,timedelta
    ws,sid,attempt,_=research
    ws.replay_experiment(attempt['id'],sid,request_id='replay')
    manifest=ws.export_session(sid)
    first,second=manifest['state']['experiments']
    second['started_at']=first['started_at']
    assert 'REPLAY_PRECEDES_SOURCE' in codes(audit_manifest(rehash(manifest)))
    first.update(status='running',report=None,error=None,finished_at=None)
    assert 'REPLAY_SOURCE_NOT_TERMINAL' in codes(audit_manifest(rehash(manifest)))
    manifest=ws.export_session(sid)
    first,second=manifest['state']['experiments']
    second.pop('replay_of')
    second['parent_id']=first['id']
    second['started_at']=(datetime.fromisoformat(first['started_at'])-timedelta(microseconds=1)).isoformat()
    assert 'CHILD_PRECEDES_PARENT' in codes(audit_manifest(rehash(manifest)))


def test_pagination_never_hides_overall_failure_and_checks_all_records(research):
    ws,sid,_,_=research
    manifest=ws.export_session(sid)
    manifest['state']['experiments'][0]['report']['ic']=.9
    result=audit_manifest(rehash(manifest),offset=999,limit=1)
    assert result['findings']==[] and result['record_integrity']=='failed'
    assert result['finding_counts']['errors']>0


@pytest.mark.parametrize('value',[None,[],{}, {'state':None}, {'state':{'id':['bad']}},
                                  {'schema_version':1,'state':{},'manifest_hash':{'bad':1}}])
def test_malformed_envelopes_return_failed_reports_without_mutation(value):
    original=copy.deepcopy(value)
    result=audit_manifest(value)
    assert result['record_integrity']=='failed' and value==original
    json.dumps(result,allow_nan=False)


def test_real_demo_namespaces_cost_warnings_and_mutable_data(tmp_path):
    from assay.demo import prepare_demo
    service,dataset=prepare_demo(tmp_path/'demo')
    sid=service.research.create_session(dataset['id'],'Audit real execution',max_experiments=1)['id']
    attempt=service.research.evaluate(sid,'-cs_rank(ts_returns(close,5))','Synthetic',request_id='a')
    assert attempt['status']=='completed'
    # Compute report IDs and lifecycle factor IDs intentionally use different namespaces.
    result=service.research.audit_session(sid)
    assert result['record_integrity']=='passed',result['findings']
    assert result['status']=='needs_review' and 'MUTABLE_DATA_SPEC' in codes(result)


def test_offline_cli_does_not_overwrite_report(research,tmp_path,capsys):
    from assay.audit import main
    ws,sid,_,_=research
    path=tmp_path/'manifest.json';out=tmp_path/'audit.json'
    path.write_text(json.dumps(ws.export_session(sid)))
    assert main(['--manifest',str(path),'--output',str(out)])==0
    assert json.loads(out.read_text())['record_integrity']=='passed'
    with pytest.raises(SystemExit): main(['--manifest',str(path),'--output',str(out)])
