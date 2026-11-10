"""Cached research retains fresh-input semantics and independent attempt histories."""
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

import numpy as np
import polars as pl

from assay.cache import L2FactorCache
from assay.engine import FactorEngine, operators
from assay.research import AssayBackend
from assay.research_execution import FactorValueCache


def engine():
    rng=np.random.default_rng(33)
    return FactorEngine.from_matrices(
        [date(2024,1,1)+timedelta(days=i) for i in range(20)], ['A','B','C'],
        {'close':rng.uniform(10,20,(20,3)), 'volume':rng.uniform(100,200,(20,3))})


def test_raw_subtrees_survive_membership_admission_and_disk_restart(tmp_path):
    cold=engine()
    cold.membership=np.ones(cold._shape,dtype=bool)
    cold.membership[:8,0]=False
    expr='ts_mean(close,3)'
    expected=cold.evaluate(expr).values
    disk=L2FactorCache(tmp_path/'values')
    for _ in range(2):
        cached=engine()
        cached.membership=cold.membership.copy()
        cached.value_cache=FactorValueCache(disk,cached.content_fingerprint(),max_bytes=1000)
        np.testing.assert_array_equal(cached.evaluate(expr).values,expected)
        np.testing.assert_array_equal(cached.evaluate('ts_mean(ts_mean(close,3),2)').values,
                                      cold.evaluate('ts_mean(ts_mean(close,3),2)').values)
        assert cached.value_cache.memo.bytes<=1000
    assert cached.value_cache.disk_hits>=1
    assert np.isfinite(expected[8,0])


def test_custom_operator_changes_are_never_hidden_by_cache(tmp_path):
    e=engine()
    e.value_cache=FactorValueCache(L2FactorCache(tmp_path),e.content_fingerprint())
    factor=[1.]
    operators.register('dynamic_test',lambda x:x*factor[0],1,1)
    try:
        first=e.evaluate('dynamic_test(close)').values.copy()
        factor[0]=2.
        np.testing.assert_array_equal(e.evaluate('dynamic_test(close)').values,first*2)
    finally:
        operators.unregister('dynamic_test')


def test_wrapped_builtin_replacement_cannot_hit_old_disk_cache(tmp_path):
    from functools import wraps
    e=engine()
    cache=FactorValueCache(L2FactorCache(tmp_path),e.content_fingerprint())
    e.value_cache=cache
    original=operators.get('add')
    first=e.evaluate('add(close,1)').values.copy()
    @wraps(original.fn)
    def changed(x,y):
        return original.fn(x,y)+100
    operators.register('add',changed,2,2,**original.schema)
    try:
        np.testing.assert_array_equal(e.evaluate('add(close,1)').values,first+100)
    finally:
        operators.register('add',original.fn,original.min_args,original.max_args,
                           needs_ctx=original.needs_ctx,**original.schema)


def test_research_reuses_panels_but_reloads_changed_market_files(tmp_path,monkeypatch):
    from assay.demo import prepare_demo
    service,ds=prepare_demo(tmp_path/'market')
    backend=service.research.backends['assay']
    calls=[]
    build=service._build_engine
    def counted(*a,**k):
        calls.append(1)
        return build(*a,**k)
    monkeypatch.setattr(service,'_build_engine',counted)
    sid=service.research.create_session(ds['id'],'cache coherence',max_experiments=4)['id']
    first=service.research.evaluate(sid,'ts_returns(close,3)','first',request_id='a')
    second=service.research.evaluate(sid,'ts_returns(close,5)','second',request_id='b')
    assert first['status']==second['status']=='completed'
    assert len(calls)==1 and backend.execution_pool.stats()['panel_hits']==1
    assert first['id']!=second['id']
    path=next((tmp_path/'market'/'price_raw').rglob('*.parquet'))
    raw=pl.read_parquet(path)
    raw.with_columns((pl.col('close')*1.02).alias('close')).write_parquet(path)
    third=service.research.evaluate(sid,'ts_returns(close,3)','new source',request_id='c')
    assert third['status']=='completed' and len(calls)==2
    assert third['report']['panel_content_hash']!=first['report']['panel_content_hash']
    cold=AssayBackend(service,reuse_panels=False).evaluate('ts_returns(close,3)',ds)
    assert third['report']['rank_ic']==cold['rank_ic']
    np.testing.assert_array_equal(third['report']['portfolio']['nav_series'],cold['portfolio']['nav_series'])
    assert len(service.research.get_session(sid)['experiments'])==3


