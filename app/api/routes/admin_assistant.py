"""The owner intake assistant: add knowledge in plain language, confirm, and it is recorded."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Path, Request
from fastapi.responses import StreamingResponse

from app.auth.dependencies import Principal, require_scopes
from app.exceptions import AppError, ErrorCode
from app.services.intake import AssistantRequest, AssistantState, IntakeService

router = APIRouter(tags=["admin: assistant"])

AdminKey = Annotated[Principal, Depends(require_scopes("admin"))]
ConversationId = Annotated[str, Path(pattern=r"^[0-9a-f]{32}$")]

SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


def _service(request: Request) -> IntakeService:
    service: IntakeService | None = getattr(request.app.state, "intake", None)
    if service is None:
        raise AppError(ErrorCode.DEPENDENCY_UNAVAILABLE, "The intake assistant is not running")
    return service


@router.post(
    "/assistant",
    summary="Add knowledge in plain language (SSE)",
    responses={200: {"content": {"text/event-stream": {}}}},
)
async def assistant(
    body: AssistantRequest, principal: AdminKey, request: Request
) -> StreamingResponse:
    """Server-Sent Events, in order: `meta` (conversation id, resumed), `token` (the reply, or
    the owner chat answer for a question), `result` (after a write: ids, job status, proposed
    tier), `draft` (a summary and its JSON waiting for yes / no / an edit; deletes then ask for
    the title or company to be retyped), then `done` (status, action, pending). A failure ends
    with `error`. Nothing is written before an explicit yes. Runs on local models only: with
    none available the request fails with 503 and the Ollama hint, before any model call."""
    service = _service(request)
    prepared = await service.prepare(body, principal.name)
    return StreamingResponse(
        service.sse(prepared), media_type="text/event-stream", headers=SSE_HEADERS
    )


@router.get("/assistant/{conversation_id}", summary="A conversation's mode and pending draft")
async def assistant_state(
    conversation_id: ConversationId, _principal: AdminKey, request: Request
) -> AssistantState:
    return await _service(request).state(conversation_id)
