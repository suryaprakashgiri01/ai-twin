"""Saved conversation starters for the console's chat, per agent."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from app.api.envelope import Deleted, Envelope, ok
from app.auth.dependencies import Principal, require_scopes
from app.exceptions import AppError, ErrorCode
from app.services.audit import AuditLog
from app.services.starters import STARTERS_MAX, Starter, StarterAgent, StarterIn, StarterStore

router = APIRouter(prefix="/starters", tags=["admin: console"])

AdminKey = Annotated[Principal, Depends(require_scopes("admin"))]


def _store(request: Request) -> StarterStore:
    store: StarterStore | None = getattr(request.app.state.resources, "starters", None)
    if store is None:
        raise AppError(ErrorCode.SERVICE_UNAVAILABLE, "Starters are not available")
    return store


@router.get("", summary="Saved conversation starters, oldest first (optionally one agent's)")
async def list_starters(
    _principal: AdminKey,
    request: Request,
    agent: Annotated[
        StarterAgent | None, Query(description="super, ask, prep, intake or public")
    ] = None,
) -> Envelope[list[Starter]]:
    return ok(await _store(request).list(agent))


@router.post("", status_code=201, summary="Save a conversation starter for one agent (audited)")
async def add_starter(body: StarterIn, principal: AdminKey, request: Request) -> Envelope[Starter]:
    store = _store(request)
    if len(await store.list(body.agent)) >= STARTERS_MAX:
        raise AppError(ErrorCode.CONFLICT, f"At most {STARTERS_MAX} starters per agent")
    saved = await store.add(body)
    audit: AuditLog = request.app.state.resources.audit
    await audit.record(
        actor=f"key:{principal.name}",
        action="starter.add",
        target=saved.id,
        details={"agent": body.agent},
    )
    return ok(saved, "Starter saved")


@router.delete("/{starter_id}", summary="Delete a saved starter (audited)")
async def delete_starter(
    starter_id: str, principal: AdminKey, request: Request
) -> Envelope[Deleted]:
    if not await _store(request).delete(starter_id):
        raise AppError(ErrorCode.NOT_FOUND, "Starter not found")
    audit: AuditLog = request.app.state.resources.audit
    await audit.record(
        actor=f"key:{principal.name}", action="starter.delete", target=starter_id, details={}
    )
    return ok(Deleted(id=starter_id), "Starter deleted")
