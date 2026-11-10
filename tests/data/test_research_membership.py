"""Historical membership must survive removals and affect cross-sectional inputs."""
from datetime import date
import numpy as np
import polars as pl

from assay.config import AssayConfig
from assay.data.schemas import UNIVERSE_SNAPSHOTS_SCHEMA, universe_snapshots_path
from assay.data.store import DataStore
from assay.engine import FactorEngine


def test_historical_union_and_membership_respect_effective_dates_and_cutoff(tmp_path):
    path = universe_snapshots_path(tmp_path,'US')
    path.parent.mkdir(parents=True)
    pl.DataFrame([
        dict(index_id='NASDAQ100',effective_date=date(2024,1,1),as_of_date=date(2024,1,1),symbols=['A','B']),
        dict(index_id='NASDAQ100',effective_date=date(2024,1,3),as_of_date=date(2024,1,3),symbols=['B','C']),
        dict(index_id='NASDAQ100',effective_date=date(2024,1,3),as_of_date=date(2024,2,1),symbols=['B','D']),
    ], schema=UNIVERSE_SNAPSHOTS_SCHEMA).write_parquet(path)
    store=DataStore(AssayConfig.for_tests(tmp_path))
    assert store.universe_span('NASDAQ100','2024-01-01','2024-01-04','2024-01-04') == ['A','B','C']
    assert store.universe_span('NASDAQ100','2024-01-01','2024-01-04','2024-02-01') == ['A','B','D']
    dates=np.array(['2024-01-01','2024-01-04'],dtype='datetime64[D]')
    membership=store.get_membership('NASDAQ100',dates,['A','B','C'],'2024-01-04')
    np.testing.assert_array_equal(membership,[[True,True,False],[False,True,True]])


def test_cross_section_excludes_nonmembers_but_retains_rolling_history():
    dates=np.array(['2024-01-01','2024-01-02'],dtype='datetime64[D]')
    engine=FactorEngine.from_matrices(dates,np.array(['A','B','C']),{'close':np.array([[1.,2.,100.],[1.,2.,100.]])})
    engine.membership=np.array([[True,True,False],[False,True,True]])
    want=np.array([[-.5,.5,np.nan],[np.nan,-49.,49.]])
    np.testing.assert_allclose(engine.evaluate('cs_demean(close)').values,want,equal_nan=True)
    np.testing.assert_allclose(engine.evaluate_many(['cs_demean(close)'])[0].values,want,equal_nan=True)
    assert engine.evaluate('ts_mean(close,2)').values[1,2] == 100.
    before=engine.panel_fingerprint()
    engine.membership[0,2]=True
    assert engine.panel_fingerprint() != before


def test_same_axes_with_restated_values_have_different_cache_identity():
    dates=np.array(['2024-01-01','2024-01-02'],dtype='datetime64[D]')
    a=FactorEngine.from_matrices(dates,np.array(['A']),{'close':np.array([[1.],[2.]])})
    b=FactorEngine.from_matrices(dates,np.array(['A']),{'close':np.array([[1.],[3.]])})
    assert a.panel_fingerprint() != b.panel_fingerprint()


def test_precomputed_subtrees_retain_pre_admission_rolling_history(tmp_path):
    from assay.engine import PrecomputeStore
    dates=np.arange('2024-01-01','2024-01-05',dtype='datetime64[D]')
    engine=FactorEngine.from_matrices(dates,np.array(['A']),{'close':np.arange(1.,5.).reshape(-1,1)})
    engine.membership=np.array([[False],[False],[True],[True]])
    expr='ts_mean(ts_mean(close,2),2)'
    want=engine.evaluate(expr).values
    store=PrecomputeStore(tmp_path/'precompute')
    assert store.build(engine,['ts_mean(close,2)']*3)['built']
    got=engine.evaluate_many([expr],precompute=store.bind(engine.panel_fingerprint()))[0].values
    np.testing.assert_allclose(got,want,equal_nan=True)


def test_group_labels_participate_in_cache_content_identity():
    dates=np.arange('2024-01-01','2024-01-04',dtype='datetime64[D]')
    symbols=np.array(['A','B','C','D'])
    data={'close':np.tile([1.,2.,100.,200.],(3,1))}
    a=FactorEngine.from_matrices(dates,symbols,data,group_data={'sector':dict(zip(symbols,[1,1,2,2]))})
    b=FactorEngine.from_matrices(dates,symbols,data,group_data={'sector':dict(zip(symbols,[1,2,1,2]))})
    assert a.panel_fingerprint() != b.panel_fingerprint()