def test_parallel_cache_writers_publish_complete_matrices(tmp_path):
    cache=L2FactorCache(tmp_path)
    matrix=np.arange(10000,dtype=float).reshape(100,100)
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _:cache.put('identical-key',matrix),range(24)))
    np.testing.assert_array_equal(cache.get('identical-key'),matrix)
    assert not list(tmp_path.rglob('*.tmp'))


def test_file_revision_detects_atomic_replacement_preserving_mtime_and_new_metadata(tmp_path):
    import os
    from assay.config import AssayConfig
    from assay.data.store import DataStore
    from assay.data.schemas import adj_events_path,security_groups_path
    store=DataStore(AssayConfig(data_dir=tmp_path,market='US'))
    args=['2024-01-01','2024-02-29']
    a=store.cache_token(args)
    path=adj_events_path(tmp_path,'US')
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_bytes(b'first')
    b=store.cache_token(args)
    assert b!=a
    original=path.stat()
    replacement=path.with_name(path.name+'.pending')
    replacement.write_bytes(b'other')
    os.utime(replacement,ns=(original.st_atime_ns,original.st_mtime_ns))
    os.replace(replacement,path)  # the canonical ingester publishes partitions atomically
    assert store.cache_token(args)!=b
    group=security_groups_path(tmp_path,'US')
    group.parent.mkdir(parents=True,exist_ok=True)
    group.write_bytes(b'groups')
    assert store.cache_token(args)!=b


def test_added_horizons_count_toward_retained_memory_budget(tmp_path):
    from assay.demo import prepare_demo
    service,ds=prepare_demo(tmp_path/'market')
    backend=service.research.backends['assay']
    assert backend.evaluate('ts_returns(close,3)',ds)['portfolio']['status']=='completed'
    pool=backend.execution_pool
    pool.max_bytes=pool.stats()['retained_bytes']+1
    more=service.research.register_dataset('more horizons',ds['universe'],ds['period'],horizons=[1,2,3,4])
    assert backend.evaluate('ts_returns(close,3)',more)['portfolio']['status']=='completed'
    assert pool.stats()['entries']==0


def test_queued_research_does_not_use_a_changed_panel(tmp_path,monkeypatch):
    import threading
    from assay.demo import prepare_demo
    service,ds=prepare_demo(tmp_path/'market')
    backend=service.research.backends['assay']
    backend.evaluate('ts_returns(close,3)',ds)
    entry=next(iter(backend.execution_pool.entries.values()))
    store=service.store_for_universe(ds['universe'])
    original=store.cache_token
    captured=threading.Event()
    def tracked(period):
        token=original(period)
        if threading.current_thread() is not threading.main_thread():
            captured.set()
        return token
    monkeypatch.setattr(store,'cache_token',tracked)
    entry['lock'].acquire()
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending=pool.submit(backend.evaluate,'ts_returns(close,3)',ds)
        try:
            assert captured.wait(5)
            path=next((tmp_path/'market'/'price_raw').rglob('*.parquet'))
            raw=pl.read_parquet(path)
            raw.with_columns((pl.col('close')*1.02).alias('close')).write_parquet(path)
        finally:
            entry['lock'].release()
        import pytest
        with pytest.raises(RuntimeError,match='changed while waiting'):
            pending.result(timeout=5)
