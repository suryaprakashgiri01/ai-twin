"""Owner memories: the preferences, standing instructions and aliases the owner agents follow.

Learned items arrive `pending` from the conversation digest and reach a prompt only once
accepted. Items created here are `active` at once. Every change is audited.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response, status

from app.api.envelope import Deleted, Envelope, ok
from app.auth.dependencies import Principal, require_scopes
from app.db.documents import MemoryStatus
from app.exceptions import AppError, ErrorCode
from app.services.admin_ops import key_actor
from app.services.memory import MemoryIn, MemoryOut, MemoryPatch, MemoryService

router = APIRouter(prefix="/memories", tags=["admin: memories"])

AdminKey = Annotated[Principal, Depends(require_scopes("admin"))]


def _memory(request: Request) -> MemoryService:
    service: MemoryService | None = getattr(request.app.state, "memory", None)
    if service is None:
        raise AppError(ErrorCode.SERVICE_UNAVAILABLE, "Owner memory is not running")
    return service


@router.get("", summary="Owner memories: pending and active by default, newest first")
async def list_memories(
    _principal: AdminKey,
    request: Request,
    status_: Annotated[MemoryStatus | None, Query(alias="status")] = None,
) -> Envelope[list[MemoryOut]]:
    return ok(await _memory(request).listing(status_))


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    summary="Remember something now (active at once; a duplicate is bumped; audited)",
)
async def create_memory(
    body: MemoryIn, principal: AdminKey, request: Request, response: Response
) -> Envelope[MemoryOut]:
    item, created = await _memory(request).add(body, key_actor(principal.name))
    if not created:
        response.status_code = status.HTTP_200_OK
    return ok(item, "Remembered" if created else "Already remembered; bumped")


@router.patch(
    "/{memory_id}",
    summary="Accept (status active), edit, or archive (status archived) a memory (audited)",
)
async def patch_memory(
    memory_id: str, body: MemoryPatch, principal: AdminKey, request: Request
) -> Envelope[MemoryOut]:
    item = await _memory(request).update(memory_id, body, key_actor(principal.name))
    return ok(item, "Memory updated")


@router.delete("/{memory_id}", summary="Delete a memory (audited)")
async def delete_memory(memory_id: str, principal: AdminKey, request: Request) -> Envelope[Deleted]:
    await _memory(request).delete(memory_id, key_actor(principal.name))
    return ok(Deleted(id=memory_id), "Memory deleted")
