"""Reconcile an asynchronous attempt using persisted execution identities."""
from .research_errors import ResearchError, recovery_error


def reconcile(workspace, experiment_id):
    from .research import _json, _now
    import json
    attempt=workspace.get_experiment(experiment_id)
    if attempt['status']!='running':
        return attempt
    if 'execution' not in attempt:
        raise ResearchError('MANUAL_RECOVERY_REQUIRED','This synchronous attempt has no durable backend job.',action='inspect_worker')
    dataset=workspace.get_dataset(attempt['dataset_id'])
    backend=workspace.backends.get(dataset['backend'])
    try:
        if backend is None:
            raise ResearchError('BACKEND_UNAVAILABLE','Register the original backend before reconciling.',
                                retryable=True,action='configure_backend')
        policy=dataset.get('expression_policy') if not attempt.get('cancellation') else None
        if policy:
            from .research_policy import check_expression
            attempt['policy_check']=check_expression(attempt['expr'],policy)
            if not attempt['policy_check']['ok']:
                raise ResearchError('EXPRESSION_POLICY_VIOLATION','; '.join(attempt['policy_check']['issues']),action='revise_expression')
        execution=attempt['execution']
        cancelling = attempt.get('cancellation')
        if not execution.get('job_id') and not cancelling:
            execution=backend.submit(attempt['expr'],dataset,attempt['id'])
            if not isinstance(execution, dict) or not isinstance(execution.get('job_id'), str) or not execution['job_id'].strip():
                raise ResearchError('INVALID_BACKEND_REPORT', 'Submission requires a durable job_id string.')
            try:
                execution = json.loads(_json(execution))
            except (TypeError, ValueError) as exc:
                raise ResearchError('INVALID_BACKEND_REPORT', 'Submission receipt must be JSON-safe.') from exc
            execution = {'backend': dataset['backend'], **execution}
            if execution['backend'] != dataset['backend']:
                raise ValueError('submission receipt backend differs from dataset')
            attempt['execution']={**execution,'phase':'submitted'}
            # Save the receipt before polling; even a crash here is recoverable by request identity.
            with workspace._db() as db:
                db.execute('BEGIN IMMEDIATE')
                current=json.loads(db.execute('SELECT payload FROM experiments WHERE id=?',(experiment_id,)).fetchone()[0])
                if current['status']!='running':
                    return current
                if current.get('cancellation') != attempt.get('cancellation'):
                    return current
                current['execution']=attempt['execution']
                db.execute('UPDATE experiments SET payload=? WHERE id=?',(_json(current),experiment_id))
        if cancelling:
            state=backend.cancel({**execution, 'request_id': attempt['id']}, cancelling['reason'])
            attempt['execution']=state.get('execution', attempt['execution'])
        else:
            state=backend.poll(execution)
        try:
            state = json.loads(_json(state))
        except (TypeError, ValueError) as exc:
            raise ResearchError('INVALID_BACKEND_REPORT', 'Backend execution state must be JSON-safe.') from exc
        if not isinstance(state, dict) or state.get('phase') not in {'queued','running','completed','failed','cancelled'}:
            raise ResearchError('INVALID_BACKEND_REPORT', 'Backend returned an invalid execution phase.')
        if state['phase'] == 'completed' and not isinstance(state.get('report'), dict):
            raise ResearchError('INVALID_BACKEND_REPORT', 'Completed backend execution requires a report object.')
        if state.get('report') is not None and not isinstance(state['report'], dict):
            raise ResearchError('INVALID_BACKEND_REPORT', 'Backend report must be an object or null.')
        if state['phase'] == 'failed' and not (state.get('report') or {}).get('failure_mode'):
            state['report'] = {**(state.get('report') or {}), 'failure_mode': 'BACKEND_EXECUTION_FAILED'}
        attempt['execution']['phase']=state['phase']
        attempt['report']=state.get('report')
        if attempt['report'] is not None:
            from .research_lifecycle import validate_report
            attempt['report'] = validate_report(attempt['report'])
            if state['phase'] == 'completed' and attempt['report'].get('failure_mode'):
                state['phase'] = attempt['execution']['phase'] = 'failed'
        attempt['error']=None
        if state['phase'] == 'cancelled':
            attempt['error'] = {'type': 'Cancelled', 'code': 'EXECUTION_CANCELLED',
                                'message': (cancelling or {}).get('reason', 'Backend job cancelled'),
                                'retryable': False, 'recommended_action': 'revise_expression'}
        if state['phase'] in {'completed','failed','cancelled'}:
            attempt['status']=state['phase']
            attempt['finished_at']=_now()
    except Exception as exc:
        error=recovery_error(exc)
        attempt['error']=error
        if error['retryable']:
            attempt['execution']['phase']='reconciliation_required'
        else:
            attempt.update(status='failed',finished_at=_now())
            attempt['execution']['phase']='failed'
    with workspace._db() as db:
        db.execute('BEGIN IMMEDIATE')
        current=json.loads(db.execute('SELECT payload FROM experiments WHERE id=?',(experiment_id,)).fetchone()[0])
        if current['status']!='running':
            return current
        if current.get('cancellation') != attempt.get('cancellation'):
            return current
        # A slower response cannot overwrite a receipt already persisted by another caller.
        if current.get('execution',{}).get('job_id') and not attempt['execution'].get('job_id'):
            attempt['execution']=current['execution']
        db.execute('UPDATE experiments SET payload=? WHERE id=?',(_json(attempt),experiment_id))
        from .research_lifecycle import index_attempt
        index_attempt(db, attempt, dataset)
    return attempt
