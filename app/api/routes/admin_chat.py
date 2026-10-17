"""Owner chat over the whole vault, and the owner's conversation history."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app.auth.dependencies import Principal, require_scopes
from app.exceptions import AppError, ErrorCode
from app.services.owner_chat import (
    ChatRequest,
    ChatResponse,
    ConversationDetail,
    ConversationSummary,
    OwnerChatService,
)

router = APIRouter(tags=["admin: chat"])

AdminKey = Annotated[Principal, Depends(require_scopes("admin"))]
ConversationId = Annotated[str, Path(pattern=r"^[0-9a-f]{32}$")]

SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


def _service(request: Request) -> OwnerChatService:
    service: OwnerChatService | None = getattr(request.app.state, "owner_chat", None)
    if service is None:
        raise AppError(ErrorCode.DEPENDENCY_UNAVAILABLE, "Owner chat is not running")
    return service


@router.post(
    "/chat",
    summary="Ask the vault (SSE by default)",
    response_model=ChatResponse,
    responses={200: {"content": {"text/event-stream": {}}}},
)
async def chat(
    body: ChatRequest,
    _principal: AdminKey,
    request: Request,
    stream: Annotated[bool, Query(description="false returns one JSON body")] = True,
) -> StreamingResponse | JSONResponse:
    """Server-Sent Events, in order: `meta` (route, rewritten query), `token` (answer text),
    `sources` (cited documents), then `done` (tier, model, citations, withheld, latency, tokens).
    A failure ends with `error` (`code`, `message`, `incomplete`) instead of `done`; the partial
    answer is stored and marked incomplete. `?stream=false` returns the same fields as one body.
    """
    service = _service(request)
    prepared = await service.prepare(body)
    if stream:
        return StreamingResponse(
            service.sse(prepared), media_type="text/event-stream", headers=SSE_HEADERS
        )
    response = await service.answer(prepared)
    return JSONResponse(response.model_dump(mode="json"))


@router.get("/conversations", summary="Owner conversations, newest first")
async def list_conversations(
    _principal: AdminKey,
    request: Request,
    limit: Annotated[int, Query(ge=1, le=200)] = 20,
) -> list[ConversationSummary]:
    return await _service(request).list_conversations(limit)


@router.get("/conversations/{conversation_id}", summary="One owner conversation with its turns")
async def get_conversation(
    conversation_id: ConversationId,
    _principal: AdminKey,
    request: Request,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
) -> ConversationDetail:
    return await _service(request).conversation(conversation_id, limit)
