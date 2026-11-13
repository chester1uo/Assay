"""Durable research-attempt submission to the FactorDB/ADES controller.

One registry transaction owns submission identity, factor/spec identity and job.
Queue dispatch is idempotent and repeatable after a lost response or restart.
"""
from __future__ import annotations
import hashlib
import json
import time
from assay.research_errors import ResearchError


def _json(value):
    return json.dumps(value, sort_keys=True, allow_nan=False)


def _job_id(request_id, owner):
    # The queue is shared across accounts. Namespace deterministic retry IDs so
    # importing the same experiment in two accounts cannot collide or cancel
    # the other account's work. Preserve existing service-workspace job IDs.
    basis = f'{owner}\x1f{request_id}' if str(owner).startswith('assay-private:') else request_id
    return 'rjob_' + hashlib.sha256(basis.encode()).hexdigest()[:24]


class FactorDBResearchBackend:
    def __init__(self, factory=None, *, owner='research'):
        if factory is None:
            from assay.factordb.service import get
            factory = get
        self.factory, self.owner = factory, owner

    def describe(self):
        return dict(kind='asynchronous', description='Version-pinned FactorDB jobs executed by ADES',
                    fields=['open','high','low','close','volume'], frequencies=['1d'],
                    execution_modes=['next_open','next_close','close'],
                    supported_adjustments=['total'], supports_async=True,
                    requires_snapshot_id=True, supports_cancellation=True, supports_immutable_snapshots=False)

    def probe(self):
        svc=self._service()
        return {'status':'available','data_version':svc.db.qfv.snapshot_id,
                'data_versions':svc.stores.versions(),
                'owner':self.owner,'budget':svc.budget(self.owner),
                'scope':'Logical panel version; physical content is not authenticated.'}

    def _service(self):
        try:
            svc=self.factory()
        except RuntimeError as exc:
            raise ResearchError('BACKEND_UNAVAILABLE',str(exc),retryable=True,action='configure_backend') from exc
        if svc.ades is None:
            raise ResearchError('BACKEND_UNAVAILABLE','Enable ADES for asynchronous research jobs.',
                                retryable=True,action='configure_backend')
        return svc

    def validate_dataset(self, dataset):
        if dataset['execution'] == 'next_close' and dataset.get('evaluation_protocol') != 'assay-daily-v2':
            raise ResearchError('EXECUTION_PROTOCOL_MISMATCH', 'Register a new dataset for corrected next-day-close labels.', action='register_dataset')
        if not isinstance(dataset.get('snapshot_id'), str) or not dataset['snapshot_id'].strip():
            raise ResearchError('SNAPSHOT_REQUIRED','Specify the data_version returned by backend probing.',action='register_dataset')
        if (dataset['adj'] != 'total' or dataset['as_of'] != dataset['period'][1]
                or dataset.get('score_period') or dataset.get('score_dates') or dataset.get('backend_options')):
            raise ResearchError('UNSUPPORTED_CONFIGURATION',
                'FactorDB research requires total adjustment, as_of=end, no score window/backend options.',action='register_dataset')

    def submit(self, expr, dataset, request_id):
        self.validate_dataset(dataset)
        svc=self._service()
        from assay.engine import parse
        from assay.factordb import canonical as cn
        from assay.factordb.registry import spec_key
        inputs=_json({'expr':expr,'dataset':dataset,'owner':self.owner})
        reg=svc.reg
        with reg._lock, reg._conn() as c:
            c.execute('BEGIN IMMEDIATE')
            previous=c.execute('SELECT * FROM research_submissions WHERE request_id=?',(request_id,)).fetchone()
            if previous:
                if previous['inputs']!=inputs:
                    raise ResearchError('REQUEST_CONFLICT','Execution identity belongs to different inputs.')
                receipt=json.loads(previous['receipt'])
            else:
                version=svc.stores.for_universe(dataset['universe']).snapshot_id
                if version!=dataset['snapshot_id']:
                    raise ResearchError('DATA_VERSION_MISMATCH','Controller data version differs from dataset snapshot.',action='inspect_data_version')
                if svc.over_budget(self.owner):
                    raise ResearchError('COMPUTE_BUDGET_EXHAUSTED','FactorDB compute budget is exhausted.',action='wait_for_budget')
                try:
                    from assay.factordb.agent import _ops
                    from assay.engine import operators
                    node=parse(expr)
                    if any(not operators.is_registered(op) for op in _ops(node)):
                        raise ValueError('Unknown operator')
                except (ValueError, SyntaxError) as exc:
                    raise ResearchError('INVALID_EXPRESSION', str(exc), action='revise_expression') from exc
                fid=cn.factor_id(cn.canonicalize(node))
                spec=spec_key(dataset['universe'],*dataset['period'],dataset['horizons'],dataset['execution'])
                jid=_job_id(request_id, self.owner)
                c.execute('INSERT OR IGNORE INTO specs VALUES (?,?,?,?,?,?)',
                    (spec,dataset['universe'],*dataset['period'],_json(dataset['horizons']),dataset['execution']))
                c.execute('INSERT OR IGNORE INTO factors (id,canonical,expr,owner,created) VALUES (?,?,?,?,?)',
                          (fid,str(cn.canonicalize(node)),expr,self.owner,time.time()))
                known=c.execute('SELECT metrics FROM scores WHERE factor_id=? AND spec=? AND version=?',(fid,spec,version)).fetchone()
                failed=bool(known and json.loads(known[0]).get('error'))
                c.execute('INSERT INTO jobs (id,owner,spec,status,total,done,failed,created,finished,factor_ids,exprs,label,version) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (jid,self.owner,spec,'done' if known else 'queued',1,int(bool(known) and not failed),int(failed),
                     time.time(),time.time() if known else None,_json([fid]),_json([expr]),'research:'+request_id,version))
                receipt={'backend':'factordb_ades','request_id':request_id,'job_id':jid,'factor_id':fid,
                         'spec':spec,'data_version':version,'owner':self.owner,'cached':bool(known)}
                c.execute('INSERT INTO research_submissions VALUES (?,?,?)',(request_id,inputs,_json(receipt)))
        # A crash after registry commit is repaired by the same stable task IDs.
        job=reg.job(receipt['job_id'])
        if job['status'] in {'queued','running'}:
            svc.ades.enqueue(receipt['job_id'],self.owner,receipt['spec'],receipt['data_version'],
                             [[receipt['factor_id'],expr]],1,idempotent=True)
        return receipt

    def cancel(self, receipt, reason):
        """Cancel by stable attempt identity, even if its submission response was lost."""
        expected_job = _job_id(receipt['request_id'], self.owner)
        if str(self.owner).startswith('assay-private:') and receipt.get('job_id') not in (None, expected_job):
            raise ResearchError('EXECUTION_IDENTITY_MISMATCH', 'Cancellation receipt does not belong to this account.')
        svc = self._service()
        with svc.reg._conn() as c:
            saved = c.execute('SELECT receipt FROM research_submissions WHERE request_id=?',
                              (receipt['request_id'],)).fetchone()
        if saved:
            receipt = {**receipt, **json.loads(saved['receipt'])}
        jid = receipt.get('job_id') or _job_id(receipt['request_id'], self.owner)
        job = svc.reg.job(jid)
        if job and job['status'] in {'done', 'failed'}:
            # Cached jobs have no queue task; their already accepted result wins.
            return {**self.poll(receipt), 'execution': receipt}
        if str(self.owner).startswith('assay-private:'):
            acknowledgement = svc.ades.cancel_job(jid, reason, owner=self.owner)
        else:
            acknowledgement = svc.ades.cancel_job(jid, reason)
        svc.recover_results(jid)
        execution = {**receipt, 'job_id': jid, 'cancellation_receipt': acknowledgement}
        if acknowledgement['status'] == 'finished':
            return {**self.poll(receipt), 'execution': receipt}
        return {'phase': 'cancelled', 'report': None, 'execution': execution}

    def poll(self, receipt):
        if str(self.owner).startswith('assay-private:') and receipt.get('job_id') != _job_id(receipt['request_id'], self.owner):
            raise ResearchError('EXECUTION_IDENTITY_MISMATCH', 'Execution receipt does not belong to this account.')
        svc=self._service()
        svc.recover_results(receipt['job_id'])
        job=svc.reg.job(receipt['job_id'])
        if job and job['status'] == 'cancelled':
            return {'phase': 'cancelled', 'report': None}
        if job is None:
            raise ResearchError('EXECUTION_NOT_FOUND','The FactorDB job is missing; do not resubmit blindly.',action='inspect_backend')
        with svc.reg._conn() as c:
            row=c.execute('SELECT metrics FROM scores WHERE factor_id=? AND spec=? AND version=?',
                          (receipt['factor_id'],receipt['spec'],receipt['data_version'])).fetchone()
        if row:
            metrics=json.loads(row['metrics'])
            report={**metrics,'data_version':receipt['data_version'],'factor_id':receipt['factor_id'],
                    'job_id':receipt['job_id'],'turnover_1d':metrics.get('turnover'),
                    'decay_halflife_days':metrics.get('decay_halflife'),'diagnostics_scope':'aggregate_only'}
            if metrics.get('error'):
                report.update(failure_mode='BACKEND_EVALUATION_FAILED',suggestion='Inspect evaluator diagnostics before revising.')
            return {'phase':'failed' if report.get('failure_mode') else 'completed','report':report}
        if job['status'] in {'done','failed'}:
            raise ResearchError('BACKEND_RESULT_MISSING',job.get('error') or 'Job ended without a matching versioned score.',action='inspect_backend')
        return {'phase':job['status'],'report':None}
