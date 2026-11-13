"""Accounting revisions cannot silently reuse a historical research identity."""
import copy
import hashlib
import json

import pytest

from assay.demo import prepare_demo
from assay.portfolio.accounting import PORTFOLIO_ACCOUNTING_PROTOCOL
from assay.portfolio.report import PortfolioLineage, PortfolioReport


def test_legacy_session_continues_without_confusing_accounting_protocols(tmp_path):
    service, dataset = prepare_demo(tmp_path / 'market')
    assert dataset['portfolio_accounting_protocol'] == PORTFOLIO_ACCOUNTING_PROTOCOL
    workspace = service.research
    backend = next(row for row in workspace.list_backends() if row['name'] == 'assay')
    assert backend['capabilities']['portfolio_accounting_protocol'] == PORTFOLIO_ACCOUNTING_PROTOCOL
    legacy = dict(dataset)
    legacy.pop('portfolio_accounting_protocol')
    metadata = {'id', 'name', 'description', 'spec_hash', 'created_at'}
    legacy['spec_hash'] = hashlib.sha256(json.dumps(
        {k: v for k, v in legacy.items() if k not in metadata},
        sort_keys=True, allow_nan=False).encode()).hexdigest()
    legacy['id'] = 'ds_legacy_accounting'
    with workspace._db() as db:
        db.execute('INSERT INTO datasets VALUES (?, ?)', (legacy['id'], json.dumps(legacy)))
    sid = workspace.create_session(legacy['id'], 'Retain old records', max_experiments=4)['id']

    # Model a persisted pre-upgrade attempt: its report has no versioned lineage
    # and its dataset and experiment identities must not be rewritten.
    old = workspace.evaluate(sid, 'ts_returns(close,3)', 'original', request_id='old')
    assert old['status'] == 'completed'
    old.pop('portfolio_accounting_protocol')
    old['report']['portfolio']['lineage'].pop('accounting_protocol')
    with workspace._db() as db:
        db.execute('UPDATE experiments SET payload=? WHERE id=?', (json.dumps(old), old['id']))
        # The old version used the unqualified score index.
        db.execute("UPDATE scores SET version=? WHERE version LIKE '%|portfolio:%'", ('mutable:' + legacy['as_of'],))
    before = workspace.get_experiment(old['id'])

    current = workspace.evaluate(sid, 'ts_returns(close,3)', 'revision', request_id='new', parent_id=old['id'])
    assert current['status'] == 'completed'
    assert current['portfolio_accounting_protocol'] == PORTFOLIO_ACCOUNTING_PROTOCOL
    assert current['report']['portfolio']['lineage']['accounting_protocol'] == PORTFOLIO_ACCOUNTING_PROTOCOL
    assert workspace.get_experiment(old['id']) == before
    assert workspace.get_session(sid)['dataset'] == legacy
    assert workspace.evaluate(sid, 'ts_returns(close,3)', 'revision', request_id='new', parent_id=old['id']) == current
    with workspace._db() as db:
        versions = {r[0] for r in db.execute('SELECT version FROM scores WHERE factor_id=? AND spec=?',
                                             (current['factor_id'], legacy['spec_hash']))}
    assert versions == {'mutable:' + legacy['as_of'],
                        'mutable:' + legacy['as_of'] + '|portfolio:' + PORTFOLIO_ACCOUNTING_PROTOCOL}
    comparison = workspace.compare_experiments([old['id'], current['id']])
    assert not comparison['comparable']
    assert [e['portfolio_accounting_protocol'] for e in comparison['experiments']] == [
        'legacy-unversioned', PORTFOLIO_ACCOUNTING_PROTOCOL]
    context = workspace.research_context(sid)
    assert [a['portfolio_accounting_protocol'] for a in context['recent_attempts']] == [
        'legacy-unversioned', PORTFOLIO_ACCOUNTING_PROTOCOL]
    assert any('different accounting protocols' in a for a in context['next_actions'])
    audit = workspace.audit_session(sid)
    assert audit['record_integrity'] == 'passed'
    assert audit['finding_counts']['by_code']['MULTIPLE_ACCOUNTING_PROTOCOLS'] == 1
    with pytest.raises(ValueError, match='same portfolio accounting protocol'):
        workspace.replay_experiment(old['id'], sid, request_id='legacy-replay')
    assert workspace.get_session(sid)['remaining_experiments'] == 2
    replay = workspace.replay_experiment(current['id'], sid, request_id='current-replay')
    assert replay['status'] == 'completed'
    assert workspace.compare_experiments([current['id'], replay['id']])['comparable']
    from assay.research import ResearchWorkspace
    manifest = workspace.export_session(sid)
    restored = ResearchWorkspace(tmp_path / 'restored.sqlite3')
    assert restored.import_session(manifest) == manifest['state']
    assert restored.export_session(sid) == manifest
    assert restored.audit_session(sid)['record_integrity'] == 'passed'
    # Older exported records may carry protocol provenance only in report lineage.
    lineage_only = copy.deepcopy(manifest)
    for attempt in lineage_only['state']['experiments']:
        attempt.pop('portfolio_accounting_protocol', None)
    payload = {k: v for k, v in lineage_only.items() if k != 'manifest_hash'}
    lineage_only['manifest_hash'] = hashlib.sha256(json.dumps(payload, sort_keys=True, allow_nan=False).encode()).hexdigest()
    other = ResearchWorkspace(tmp_path / 'lineage-only.sqlite3')
    other.import_session(lineage_only)
    with other._db() as db:
        versions = {r[0] for r in db.execute('SELECT version FROM scores WHERE factor_id=? AND spec=?',
                                             (current['factor_id'], legacy['spec_hash']))}
    assert versions == {'mutable:' + legacy['as_of'],
                        'mutable:' + legacy['as_of'] + '|portfolio:' + PORTFOLIO_ACCOUNTING_PROTOCOL}


