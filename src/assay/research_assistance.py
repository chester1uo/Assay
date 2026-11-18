"""Read-only ID discovery and evidence views for research clients."""
from __future__ import annotations

from difflib import get_close_matches

from assay.research_inspection import summarize_attempt


def resolve_id(workspace, kind: str, query: str, *, session_id: str | None = None) -> dict:
    """Find exact/prefix matches or suggest similar IDs without changing state."""
    if kind not in {'session', 'experiment'}:
        raise ValueError('kind must be session or experiment')
    if not isinstance(query, str) or not 8 <= len(query) <= 100:
        raise ValueError('query must contain between 8 and 100 characters')
    if kind == 'session':
        if session_id is not None:
            raise ValueError('session_id is only used for experiment lookup')
        ids = [item['id'] for item in workspace.list_sessions()]
    else:
        if not isinstance(session_id, str) or not session_id:
            raise ValueError('experiment lookup requires an exact session_id')
        ids = [item['id'] for item in workspace.get_session(session_id)['experiments']]
    ids = sorted(set(ids))
    if query in ids:
        status, matches = 'exact', [query]
    else:
        prefix = [identifier for identifier in ids if identifier.startswith(query)]
        if prefix:
            status, matches = ('unique_prefix' if len(prefix) == 1 else 'ambiguous_prefix'), prefix
        else:
            matches = get_close_matches(query, ids, n=5, cutoff=.8)
            status = 'suggestions' if matches else 'not_found'
    return {'kind': kind, 'query': query, 'status': status, 'candidates': matches[:5],
            'has_more': len(matches) > 5,
            'resolved_id': matches[0] if status in {'exact', 'unique_prefix'} else None,
            'next_action': 'Use an exact ID from candidates; similar IDs are suggestions, not corrections.'}


def research_report(workspace, session_id: str, *, experiment_ids: list[str] | None = None,
                    limit: int = 20) -> dict:
    """Return bounded saved evidence and exact IDs for one research session."""
    if type(limit) is not int or not 1 <= limit <= 50:
        raise ValueError('limit must be an integer from 1 to 50')
    if experiment_ids is not None:
        if (not isinstance(experiment_ids, list) or not 1 <= len(experiment_ids) <= limit
                or any(not isinstance(i, str) or not i for i in experiment_ids)):
            raise ValueError('experiment_ids must contain 1 to limit exact IDs')
        if len(experiment_ids) != len(set(experiment_ids)):
            raise ValueError('experiment_ids must be unique')
    state = workspace.get_session(session_id)
    attempts = state['experiments']
    if experiment_ids is None:
        selected = attempts[-limit:]
    else:
        by_id = {attempt['id']: attempt for attempt in attempts}
        if any(identifier not in by_id for identifier in experiment_ids):
            raise ValueError('experiment_ids must be exact IDs from this session')
        selected = [by_id[identifier] for identifier in experiment_ids]
    return {'session_id': state['id'], 'goal': state['goal'],
            'dataset_id': state['dataset']['id'], 'total_experiments': len(attempts),
            'selection': 'latest' if experiment_ids is None else 'explicit',
            'truncated': experiment_ids is None and len(attempts) > limit,
            'experiment_ids': [attempt['id'] for attempt in selected],
            'experiments': [summarize_attempt(attempt) for attempt in selected]}
