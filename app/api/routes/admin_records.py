"""Applications and interviews as structured records (company, role, stage, date, notes)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, status

from app.auth.dependencies import Principal, require_scopes
from app.services.admin_ops import AdminOps, key_actor
from app.services.profile import RecordIn, RecordKind, RecordOut, RecordPatch

router = APIRouter(prefix="/records", tags=["admin: records"])

AdminKey = Annotated[Principal, Depends(require_scopes("admin"))]


def _ops(request: Request) -> AdminOps:
    return AdminOps.from_state(request.app.state)


@router.get("", summary="Records, newest first")
async def list_records(
    _principal: AdminKey, request: Request, kind: Annotated[RecordKind | None, Query()] = None
) -> list[RecordOut]:
    return await _ops(request).list_records(kind)


@router.post("", status_code=status.HTTP_201_CREATED, summary="Add a record (audited)")
async def create_record(body: RecordIn, principal: AdminKey, request: Request) -> RecordOut:
    return await _ops(request).create_record(body, key_actor(principal.name))


@router.patch("/{record_id}", summary="Edit a record (audited)")
async def patch_record(
    record_id: str, body: RecordPatch, principal: AdminKey, request: Request
) -> RecordOut:
    return await _ops(request).update_record(record_id, body, key_actor(principal.name))


@router.delete(
    "/{record_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Delete a record (audited)"
)
async def delete_record(record_id: str, principal: AdminKey, request: Request) -> None:
    await _ops(request).delete_record(record_id, key_actor(principal.name))
