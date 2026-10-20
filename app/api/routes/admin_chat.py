"""Owner chat over the whole vault, and the owner's conversation history."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app.auth.dependencies import Principal, require_scopes
from app.core.config import Settings
from app.exceptions import AppError, ErrorCode
from app.services.audit import AuditLog
from app.services.digest import ConversationDigester, TitleRefresh
from app.services.naming import NAME_MAX, NAME_PATTERN
from app.services.owner_chat import (
    ChatRequest,
    ChatResponse,
    ConversationDetail,
    ConversationSummary,
    OwnerChatService,
    TitleUpdate,
)

router = APIRouter(tags=["admin: chat"])

AdminKey = Annotated[Principal, Depends(require_scopes("admin"))]
ConversationId = Annotated[str, Path(pattern=r"^[0-9a-f]{32}$")]
ConversationName = Annotated[str, Path(pattern=NAME_PATTERN, max_length=NAME_MAX)]

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


@router.get("/conversations/by-name/{name}", summary="One owner conversation, found by its name")
async def conversation_by_name(
    name: ConversationName, _principal: AdminKey, request: Request
) -> ConversationSummary:
    return await _service(request).conversation_by_name(name)


@router.patch(
    "/conversations/{conversation_id}", summary="Rename or re-describe a conversation (audited)"
)
async def update_conversation(
    conversation_id: ConversationId, body: TitleUpdate, principal: AdminKey, request: Request
) -> ConversationSummary:
    summary = await _service(request).update_title(conversation_id, body)
    audit: AuditLog = request.app.state.resources.audit
    await audit.record(
        actor=f"key:{principal.name}",
        action="conversation.update",
        target=conversation_id,
        details={"fields": sorted(body.model_dump(exclude_none=True))},
    )
    return summary


@router.post(
    "/conversations/{conversation_id}/title",
    summary="Refresh the name, description and summary; learn pending memories (local model when "
    "needed)",
)
async def refresh_title(
    conversation_id: ConversationId,
    _principal: AdminKey,
    request: Request,
    wait_seconds: Annotated[
        float | None,
        Query(ge=0, le=120, description="how long to wait before the refresh continues later"),
    ] = None,
) -> TitleRefresh:
    """One model call at the conversation's own tier (intake or confidential → local model
    only). The description and the rolling summary are always refreshed; the name only if the
    owner never renamed it. Memory candidates from the owner's messages are stored as pending
    (`new_memories`). `scheduled` means a slow model is still working and will save its result
    when done. With no `wait_seconds`, intake (and other local-only) conversations use the
    longer local wait."""
    digester: ConversationDigester | None = getattr(request.app.state, "digester", None)
    if digester is None:
        raise AppError(ErrorCode.DEPENDENCY_UNAVAILABLE, "Title refresh is not running")
    settings: Settings = request.app.state.settings
    if wait_seconds is not None:
        wait = wait_seconds
    else:
        detail = await _service(request).conversation(conversation_id, limit=1)
        wait = (
            settings.title_refresh_wait_local_seconds
            if detail.mode == "intake"
            else settings.title_refresh_wait_seconds
        )
    return await digester.refresh_within(conversation_id, wait)


@router.get(
    "/conversations/{conversation_id}",
    summary="One owner conversation with its rolling summary and turns",
)
async def get_conversation(
    conversation_id: ConversationId,
    _principal: AdminKey,
    request: Request,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
) -> ConversationDetail:
    return await _service(request).conversation(conversation_id, limit)
