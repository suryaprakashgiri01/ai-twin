"""The super agent: one conversation that plans and delegates to intake, ask and prep."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from app.auth.dependencies import Principal, require_scopes
from app.exceptions import AppError, ErrorCode
from app.services.super_agent import SuperAgent, SuperRequest

router = APIRouter(tags=["admin: super agent"])

AdminKey = Annotated[Principal, Depends(require_scopes("admin"))]
SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


def _agent(request: Request) -> SuperAgent:
    agent: SuperAgent | None = getattr(request.app.state, "super_agent", None)
    if agent is None:
        raise AppError(ErrorCode.SERVICE_UNAVAILABLE, "The super agent is not running")
    return agent


@router.post(
    "/super",
    summary="One message to every agent: plan, delegate, answer (SSE)",
    responses={200: {"content": {"text/event-stream": {}}}},
)
async def super_agent(
    body: SuperRequest, principal: AdminKey, request: Request
) -> StreamingResponse:
    """Events: `meta` (the plan: up to four steps, each `intake`, `ask`, `prep` or `chat`, and
    whether a model or the rules made it), `step` (each step running, then done, pending or
    error, with its model and tier), `token` (the composed answer), `sources`, then `done`
    (tier, steps, pending, models, latency) or `error`. Intake drafts that only add or update
    data are confirmed automatically; deletes, publishing and tier changes wait for a yes in the
    next message. Confidential steps run on local models only."""
    agent = _agent(request)
    prepared = await agent.prepare(body, principal.name)
    return StreamingResponse(
        agent.sse(prepared), media_type="text/event-stream", headers=SSE_HEADERS
    )
