"""Prep: interview and application answers in the owner's voice (SSE by default)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app.api.envelope import Envelope, ok
from app.auth.dependencies import Principal, require_scopes
from app.exceptions import AppError, ErrorCode
from app.services.prep import PrepRequest, PrepResponse, PrepService

router = APIRouter(tags=["admin: prep"])

AdminKey = Annotated[Principal, Depends(require_scopes("admin"))]
SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


def _service(request: Request) -> PrepService:
    service: PrepService | None = getattr(request.app.state, "prep", None)
    if service is None:
        raise AppError(ErrorCode.SERVICE_UNAVAILABLE, "Prep is not running")
    return service


@router.post(
    "/prep",
    summary="Answer an interview or application question as the owner (SSE by default)",
    response_model=Envelope[PrepResponse],
    responses={200: {"content": {"text/event-stream": {}}}},
)
async def prep(
    body: PrepRequest,
    _principal: AdminKey,
    request: Request,
    stream: Annotated[bool, Query(description="false returns one JSON body")] = True,
) -> StreamingResponse | JSONResponse:
    """Events: `meta` (`kind`: behavioural, scenario, application, pitch or project; the
    sub-queries), `token` (the answer, already through the outward-safety filter), `sources`,
    then `done` (`tier`, `model`, `removed`: internal details rewritten, `checks`: what to verify
    before sending). `words`/`chars` set a length limit; `revise` (shorter, longer, variant)
    rewrites the previous answer. Confidential sources are read by local models only."""
    service = _service(request)
    prepared = await service.prepare(body)
    if stream:
        return StreamingResponse(
            service.sse(prepared), media_type="text/event-stream", headers=SSE_HEADERS
        )
    return JSONResponse(ok(await service.answer(prepared)).model_dump(mode="json", by_alias=True))