def test_assay_backend_alias_and_unknown_historical_protocol(tmp_path):
    from assay.research import AssayBackend, ResearchWorkspace

    service, base = prepare_demo(tmp_path / 'market')
    ws = service.research
    ws.register_backend('custom-assay', AssayBackend(service, reuse_panels=False))
    dataset = ws.register_dataset('alias', base['universe'], base['period'],
        as_of=base['as_of'], horizons=base['horizons'], execution=base['execution'],
        adj=base['adj'], backend='custom-assay')
    sid = ws.create_session(dataset['id'], 'Alias', max_experiments=2)['id']
    attempt = ws.evaluate(sid, 'ts_returns(close,3)', 'hypothesis', request_id='alias')
    assert attempt['status'] == 'completed'
    manifest = ws.export_session(sid)
    restored = ResearchWorkspace(tmp_path / 'alias.sqlite3')
    assert restored.import_session(manifest) == manifest['state']
    assert restored.audit_session(sid)['record_integrity'] == 'passed'

    # A retained future or older protocol can be imported but not executed under
    # a different accounting implementation.
    archived = copy.deepcopy(manifest)
    state = archived['state']
    state['dataset']['portfolio_accounting_protocol'] = 'historical-protocol-v0'
    metadata = {'id', 'name', 'description', 'spec_hash', 'created_at'}
    digest = hashlib.sha256(json.dumps({k: v for k, v in state['dataset'].items() if k not in metadata},
                                       sort_keys=True, allow_nan=False).encode()).hexdigest()
    state['dataset']['spec_hash'] = digest
    for row in state['experiments']:
        row['spec_hash'] = digest
        row['portfolio_accounting_protocol'] = 'historical-protocol-v0'
        row['report']['portfolio']['spec_hash'] = digest
        row['report']['portfolio']['lineage']['accounting_protocol'] = 'historical-protocol-v0'
    payload = {k: v for k, v in archived.items() if k != 'manifest_hash'}
    archived['manifest_hash'] = hashlib.sha256(json.dumps(payload, sort_keys=True, allow_nan=False).encode()).hexdigest()
    historical = ResearchWorkspace(tmp_path / 'historical.sqlite3', {'custom-assay': AssayBackend(service)})
    assert historical.import_session(archived) == state
    with pytest.raises(ValueError, match='unsupported accounting protocol'):
        historical.evaluate(sid, 'ts_returns(close,5)', 'new', request_id='new')
    assert historical.get_session(sid)['remaining_experiments'] == 1

    conflict = copy.deepcopy(manifest)
    row = conflict['state']['experiments'][0]
    row.pop('portfolio_accounting_protocol')
    row['report']['portfolio']['lineage']['accounting_protocol'] = 'different-protocol'
    payload = {k: v for k, v in conflict.items() if k != 'manifest_hash'}
    conflict['manifest_hash'] = hashlib.sha256(json.dumps(payload, sort_keys=True, allow_nan=False).encode()).hexdigest()
    with pytest.raises(ValueError, match='report accounting differs from dataset protocol'):
        ResearchWorkspace(tmp_path / 'conflict.sqlite3').import_session(conflict)


