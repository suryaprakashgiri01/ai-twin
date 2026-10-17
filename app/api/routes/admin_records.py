"""Applications and interviews as structured records (company, role, stage, date, notes)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, status

from app.api.routes.admin_ingest import parse_id
from app.auth.dependencies import Principal, require_scopes
from app.exceptions import AppError, ErrorCode
from app.services.audit import AuditLog
from app.services.profile import RecordIn, RecordKind, RecordOut, RecordPatch, RecordStore

router = APIRouter(prefix="/records", tags=["admin: records"])

AdminKey = Annotated[Principal, Depends(require_scopes("admin"))]


def _store(request: Request) -> RecordStore:
    store: RecordStore = request.app.state.resources.records
    return store


async def _audit(request: Request, principal: Principal, action: str, target: str) -> None:
    audit: AuditLog = request.app.state.resources.audit
    await audit.record(actor=f"key:{principal.name}", action=action, target=target, details={})


@router.get("", summary="Records, newest first")
async def list_records(
    _principal: AdminKey, request: Request, kind: Annotated[RecordKind | None, Query()] = None
) -> list[RecordOut]:
    return await _store(request).list(kind)


@router.post("", status_code=status.HTTP_201_CREATED, summary="Add a record (audited)")
async def create_record(body: RecordIn, principal: AdminKey, request: Request) -> RecordOut:
    record = await _store(request).create(body)
    await _audit(request, principal, "record.create", record.id)
    return record


@router.patch("/{record_id}", summary="Edit a record (audited)")
async def patch_record(
    record_id: str, body: RecordPatch, principal: AdminKey, request: Request
) -> RecordOut:
    changes = body.model_dump(exclude_unset=True)
    if not changes:
        raise AppError(ErrorCode.VALIDATION_ERROR, "Nothing to change")
    record = await _store(request).update(parse_id(record_id), changes)
    if record is None:
        raise AppError(ErrorCode.NOT_FOUND, "Record not found")
    await _audit(request, principal, "record.update", record_id)
    return record


@router.delete(
    "/{record_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Delete a record (audited)"
)
async def delete_record(record_id: str, principal: AdminKey, request: Request) -> None:
    if not await _store(request).delete(parse_id(record_id)):
        raise AppError(ErrorCode.NOT_FOUND, "Record not found")
    await _audit(request, principal, "record.delete", record_id)
