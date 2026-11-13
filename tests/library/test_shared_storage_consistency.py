"""Separate serving processes must observe durable account-owned records."""
import polars as pl
from datetime import date

from assay.config import AssayConfig
from assay.library import CombinationStore, FactorLibrary, FactorReport
from assay.research_tenant import current_tenant
from assay.service import AssayService


def test_factor_and_combination_indices_refresh_across_instances(tmp_path):
    a, b = FactorLibrary(tmp_path/'factors'), FactorLibrary(tmp_path/'factors')
    report = FactorReport('factor_one', 'close', 'close', .1, .2, .1, .2)
    a.save(report)
    assert [r.factor_id for r in b.list()] == ['factor_one']
    report.rank_icir = .8
    a.save(report)
    assert b.list()[0].rank_icir == .8
    assert a.delete(['factor_one']) == 1
    assert b.list() == []
    x, y = CombinationStore(tmp_path/'combinations'), CombinationStore(tmp_path/'combinations')
    row = x.save({'weights': {'close': 1}}, name='mine')
    assert [r['id'] for r in y.list()] == [row['id']]
    assert x.delete([row['id']]) == 1
    assert y.list() == []


def test_private_session_ids_do_not_alias_between_workers_or_restarts(tmp_path):
    cfg = AssayConfig.for_tests(data_dir=str(tmp_path))
    token = current_tenant.set('account:123')
    try:
        services = [AssayService(cfg) for _ in range(3)]
        panel = pl.DataFrame({'date':[date(2024,1,2)], 'symbol':['A'], 'close':[1.]})
        ids = [s.sessions.create_session(panel) for s in services]
        assert len(set(ids)) == len(ids)
        assert all(services[i].sessions.get(ids[j]) is None
                   for i in range(len(ids)) for j in range(len(ids)) if i != j)
    finally:
        current_tenant.reset(token)


def test_workspace_eviction_retires_only_idle_owned_panels(tmp_path):
    from types import SimpleNamespace
    from assay.cache import SessionRegistry
    from assay.research_execution import ResearchExecutionPool
    panel = pl.DataFrame({'date':[date(2024,1,2)], 'symbol':['A'], 'close':[1.]})
    sessions = SessionRegistry()
    idle, active = sessions.create_session(panel), sessions.create_session(panel)
    pool = ResearchExecutionPool(SimpleNamespace(sessions=sessions))
    pool.entries['idle'] = {'sid': idle, 'users': 0}
    pool.entries['active'] = {'sid': active, 'users': 1}
    pool.retire()
    assert sessions.get(idle) is None
    assert sessions.get(active) is not None
    assert pool.retired and 'idle' not in pool.entries and 'active' in pool.entries
