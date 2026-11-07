"""Research workbench REST adapter over the same durable workspace as MCP."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from assay.api.app import get_service
from assay.api.auth import get_api_key

router = APIRouter(dependencies=[Depends(get_api_key)])


def invoke(method, *args, **kwargs):
    try:
        return getattr(get_service().research, method)(*args, **kwargs)
    except ValueError as exc:
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


@router.post('/sessions/{session_id}/experiments')
def evaluate(session_id: str, req: ExperimentRequest):
    # Sync route runs in FastAPI's thread pool; reads remain available during evaluation.
    return invoke('evaluate', session_id, **req.model_dump(), agent='human:webui')


@router.post('/sessions/{session_id}/notes')
def note(session_id: str, req: NoteRequest):
    return invoke('add_note', session_id, req.text, agent='human:webui')
