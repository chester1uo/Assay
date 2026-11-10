"""Research workbench REST adapter over the same durable workspace as MCP."""
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from assay.api.app import get_service
from assay.api.auth import get_api_key

router = APIRouter(dependencies=[Depends(get_api_key)])


def invoke(method, *args, **kwargs):
    try:
        return getattr(get_service().research, method)(*args, **kwargs)
    except ValueError as exc:
        from assay.research_errors import ResearchError
        if isinstance(exc, ResearchError):
            raise HTTPException(status_code=400, detail=exc.to_dict()) from exc
        raise HTTPException(status_code=400, detail=str(exc)) from exc



class ResearchRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')


class SessionRequest(ResearchRequest):
    dataset_id: str
    goal: str = Field(min_length=1)
    max_experiments: int = Field(default=20, ge=1, strict=True)


class ExperimentRequest(ResearchRequest):
    expr: str = Field(min_length=1)
    hypothesis: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    parent_id: str | None = None


class NoteRequest(ResearchRequest):
    text: str = Field(min_length=1)


class DatasetRequest(ResearchRequest):
    name: str = Field(min_length=1)
    universe: str = Field(min_length=1)
    period: list[str] = Field(min_length=2, max_length=2)
    backend: str = 'assay'
    as_of: str | None = None
    horizons: list[int] | None = None
    execution: str = 'next_open'
    adj: str = 'split'
    snapshot_id: str | None = None
    description: str = ''
    backend_options: dict | None = None
    score_period: list[str] | None = None
    score_dates: list[str] | None = None
    expression_policy: dict | None = None
    portfolio: dict | None = None


@router.get('/backends')
def backends(probe: bool = False):
    return invoke('list_backends', probe=probe)


@router.post('/datasets')
def register_dataset(req: DatasetRequest):
    return invoke('register_dataset', **req.model_dump())


@router.post('/sessions/import')
def import_session(manifest: dict):
    return invoke('import_session', manifest)


@router.get('/datasets')
def datasets():
    return invoke('list_datasets')


@router.get('/sessions')
def sessions():
    return invoke('list_sessions')


@router.post('/sessions')
def create_session(req: SessionRequest):
    return invoke('create_session', **req.model_dump(), agent='human:webui')


@router.get('/sessions/{session_id}')
def session(session_id: str):
    return invoke('get_session', session_id)


@router.get('/sessions/{session_id}/manifest')
def manifest(session_id: str):
    return invoke('export_session', session_id)


@router.get('/sessions/{session_id}/context')
def context(session_id: str, limit: int = Query(10, ge=1, le=50)):
    return invoke('research_context', session_id, limit=limit)


@router.get('/sessions/{session_id}/audit')
def audit(session_id: str, offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=100),
          expected_manifest_hash: str | None = Query(None, pattern=r'^[0-9a-fA-F]{64}$')):
    return invoke('audit_session',session_id,offset=offset,limit=limit,
                  expected_manifest_hash=expected_manifest_hash)


@router.post('/sessions/{session_id}/experiments')
def evaluate(session_id: str, req: ExperimentRequest):
    # Sync route runs in FastAPI's thread pool; reads remain available during evaluation.
    return invoke('evaluate', session_id, **req.model_dump(), agent='human:webui')


@router.post('/sessions/{session_id}/notes')
def note(session_id: str, req: NoteRequest):
    return invoke('add_note', session_id, req.text, agent='human:webui')


@router.get('/experiments/{experiment_id}')
def inspect(experiment_id: str, section: str = 'summary', offset: int = Query(0, ge=0),
            limit: int = Query(20, ge=1, le=100)):
    from assay.research_inspection import inspect_experiment
    attempt = invoke('get_experiment', experiment_id)
    try:
        return inspect_experiment(attempt, section=section, offset=offset, limit=limit)
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc)) from exc


class CompareRequest(ResearchRequest):
    experiment_ids: list[str] = Field(min_length=2)


@router.post('/experiments/compare')
def compare(req: CompareRequest):
    return invoke('compare_experiments', req.experiment_ids)


class ReplayRequest(ResearchRequest):
    session_id: str
    request_id: str = Field(min_length=1)


@router.post('/experiments/{experiment_id}/replay')
def replay(experiment_id: str, req: ReplayRequest):
    return invoke('replay_experiment', experiment_id, **req.model_dump(), agent='rest')


@router.post('/experiments/{experiment_id}/reconcile')
def reconcile(experiment_id: str):
    return invoke('reconcile_experiment', experiment_id)


class CancellationRequest(ResearchRequest):
    reason: str = Field(min_length=1)


@router.post('/experiments/{experiment_id}/cancel')
def cancel(experiment_id: str, req: CancellationRequest):
    return invoke('cancel_experiment', experiment_id, req.reason)


@router.post('/experiments/{experiment_id}/abandon')
def abandon(experiment_id: str, req: CancellationRequest):
    return invoke('abandon_experiment', experiment_id, req.reason)


class DecisionRequest(ResearchRequest):
    decision: str
    reason: str = Field(min_length=1)
    request_id: str = Field(min_length=1)


@router.post('/experiments/{experiment_id}/decisions')
def decide(experiment_id: str, req: DecisionRequest):
    return invoke('decide_candidate', experiment_id, **req.model_dump(), agent='human:webui')


@router.get('/sessions/{session_id}/candidates')
def candidates(session_id: str):
    return invoke('list_candidates', session_id)


@router.get('/factors/{factor_id}/history')
def factor_history(factor_id: str, session_id: str | None = None, offset: int = Query(0, ge=0),
                   limit: int = Query(20, ge=1, le=100)):
    from assay.research_inspection import inspect_factor_history
    return inspect_factor_history(invoke('factor_history', factor_id, session_id=session_id),
                                  offset=offset, limit=limit)
