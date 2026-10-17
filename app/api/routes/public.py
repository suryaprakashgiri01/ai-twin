"""Visitor-facing API, mounted in both profiles; in `public` it is the only API besides health.

Every route needs a key with `public:read`. The portfolio's server route holds that key and
forwards the visitor's address in `X-Visitor-Address`; only its salted hash is used.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app.auth.dependencies import Principal, require_scopes
from app.auth.visitor import Visitor, resolve_visitor
from app.core.config import Settings
from app.exceptions import AppError, ErrorCode
from app.services.public_chat import PublicChat, PublicChatRequest, PublicChatResponse
from app.services.rate_limit import Limits, RateLimiter

PUBLIC_PREFIX = "/v1"
SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}

router = APIRouter(prefix=PUBLIC_PREFIX, tags=["public"])

PublicKey = Annotated[Principal, Depends(require_scopes("public:read"))]


def limits(settings: Settings) -> Limits:
    return Limits(
        per_minute=settings.public_messages_per_minute,
        per_day=settings.public_messages_per_day,
        global_per_day=settings.public_daily_message_cap,
    )


async def visitor(request: Request, principal: PublicKey) -> Visitor:
    return resolve_visitor(request, principal)


async def rate_limited_visitor(
    request: Request, who: Annotated[Visitor, Depends(visitor)]
) -> Visitor:
    """429 `RATE_LIMITED` with `Retry-After` once a visitor or the global cap is over."""
    settings: Settings = request.app.state.settings
    limiter = RateLimiter(request.app.state.resources.redis, limits(settings))
    decision = await limiter.hit(who.hash)
    if not decision.allowed:
        raise AppError(
            ErrorCode.RATE_LIMITED,
            "Too many messages; try again later",
            details={"window": decision.window},
            headers={"Retry-After": str(decision.retry_after)},
        )
    return who


def _chat(request: Request) -> PublicChat:
    service: PublicChat | None = getattr(request.app.state, "public_chat", None)
    if service is None:
        raise AppError(ErrorCode.DEPENDENCY_UNAVAILABLE, "Public chat is not running")
    return service


@router.post(
    "/chat",
    summary="Ask the AI twin (SSE by default)",
    response_model=PublicChatResponse,
    responses={200: {"content": {"text/event-stream": {}}}},
)
async def chat(
    body: PublicChatRequest,
    who: Annotated[Visitor, Depends(rate_limited_visitor)],
    request: Request,
    stream: Annotated[bool, Query(description="false returns one JSON body")] = True,
) -> StreamingResponse | JSONResponse:
    """Server-Sent Events, in order: `meta` (conversation id, intent), `token` (answer text,
    sent only after the output guard has passed the whole answer), `sources`, then `done`.
    An unexpected failure ends with `error` instead of `done`. `?stream=false` returns one body.
    """
    service = _chat(request)
    prepared = await service.prepare(body, who)
    if stream:
        return StreamingResponse(
            service.sse(prepared), media_type="text/event-stream", headers=SSE_HEADERS
        )
    response = await service.answer(prepared)
    return JSONResponse(response.model_dump(mode="json"))
