"""Factor evaluation routes (architecture §4.2).

* ``POST /v1/factor/evaluate`` — blocking JSON report (``stream=false``) or an SSE
  ``text/event-stream`` of ``eval.*`` events (``stream=true``).
* ``POST /v1/factor/batch``    — parallel evaluation, returns ``{total, elapsed_ms, reports}``.

Both mount under the ``/v1/factor`` prefix (see :mod:`assay.api.app`). The service is
resolved via the lazy :func:`~assay.api.app.get_service` dependency so importing the
app needs no credentials and a missing data store surfaces as HTTP 503.

SSE framing follows the spec exactly: ``event: <name>\\ndata: <json>\\n\\n`` per event.
"""

from __future__ import annotations

import json
import math
import time
from typing import Any, AsyncGenerator

from fastapi import APIRouter, Body, Depends, Header
from sse_starlette.sse import EventSourceResponse

from assay.api.app import get_service
from assay.api.auth import get_api_key
from assay.api.models import BatchRequest, BatchResponse, EvaluateRequest
from assay.engine import detect_dialect, iter_fields, iter_ops, lint, parse
from assay.engine.ast import FieldNode, LitNode, OpNode

router = APIRouter()


def _ast_to_dict(node) -> Any:
    """Render the unified AST as a compact, JSON-serialisable nested structure.

    ``OpNode`` -> ``{"op": name, "args": [...]}``; ``FieldNode`` -> ``{"field": name}``;
    ``LitNode`` -> ``{"lit": value}``. Used by ``/lint`` so the editor can show the
    parsed tree without a round-trip through the numeric core.
    """
    if isinstance(node, OpNode):
        return {"op": node.op, "args": [_ast_to_dict(c) for c in node.children]}
    if isinstance(node, FieldNode):
        return {"field": node.name}
    if isinstance(node, LitNode):
        return {"lit": node.value}
    return str(node)


def _json_safe(obj: Any) -> Any:
    """Recursively replace non-finite floats (NaN/Inf) with ``None``.

    ``json.dumps`` defaults to ``allow_nan=True`` and emits bare ``NaN``/``Infinity``
    tokens, which are **invalid JSON** that the browser's ``JSON.parse`` rejects —
    so a streamed ``ic_series`` with NaN warm-up values would break the SSE consumer.
    The blocking path is safe because ``FactorReport.to_dict()`` already does this;
    intermediate SSE events (``ic_series``/``groups``) do not, so we sanitise here.
    """
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    return obj


def _sse_iter(events: AsyncGenerator[dict, None]) -> AsyncGenerator[dict, None]:
    """Adapt service ``{"event", "data"}`` dicts to sse-starlette's event payloads.

    sse-starlette serialises ``{"event": name, "data": str}`` into the exact
    ``event: <name>\\ndata: <json>\\n\\n`` frame the architecture §4.2 contract shows.
    Payloads are NaN-sanitised so every frame is valid JSON for ``JSON.parse``.
    """

    async def _gen() -> AsyncGenerator[dict, None]:
        async for ev in events:
            data = json.dumps(_json_safe(ev["data"]), default=str, allow_nan=False)
            yield {"event": ev["event"], "data": data}

    return _gen()


@router.post("/evaluate")
async def evaluate_factor(
    req: EvaluateRequest,
    x_session_id: str | None = Header(default=None, alias="X-Session-Id"),
    api_key: str | None = Depends(get_api_key),
) -> Any:
    """Evaluate one factor — JSON report, or an SSE stream when ``stream=true``.

    The ``X-Session-Id`` header (architecture §4.2 usage note) takes precedence over
    the body's ``session_id`` so the WebUI can bind a request to its active session
    without rewriting the body. Diagnostic failures are returned *in the report*
    (``failure_mode`` / ``suggestion``), not as HTTP errors — only transport/data
    failures raise (mapped to §4.6 by the app's exception handler).
    """
    svc = get_service()
    kwargs = req.service_kwargs()
    if x_session_id:  # header overrides body session_id
        kwargs["session_id"] = x_session_id

    if req.stream:
        events = svc.stream(req.expr, **kwargs)
        return EventSourceResponse(_sse_iter(events))

    report = svc.evaluate(req.expr, **kwargs)
    return report.to_dict()


@router.post("/lint")
async def lint_factor(
    expr: str = Body(..., embed=True),
    api_key: str | None = Depends(get_api_key),
) -> dict[str, Any]:
    """Parse-only diagnostics for the editor — data-free, fast, never 5xx on bad input.

    Returns the detected ``dialect``, the ``canonical`` round-tripped string, the sorted
    ``fields`` / ``operators`` used, a compact nested ``ast`` (``null`` on parse failure),
    and the full ``diagnostics`` envelope from :func:`assay.engine.lint` (which never
    raises — syntax errors land in ``diagnostics.status == 'error'``). No service / panel
    is touched, so this works on a credential-less deployment.
    """
    diagnostics = lint(expr).to_dict()
    dialect = detect_dialect(expr)
    try:
        node = parse(expr)
    except Exception:  # parse failure is reported via diagnostics; keep this 200
        return {
            "dialect": dialect,
            "canonical": None,
            "fields": [],
            "operators": [],
            "ast": None,
            "diagnostics": diagnostics,
        }
    return {
        "dialect": dialect,
        "canonical": str(node),
        "fields": sorted(iter_fields(node)),
        "operators": sorted(iter_ops(node)),
        "ast": _ast_to_dict(node),
        "diagnostics": diagnostics,
    }


@router.post("/batch", response_model=BatchResponse)
async def batch_factors(
    req: BatchRequest,
    api_key: str | None = Depends(get_api_key),
) -> BatchResponse:
    """Evaluate many factors in parallel; returns reports sorted by ``sort_by`` desc."""
    svc = get_service()
    t0 = time.perf_counter()
    reports = svc.batch(req.exprs, **req.service_kwargs())
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    return BatchResponse(
        total=len(reports),
        elapsed_ms=elapsed_ms,
        reports=[r.to_dict() for r in reports],
    )
