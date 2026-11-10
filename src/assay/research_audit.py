"""Read-only, deterministic research evidence checks for human and agent judges.

This checks saved-record consistency. It does not execute a backend, authenticate
the author of a manifest, or establish out-of-sample investment performance.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime
import hashlib
import json
import math

from assay.research_inspection import bounded

AUDIT_VERSION = 1


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def _finite(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


class _Audit:
    def __init__(self):
        self.findings = []
        self.checks = {}

    def mark(self, category):
        self.checks.setdefault(category, 'passed')

    def add(self, category, code, message, *paths, severity='error', experiment_id=None):
        self.mark(category)
        if severity == 'error' or self.checks[category] != 'failed':
            self.checks[category] = 'failed' if severity == 'error' else 'needs_review'
        self.findings.append(dict(code=code, category=category, severity=severity,
            message=message, evidence=list(paths), experiment_id=experiment_id))

    def timestamp(self, value, path, experiment_id=None):
        self.mark('chronology')
        parsed = datetime.fromisoformat(value)
        if parsed.utcoffset() is None:
            self.add('chronology', 'TIMEZONE_UNSPECIFIED', 'Timestamp has no timezone; ordering is not verified.',
                     path, severity='warning', experiment_id=experiment_id)
            return None
        return parsed


def _series(audit, report, path, experiment_id, *, portfolio=False, period=None, expected_dates=None):
    audit.mark('metric_consistency')
    dates_key = 'nav_dates' if portfolio else 'dates'
    names = ['nav_series'] if portfolio else ['ic_series', 'rank_ic_series']
    dates = report.get(dates_key)
    if not isinstance(dates, list) or not dates:
        audit.add('metric_consistency', 'SERIES_UNAVAILABLE', 'Saved metric series are unavailable for arithmetic checks.',
                  path, severity='warning', experiment_id=experiment_id)
        return
    try:
        from datetime import date
        parsed = [date.fromisoformat(d) for d in dates]
        ordered = all(a < b for a, b in zip(parsed, parsed[1:]))
    except (TypeError, ValueError):
        ordered = False
    if not ordered:
        audit.add('metric_consistency', 'SERIES_DATES_INVALID', 'Series dates must be valid and strictly increasing.',
                  path+'/'+dates_key, experiment_id=experiment_id)
    elif period is not None:
        lower,upper = map(date.fromisoformat,period)
        if any(not lower <= d <= upper for d in parsed):
            audit.add('metric_consistency', 'SERIES_OUTSIDE_SPEC', 'Series dates lie outside the registered evaluation window.',
                      path+'/'+dates_key, '/state/dataset', experiment_id=experiment_id)
    if expected_dates is not None and dates != expected_dates:
        audit.add('metric_consistency', 'SCORE_DATES_MISMATCH', 'Saved scoring dates differ from the registered score dates.',
                  path+'/'+dates_key, '/state/dataset/score_dates', experiment_id=experiment_id)
    for name in names:
        values = report.get(name)
        if not isinstance(values, list) or len(values) != len(dates):
            audit.add('metric_consistency', 'SERIES_LENGTH_MISMATCH', 'Saved dates and metric values are misaligned.',
                      path+'/'+dates_key, path+'/'+name, experiment_id=experiment_id)
            continue
        if any(v is not None and not _finite(v) for v in values):
            audit.add('metric_consistency', 'INVALID_SERIES_VALUE', 'Series values must be finite numbers or null.',
                      path+'/'+name, experiment_id=experiment_id)
            continue
        finite = [v for v in values if _finite(v)]
        if not portfolio and any(abs(v) > 1+1e-12 for v in finite):
            audit.add('metric_consistency', 'IC_SERIES_OUT_OF_RANGE', 'Individual IC values must be between -1 and 1.',
                      path+'/'+name, experiment_id=experiment_id)
        if portfolio:
            actual, metric = report.get('total_return'), 'total_return'
            expected = finite[-1]/finite[0]-1 if len(finite) >= 2 and finite[0] > 0 else None
            if expected is None:
                audit.add('metric_consistency','NAV_EVIDENCE_INVALID','Completed NAV needs two finite points and a positive initial value.',
                          path+'/'+name,experiment_id=experiment_id)
        else:
            metric = 'rank_ic' if name == 'rank_ic_series' else 'ic'
            actual = report.get(metric)
            try:
                expected = math.fsum(finite)/len(finite) if finite else None
            except OverflowError:
                expected = None
                audit.add('metric_consistency', 'INVALID_SERIES_VALUE', 'Series aggregate is not finite.',
                          path+'/'+name, experiment_id=experiment_id)
            if not finite:
                audit.add('metric_consistency','NO_VALID_IC_VALUES','No finite observations support the aggregate IC.',
                          path+'/'+name,severity='error' if actual is not None else 'warning',experiment_id=experiment_id)
        if expected is not None and (not _finite(actual) or not math.isclose(actual, expected, rel_tol=1e-9, abs_tol=1e-10)):
            audit.add('metric_consistency', 'METRIC_SERIES_MISMATCH', f'{metric} disagrees with the saved series.',
                      path+'/'+metric, path+'/'+name, experiment_id=experiment_id)


def _attempts(audit, state, external_sources):
    from assay.research_lifecycle import identity

    dataset = state['dataset']
    started_session = audit.timestamp(state['created_at'], '/state/created_at')
    panels = set()
    finished, starts = {}, {}
    records = {a['id']:a for a in state['experiments']}
    ids = set(records)
    for i, attempt in enumerate(state['experiments']):
        eid = attempt['id']
        base = f'/state/experiments/{i}'
        audit.mark('provenance')
        start = audit.timestamp(attempt['started_at'], base+'/started_at', eid)
        end = audit.timestamp(attempt['finished_at'], base+'/finished_at', eid) if attempt['finished_at'] else None
        finished[eid] = end
        starts[eid] = start
        if start and ((started_session and start < started_session) or (end and end < start)):
            audit.add('chronology', 'TIMESTAMP_ORDER', 'Experiment timestamps contradict session or completion order.',
                      base+'/started_at', base+'/finished_at', experiment_id=eid)
        parent_start = starts.get(attempt.get('parent_id'))
        if start and parent_start and start < parent_start:
            audit.add('chronology','CHILD_PRECEDES_PARENT','Revision starts before its parent experiment.',
                      base+'/parent_id',base+'/started_at',experiment_id=eid)
        if attempt['status'] == 'running':
            audit.add('completion', 'EXPERIMENT_RUNNING', 'An attempt is still running or awaits reconciliation.',
                      base+'/status', severity='warning', experiment_id=eid)
        if attempt.get('replay_of') and attempt['replay_of'] not in ids:
            source = external_sources.get(attempt['replay_of'])
            if source is None:
                audit.add('provenance', 'EXTERNAL_REPLAY_SOURCE', 'Replay source is unavailable in this audit snapshot.',
                          base+'/replay_of', severity='warning', experiment_id=eid)
            elif (not isinstance(source,dict) or source.get('status') not in {'completed','failed','abandoned','cancelled'}
                    or any(source.get(k) != attempt[k]
                    for k in ['expr','hypothesis','spec_hash'])):
                audit.add('provenance', 'REPLAY_SOURCE_MISMATCH', 'Replay differs from its stored source.',
                          base+'/replay_of', experiment_id=eid)
            elif isinstance(source.get('finished_at'),str):
                try:
                    source_end = audit.timestamp(source['finished_at'],base+'/replay_of',eid)
                    if start and source_end and start < source_end:
                        audit.add('chronology','REPLAY_PRECEDES_SOURCE','Replay starts before source completion.',
                                  base+'/replay_of',base+'/started_at',experiment_id=eid)
                except ValueError:
                    audit.add('provenance','REPLAY_SOURCE_MISMATCH','Stored replay source has an invalid completion time.',
                              base+'/replay_of',experiment_id=eid)
        elif attempt.get('replay_of'):
            if records[attempt['replay_of']]['status'] == 'running':
                audit.add('provenance','REPLAY_SOURCE_NOT_TERMINAL','Replay references a source that has not completed.',
                          base+'/replay_of',experiment_id=eid)
            elif start and finished.get(attempt['replay_of']) and start < finished[attempt['replay_of']]:
                audit.add('chronology','REPLAY_PRECEDES_SOURCE','Replay starts before source completion.',
                          base+'/replay_of',base+'/started_at',experiment_id=eid)
        if attempt['status'] != 'completed':
            continue
        report = attempt['report']
        path = base+'/report'
        for name, value in [('universe_id', dataset['universe']), ('execution', dataset['execution']),
                            ('universe_as_of', dataset['as_of']), ('data_as_of', dataset['as_of'])]:
            if name in report and report[name] != value:
                audit.add('provenance', 'REPORT_SPEC_MISMATCH', f'Report {name} disagrees with its dataset.',
                          path+'/'+name, '/state/dataset', experiment_id=eid)
        if 'expr' in report and (not isinstance(report['expr'],str) or identity(report['expr'])[0] != identity(attempt['expr'])[0]):
            audit.add('provenance', 'REPORT_EXPRESSION_MISMATCH', 'Report expression differs from the attempted factor.',
                      path+'/expr', base+'/expr', experiment_id=eid)
        audit.mark('diagnostics')
        diag = report.get('diagnostics') or {}
        if report.get('lookahead_detected') or (isinstance(diag,dict) and diag.get('lookahead_detected')):
            audit.add('diagnostics', 'LOOKAHEAD_FLAGGED', 'A completed report carries a lookahead diagnostic.',
                      path+'/lookahead_detected', path+'/diagnostics', experiment_id=eid)
        if (isinstance(diag,dict) and diag.get('ok') is False) or report.get('scoring_complete') is False:
            audit.add('diagnostics', 'INCOMPLETE_DIAGNOSTICS', 'A completed report records failed diagnostics or incomplete scoring.',
                      path+'/diagnostics', path+'/scoring_complete', experiment_id=eid)
        if dataset.get('expression_policy'):
            from assay.research_policy import check_expression
            if not check_expression(attempt['expr'], dataset['expression_policy'])['ok']:
                audit.add('diagnostics', 'EXPRESSION_POLICY_VIOLATION', 'A completed factor violates the registered expression policy.',
                          base+'/expr', '/state/dataset/expression_policy', experiment_id=eid)
        for name in ['ic', 'rank_ic', 'icir', 'rank_icir', 'turnover_1d']:
            value = report.get(name)
            if value is not None and (not _finite(value) or (name in {'ic','rank_ic'} and abs(value) > 1+1e-12)):
                audit.add('metric_consistency', 'INVALID_METRIC', f'{name} has an invalid numeric value.',
                          path+'/'+name, experiment_id=eid)
        _series(audit, report, path, eid,period=dataset.get('score_period',dataset['period']),
                expected_dates=dataset.get('score_dates'))
        panel = report.get('panel_content_hash')
        if isinstance(panel,str) and panel:
            panels.add(panel)
        elif dataset['backend'] == 'assay':
            audit.add('provenance', 'PANEL_FINGERPRINT_MISSING', 'No observed panel fingerprint accompanies this completed report.',
                      path, severity='warning', experiment_id=eid)
        portfolio = report.get('portfolio')
        required = bool(dataset.get('portfolio')) and dataset['portfolio'].get('enabled') is not False
        audit.mark('portfolio')
        if required and (not isinstance(portfolio,dict) or portfolio.get('status') != 'completed'):
            audit.add('portfolio', 'PORTFOLIO_EVIDENCE_MISSING', 'Configured portfolio evaluation has no completed evidence.',
                      path+'/portfolio', '/state/dataset/portfolio', experiment_id=eid)
        if portfolio is not None and not isinstance(portfolio,dict):
            audit.add('portfolio','MALFORMED_PORTFOLIO','Portfolio evidence must be an object.',path+'/portfolio',experiment_id=eid)
        if not isinstance(portfolio,dict):
            continue
        ppath = path+'/portfolio'
        for name, value in [('spec_hash', dataset['spec_hash']), ('panel_content_hash', panel)]:
            if value is not None and name in portfolio and portfolio[name] != value:
                audit.add('portfolio', 'PORTFOLIO_SPEC_MISMATCH', f'Portfolio {name} disagrees with factor evidence.',
                          ppath+'/'+name, path, experiment_id=eid)
        if 'config' in portfolio and required and portfolio['config'] != dataset['portfolio']:
            audit.add('portfolio', 'PORTFOLIO_CONFIG_MISMATCH', 'Portfolio configuration differs from the frozen specification.',
                      ppath+'/config', '/state/dataset/portfolio', experiment_id=eid)
        for name in ['total_return','gross_return','annual_return','sharpe','sortino','max_drawdown','annual_turnover','cost_drag']:
            value = portfolio.get(name)
            if value is not None and not _finite(value):
                audit.add('portfolio','INVALID_PORTFOLIO_METRIC',f'{name} is not finite numeric evidence.',
                          ppath+'/'+name,experiment_id=eid)
        if portfolio.get('status') == 'completed' or required:
            _series(audit, portfolio, ppath, eid, portfolio=True,period=dataset['period'])
        assumptions = portfolio.get('cost_assumptions')
        if required and not isinstance(assumptions,dict):
            audit.add('portfolio', 'COST_ASSUMPTIONS_MISSING', 'Cost assumptions are not recorded.',
                      ppath, severity='warning', experiment_id=eid)
        if isinstance(assumptions,dict) and required:
            for name in ['commission_rate', 'slippage_model']:
                if assumptions.get(name) != dataset['portfolio'].get(name):
                    audit.add('portfolio', 'COST_CONFIG_MISMATCH', f'Recorded {name} differs from the portfolio configuration.',
                              ppath+'/cost_assumptions/'+name, '/state/dataset/portfolio/'+name, experiment_id=eid)
        if portfolio.get('warnings'):
            audit.add('portfolio', 'PORTFOLIO_WARNINGS', 'The portfolio report contains execution or cost limitations.',
                      ppath+'/warnings', severity='warning', experiment_id=eid)
    for i, decision in enumerate(state['decisions']):
        audit.mark('lifecycle')
        when = audit.timestamp(decision['created_at'], f'/state/decisions/{i}/created_at')
        end = finished.get(decision['experiment_id'])
        if when and end and when < end:
            audit.add('chronology', 'DECISION_PRECEDES_EVIDENCE', 'Decision predates completion of its supporting experiment.',
                      f'/state/decisions/{i}', experiment_id=decision['experiment_id'])
    if len(panels) > 1:
        audit.add('provenance', 'MULTIPLE_PANEL_REVISIONS', 'Completed attempts used different observed panels.',
                  '/state/experiments', severity='warning')


def _registry(audit, state, registry):
    from assay.research_lifecycle import identity

    audit.mark('registry')
    links = {r['experiment_id']:r for r in registry['links']}
    rows = {r['id']:r for r in registry['experiments']}
    for i, attempt in enumerate(state['experiments']):
        eid, path = attempt['id'], f'/state/experiments/{i}'
        fid, canonical = identity(attempt['expr'])
        link, row = links.get(eid), rows.get(eid)
        if (fid is not None and (link is None or link['factor_id'] != fid or link['canonical'] != canonical)
                or fid is None and link is not None):
            audit.add('registry', 'REGISTRY_LINK_MISMATCH', 'Canonical factor registry and saved attempt disagree.',
                      path, experiment_id=eid)
        if row is None or any(row[k] != attempt[k] for k in ['id','session_id','request_id']):
            audit.add('registry', 'STORAGE_IDENTITY_MISMATCH', 'Storage identity columns disagree with the attempt payload.',
                      path, experiment_id=eid)
    events = {r['id']:r for r in registry['decisions']}
    for i,event in enumerate(state['decisions']):
        row = events.get(event['id'])
        if row is None or any(row[k] != event[k] for k in ['id','session_id','factor_id','experiment_id','request_id']):
            audit.add('registry', 'DECISION_STORAGE_MISMATCH', 'Decision index columns disagree with the saved evidence.',
                      f'/state/decisions/{i}', experiment_id=event['experiment_id'])


def _summary(state, offset, limit):
    from assay.research_lifecycle import identity
    attempts = state['experiments']
    counts = Counter(a['status'] for a in attempts)
    latest = {d['factor_id']:d for d in state['decisions']}
    groups = {}
    for i, a in enumerate(attempts):
        key = a.get('factor_id') or identity(a['expr'])[0] or a['expr']
        groups.setdefault(key,[]).append((i,a))
    candidates = []
    for fid, group in groups.items():
        decision = latest.get(fid)
        pair = next(((i,a) for i,a in group if decision and a['id'] == decision['experiment_id']), group[-1])
        i,a = pair
        r = a.get('report') or {}
        pf = r.get('portfolio') if isinstance(r.get('portfolio'),dict) else {}
        candidates.append(dict(factor_id=a.get('factor_id') or identity(a['expr'])[0], experiment_id=a['id'], status=a['status'],
            decision=decision['decision'] if decision else 'candidate', expression=bounded(a['expr']),
            hypothesis=bounded(a['hypothesis']),
            metrics={k:r.get(k) if _finite(r.get(k)) else None for k in ['ic','rank_ic','turnover_1d']},
            portfolio_metrics={k:pf.get(k) if _finite(pf.get(k)) else None for k in ['total_return','sharpe','max_drawdown','cost_drag']},
            evidence=f'/state/experiments/{i}'))
    dataset = {k:v for k,v in state['dataset'].items() if k!='backend_options'}
    if state['dataset'].get('backend_options'):
        dataset['backend_options_keys'] = sorted(state['dataset']['backend_options'])
    return dict(session_id=state['id'], goal=bounded(state['goal']), dataset=bounded(dataset),
        budget=dict(limit=state['budget'], spent=len(attempts), remaining=state['remaining_experiments']),
        attempts=dict(total=len(attempts), statuses=dict(counts)),
        completion='empty' if not attempts else 'in_progress' if counts['running'] else 'terminal',
        unique_factors=sum(identity(g[0][1]['expr'])[0] is not None for g in groups.values()),
        repeated_factor_groups=sum(len(g)>1 for g in groups.values()),
        candidate_decisions=dict(Counter(d['decision'] for d in latest.values())),
        candidates=candidates[offset:offset+limit], candidate_total=len(candidates))


def audit_manifest(manifest, *, offset=0, limit=20, expected_manifest_hash=None, registry=None, source='manifest'):
    """Audit every record; paginate findings/candidate evidence, not the checks."""
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError('require offset >= 0 and 1 <= limit <= 100')
    if expected_manifest_hash is not None and (not isinstance(expected_manifest_hash,str)
            or len(expected_manifest_hash)!=64 or any(c not in '0123456789abcdefABCDEF' for c in expected_manifest_hash)):
        raise ValueError('expected_manifest_hash must be a SHA-256 hex digest')
    from assay.research import ResearchWorkspace
    from assay.research_manifest import validate_state

    audit = _Audit()
    audit.mark('manifest_integrity')
    if not ResearchWorkspace.verify_manifest(manifest):
        audit.add('manifest_integrity', 'MANIFEST_INTEGRITY', 'Manifest version, envelope or content hash is invalid.', '/')
    if expected_manifest_hash is not None and (not isinstance(manifest,dict) or
            manifest.get('manifest_hash') != expected_manifest_hash.lower()):
        audit.add('manifest_integrity','EXPECTED_MANIFEST_MISMATCH','Input differs from the manifest selected by the judge.',
                  '/manifest_hash')
    state = manifest.get('state') if isinstance(manifest,dict) else None
    if isinstance(state,dict) and 'decisions' not in state:
        state = dict(state,decisions=[])
    if isinstance(state,dict) and isinstance(state.get('experiments'),list) and type(state.get('budget')) is int:
        audit.mark('budget')
        if (len(state['experiments']) > state['budget'] or type(state.get('remaining_experiments')) is not int
                or state['remaining_experiments'] != state['budget']-len(state['experiments'])):
            audit.add('budget','BUDGET_MISMATCH','Attempt count and remaining budget disagree.',
                      '/state/budget','/state/remaining_experiments','/state/experiments')
    schema_ok = False
    try:
        validate_state(state, allow_running=True)
        schema_ok = True
        audit.mark('record_consistency')
    except (ValueError,TypeError,KeyError,AttributeError,OverflowError) as exc:
        message = str(exc) if isinstance(exc,ValueError) else 'Malformed record structure.'
        audit.add('record_consistency', 'SESSION_RECORD_INVALID', message[:500], '/state')
    summary = None
    if schema_ok:
        audit.mark('budget')
        audit.mark('lifecycle')
        audit.mark('completion')
        _attempts(audit,state,registry.get('replay_sources',{}) if registry is not None else {})
        if not state['experiments']:
            audit.add('completion','NO_EXPERIMENTS','The session has no experimental evidence.',
                      '/state/experiments', severity='warning')
        if state['dataset'].get('snapshot_id') is None:
            audit.add('provenance','MUTABLE_DATA_SPEC','The dataset does not name an immutable snapshot.',
                      '/state/dataset/snapshot_id',severity='warning')
        if registry is not None:
            _registry(audit,state,registry)
        summary = _summary(state,offset,limit)
    severity = Counter(f['severity'] for f in audit.findings)
    counts = Counter(f['code'] for f in audit.findings)
    valid = severity['error'] == 0
    report = dict(kind='assay_research_audit',audit_version=AUDIT_VERSION,source=source,
        session_id=bounded(state.get('id')) if isinstance(state,dict) and isinstance(state.get('id'),str) else None,
        manifest_hash=manifest.get('manifest_hash') if isinstance(manifest,dict) and isinstance(manifest.get('manifest_hash'),str) else None,
        status='failed' if not valid else 'needs_review' if severity['warning'] else 'passed',
        record_integrity='passed' if valid else 'failed',
        checks=[dict(category=k,status=v) for k,v in audit.checks.items()],
        finding_counts=dict(errors=severity['error'],warnings=severity['warning'],by_code=dict(counts)),
        findings=audit.findings[offset:offset+limit],
        page=dict(offset=offset,limit=limit,finding_total=len(audit.findings),
                  has_more=offset+limit<len(audit.findings),
                  candidate_total=summary['candidate_total'] if summary else 0,
                  candidates_have_more=bool(summary and offset+limit<summary['candidate_total'])),
        summary=summary,
        assessment=dict(scope='saved_record_self_consistency',registry_checked=registry is not None and schema_ok,
            registry_scope=['factor_attempt_links','experiment_identity_columns','decision_identity_columns'] if registry is not None else [],
            execution_replayed=False,author_authenticated=False,out_of_sample_validity='not_assessed',
            goal_achievement='requires_judge',profitability='not_assessed'))
    report['audit_hash'] = _digest(report)
    return report


def audit_session(workspace, session_id, *, offset=0, limit=20, expected_manifest_hash=None):
    """Read the session and registry in the same SQLite snapshot; perform no writes."""
    with workspace._db() as db:
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        state = workspace._session_state(db,session_id)
        registry = dict(links=[dict(r) for r in db.execute(
            'SELECT a.experiment_id,a.factor_id,f.canonical FROM research_factor_attempts a '
            'JOIN experiments e ON e.id=a.experiment_id LEFT JOIN factors f ON f.id=a.factor_id '
            'WHERE e.session_id=?',(session_id,))],
            experiments=[dict(r) for r in db.execute(
                'SELECT id,session_id,request_id FROM experiments WHERE session_id=?',(session_id,))],
            decisions=[dict(r) for r in db.execute(
                'SELECT id,session_id,factor_id,experiment_id,request_id FROM research_decisions WHERE session_id=?',(session_id,))],
            replay_sources={})
        local_ids = {a['id'] for a in state['experiments']}
        external_ids = sorted({a['replay_of'] for a in state['experiments'] if a.get('replay_of') and a['replay_of'] not in local_ids})
        for i in range(0,len(external_ids),500):
            chunk=external_ids[i:i+500]
            for row in db.execute('SELECT id,payload FROM experiments WHERE id IN ('+','.join('?' for _ in chunk)+')',chunk):
                registry['replay_sources'][row[0]] = json.loads(row[1])
    payload = dict(schema_version=1,kind='assay_research_session_manifest',state=state)
    return audit_manifest(dict(payload,manifest_hash=_digest(payload)),offset=offset,limit=limit,
                          expected_manifest_hash=expected_manifest_hash,registry=registry,source='workspace_snapshot')
