"""Owner memories: the preferences, standing instructions and aliases the owner agents follow.

Learned items arrive `pending` from the conversation digest and reach a prompt only once
accepted. Items created here are `active` at once. Every change is audited.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response, status

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
) -> list[MemoryOut]:
    return await _memory(request).listing(status_)


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    summary="Remember something now (active at once; a duplicate is bumped; audited)",
)
async def create_memory(
    body: MemoryIn, principal: AdminKey, request: Request, response: Response
) -> MemoryOut:
    item, created = await _memory(request).add(body, key_actor(principal.name))
    if not created:
        response.status_code = status.HTTP_200_OK
    return item


@router.patch(
    "/{memory_id}",
    summary="Accept (status active), edit, or archive (status archived) a memory (audited)",
)
async def patch_memory(
    memory_id: str, body: MemoryPatch, principal: AdminKey, request: Request
) -> MemoryOut:
    return await _memory(request).update(memory_id, body, key_actor(principal.name))


@router.delete(
    "/{memory_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Delete a memory (audited)"
)
async def delete_memory(memory_id: str, principal: AdminKey, request: Request) -> None:
    await _memory(request).delete(memory_id, key_actor(principal.name))