def test_report_identity_and_legacy_lineage_distinguish_accounting_versions():
    old = PortfolioReport.compute_run_id('factor', 'same-config')
    new = PortfolioReport.compute_run_id('factor', 'same-config', PORTFOLIO_ACCOUNTING_PROTOCOL)
    assert old != new
    assert PortfolioLineage.from_dict({'adj_version': 'split'}).accounting_protocol == 'legacy-unversioned'
    assert PortfolioLineage.from_dict(None).accounting_protocol == 'legacy-unversioned'
    current = PortfolioLineage(accounting_protocol=PORTFOLIO_ACCOUNTING_PROTOCOL)
    assert PortfolioLineage.from_dict(current.to_dict()).accounting_protocol == PORTFOLIO_ACCOUNTING_PROTOCOL


def test_research_dataset_runs_with_declared_capital_and_minimum_fees(tmp_path):
    service, base = prepare_demo(tmp_path / 'market')
    ws = service.research
    dataset = ws.register_dataset('capital-aware', base['universe'], base['period'],
        as_of=base['as_of'], horizons=base['horizons'], execution=base['execution'], adj=base['adj'],
        portfolio={'initial_capital': 10000, 'commission_min': 5., 'slippage_model': 'zero'})
    assert dataset['portfolio']['initial_capital'] == 10000.
    assert dataset['spec_hash'] != base['spec_hash']
    sid = ws.create_session(dataset['id'], 'Commission-aware factor research', max_experiments=1)['id']
    attempt = ws.evaluate(sid, 'ts_returns(close,3)', 'Return factor', request_id='capital-aware')
    assert attempt['status'] == 'completed'
    assert 'not a cost-adjusted' in attempt['report']['rank_ic_definition']
    portfolio = attempt['report']['portfolio']
    assert portfolio['status'] == 'completed'
    assert portfolio['cost_assumptions']['minimum_commission_capital_supplied'] is True
    assert portfolio['cost_assumptions']['initial_capital'] == 10000.
    assert portfolio['cost_assumptions']['commission_min'] == 5.
    assert 'positive values' in portfolio['cost_drag_definition']
    assert not any('minimum commission' in w.lower() for w in portfolio['warnings'])
    assert any('participation' in w.lower() for w in portfolio['warnings'])
    assert portfolio['cost_drag'] > 0
    from assay.research_audit import audit_manifest
    manifest = ws.export_session(sid)
    portfolio_record = manifest['state']['experiments'][0]['report']['portfolio']
    portfolio_record['cost_assumptions']['initial_capital'] = 1.
    payload = {k: v for k, v in manifest.items() if k != 'manifest_hash'}
    manifest['manifest_hash'] = hashlib.sha256(json.dumps(payload, sort_keys=True, allow_nan=False).encode()).hexdigest()
    audited = audit_manifest(manifest)
    assert audited['finding_counts']['by_code']['COST_CONFIG_MISMATCH'] >= 1
    saved_minimum = portfolio_record['cost_assumptions'].pop('commission_min')
    payload = {k: v for k, v in manifest.items() if k != 'manifest_hash'}
    manifest['manifest_hash'] = hashlib.sha256(json.dumps(payload, sort_keys=True, allow_nan=False).encode()).hexdigest()
    assert audit_manifest(manifest)['finding_counts']['by_code']['COST_ASSUMPTIONS_MISSING'] >= 1
    portfolio_record['cost_assumptions']['commission_min'] = saved_minimum
    del portfolio_record['cost_assumptions']['initial_capital']
    del portfolio_record['cost_assumptions']['minimum_commission_capital_supplied']
    payload = {k: v for k, v in manifest.items() if k != 'manifest_hash'}
    manifest['manifest_hash'] = hashlib.sha256(json.dumps(payload, sort_keys=True, allow_nan=False).encode()).hexdigest()
    assert audit_manifest(manifest)['finding_counts']['by_code']['COST_ASSUMPTIONS_MISSING'] >= 1
    del portfolio_record['cost_assumptions']
    payload = {k: v for k, v in manifest.items() if k != 'manifest_hash'}
    manifest['manifest_hash'] = hashlib.sha256(json.dumps(payload, sort_keys=True, allow_nan=False).encode()).hexdigest()
    assert audit_manifest(manifest)['record_integrity'] == 'failed'
