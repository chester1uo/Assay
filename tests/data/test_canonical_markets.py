"""Canonical snapshots must route and retain market-specific research identities."""
from datetime import date,timedelta
import dataclasses
import numpy as np
import polars as pl
import pytest

from assay.config import AssayConfig
from assay.data.schemas import PRICE_RAW_SCHEMA,UNIVERSE_SNAPSHOTS_SCHEMA,price_partition_path,universe_snapshots_path
from assay.data.store.canonical import CanonicalPanelStore,publish_snapshot
from assay.data.markets import MarketStores
from assay.factordb import service
from assay.integrations.factordb import FactorDBResearchBackend
from assay.research import ResearchWorkspace
from assay.ades.worker import Worker
from tests.ades.test_ades import controller,_ClientTransport


def canonical_fixture(root):
    for market,universe in [('CN','CSI300'),('HK','HSI')]:
        rows=[]
        symbols=[f'{market}{i}' for i in range(6)]
        for t in range(10):
            day=date(2024,12,2)+timedelta(days=t)
            for i,symbol in enumerate(symbols):
                px=float(10+i+(t+1)*(i+1)/100)
                rows.append(dict(date=day,symbol=symbol,open=px,high=px+1,low=px-1,close=px+.1,
                    volume=1000.+i*t,transactions=10,as_of_date=day,source_id='synthetic'))
        path=price_partition_path(root,market,2024,12);path.parent.mkdir(parents=True,exist_ok=True)
        pl.DataFrame(rows,schema=PRICE_RAW_SCHEMA).write_parquet(path)
        path=universe_snapshots_path(root,market);path.parent.mkdir(parents=True,exist_ok=True)
        pl.DataFrame([dict(index_id=universe,effective_date=date(2024,12,2),as_of_date=date(2024,12,2),symbols=symbols)],schema=UNIVERSE_SNAPSHOTS_SCHEMA).write_parquet(path)
    return publish_snapshot(root)


def test_canonical_snapshot_and_market_axis_are_verified(tmp_path):
    metadata=canonical_fixture(tmp_path)
    cfg=dataclasses.replace(AssayConfig.for_tests(tmp_path),market='CN')
    store=CanonicalPanelStore(cfg)
    assert store.snapshot_id==metadata['markets']['CN']['version']
    dates,symbols,values=store.get_matrices(['close'],store.get_universe('CSI300','2024-12-11','2024-12-11'),
        '2024-12-02','2024-12-11','2024-12-11','none')
    assert values['close'].shape==(10,6) and len(dates)==10 and all(s.startswith('CN') for s in symbols)
    path=price_partition_path(tmp_path,'CN',2024,12)
    pl.read_parquet(path).with_columns((pl.col('close')+1).alias('close')).write_parquet(path)
    with pytest.raises(ValueError,match='changed without publishing'):
        store.refresh()


@pytest.mark.parametrize('market,universe',[('CN','CSI300'),('HK','HSI')])
def test_market_research_runs_on_matching_snapshot(controller,tmp_path,monkeypatch,market,universe):
    client,_=controller
    root=tmp_path/'canonical';canonical_fixture(root)
    monkeypatch.setenv('ASSAY_DATA_DIR_CN',str(root));monkeypatch.setenv('ASSAY_DATA_DIR_HK',str(root))
    svc=service.get()
    versions=svc.stores.versions()
    assert set(versions)=={'US','CN','HK'}
    ws=ResearchWorkspace(tmp_path/'research.sqlite3',{'factordb_ades':FactorDBResearchBackend(lambda:svc)})
    dataset=ws.register_dataset('market','%s'%universe,['2024-12-02','2024-12-11'],horizons=[1],adj='total',
        backend='factordb_ades',snapshot_id=versions[market])
    session=ws.create_session(dataset['id'],'deployment compatibility',max_experiments=1)
    attempt=ws.evaluate(session['id'],'cs_rank(close)','market-specific synthetic fixture',request_id='fixed')
    assert attempt['status']=='running' and attempt['execution']['data_version']==versions[market]
    worker=Worker(_ClientTransport(client,'wk-1'),svc.stores,'multi-market')
    assert worker.run_once()
    done=ws.reconcile_experiment(attempt['id'])
    assert done['status']=='completed' and done['report']['data_version']==versions[market]
    assert svc.reg.job(attempt['execution']['job_id'])['version']==versions[market]
    assert client.get('/v1/fdb/markets').status_code==200
    assert svc.values('cs_rank(close)',universe=universe,date='2024-12-11')['n']==6
