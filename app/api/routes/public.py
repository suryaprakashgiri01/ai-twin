"""Visitor-facing API, mounted in both profiles; in `public` it is the only API besides health.

Every route needs a key with `public:read`. The portfolio's server route holds that key and
forwards the visitor's address in `X-Visitor-Address`; only its salted hash is used.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Security
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse

from app.auth.dependencies import Principal, api_key_header, require_scopes
from app.auth.visitor import Visitor, resolve_visitor
from app.core.config import Settings
from app.exceptions import AppError, ErrorCode
from app.services.assistant import AssistantInfo, assistant_info
from app.services.public_chat import PublicChat, PublicChatRequest, PublicChatResponse
from app.services.rate_limit import Limits, RateLimiter
from app.services.resume import ResumeProvider, count_download
from app.services.security_events import SecurityEventWriter, first_in_window

PUBLIC_PREFIX = "/v1"
SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}

router = APIRouter(prefix=PUBLIC_PREFIX, tags=["public"])

AUTH_FAILURES = frozenset({ErrorCode.AUTH_REQUIRED, ErrorCode.AUTH_INVALID, ErrorCode.FORBIDDEN})
# A burst of the same failure is recorded once per this many seconds.
AUTH_EVENT_WINDOW = 60

_require_public_read = require_scopes("public:read")


def _events(request: Request) -> SecurityEventWriter | None:
    return getattr(request.app.state.resources, "security_events", None)


async def public_key(
    request: Request, api_key: Annotated[str | None, Security(api_key_header)] = None
) -> Principal:
    """`require_scopes("public:read")`, recording failures as security events."""
    try:
        return await _require_public_read(request, api_key)
    except AppError as exc:
        events = _events(request)
        if (
            exc.code in AUTH_FAILURES
            and events is not None
            and await first_in_window(
                request.app.state.resources.redis, f"auth:{exc.code.value}", AUTH_EVENT_WINDOW
            )
        ):
            await events.record(
                "auth_failed", "medium", rules=[exc.code.value], details={"path": request.url.path}
            )
        raise


PublicKey = Annotated[Principal, Depends(public_key)]


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
        events = _events(request)
        burst = f"rl:{who.hash}:{decision.window}"
        if events is not None and await first_in_window(
            request.app.state.resources.redis, burst, max(decision.retry_after, 1)
        ):
            await events.record(
                "rate_limited",
                "medium",
                rules=[f"rate_limit:{decision.window}"],
                visitor_hash=who.hash,
            )
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


def _resume(request: Request) -> ResumeProvider:
    provider: ResumeProvider | None = getattr(request.app.state, "resume", None)
    if provider is None:
        raise AppError(ErrorCode.NOT_FOUND, "No resume is available")
    return provider


@router.get(
    "/resume",
    summary="The owner's current resume",
    response_class=Response,
    responses={200: {"content": {"application/pdf": {}}}, 404: {"description": "No resume"}},
)
async def resume(request: Request, who: Annotated[Visitor, Depends(visitor)]) -> Response:
    """The vault serves the profile's resume blob; the public deployment, `PUBLIC_RESUME_PATH`.
    Each download is counted, with the visitor known only by their salted hash."""
    found = await _resume(request).open()
    if found is None:
        raise AppError(ErrorCode.NOT_FOUND, "No resume is available")
    await count_download(request.app.state.resources.redis, who.hash)
    disposition = {"Content-Disposition": f'attachment; filename="{found.filename}"'}
    if found.path is not None:
        return FileResponse(found.path, media_type=found.content_type, headers=disposition)
    return Response(found.data, media_type=found.content_type, headers=disposition)


@router.get("/assistant", summary="Greeting, AI-twin disclosure, privacy note, availability")
async def assistant(request: Request, _principal: PublicKey) -> AssistantInfo:
    return await assistant_info(request.app.state.settings, request.app.state.resources.profile)
