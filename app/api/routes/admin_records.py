"""Job-search pipeline records (applications, assessments, interviews, outcomes): company, role,
stage, date, notes, location, company type."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, status

from app.agents.intake.batch import RecordsBatchIn
from app.api.envelope import Deleted, Envelope, ok
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
) -> Envelope[list[RecordOut]]:
    return ok(list(await _ops(request).list_records(kind)))


@router.post("", status_code=status.HTTP_201_CREATED, summary="Add a record (audited)")
async def create_record(
    body: RecordIn, principal: AdminKey, request: Request
) -> Envelope[RecordOut]:
    return ok(await _ops(request).create_record(body, key_actor(principal.name)), "Record added")


@router.post(
    "/batch",
    status_code=status.HTTP_201_CREATED,
    summary="Add several records in one call (audited)",
)
async def create_records_batch(
    body: RecordsBatchIn, principal: AdminKey, request: Request
) -> Envelope[list[RecordOut]]:
    """Daily application dumps: up to 50 records. Each item is validated like a single create."""
    records = await _ops(request).create_records(body.records, key_actor(principal.name))
    return ok(list(records), f"{len(records)} records added")


@router.patch("/{record_id}", summary="Edit a record (audited)")
async def patch_record(
    record_id: str, body: RecordPatch, principal: AdminKey, request: Request
) -> Envelope[RecordOut]:
    record = await _ops(request).update_record(record_id, body, key_actor(principal.name))
    return ok(record, "Record updated")


@router.delete("/{record_id}", summary="Delete a record (audited)")
async def delete_record(record_id: str, principal: AdminKey, request: Request) -> Envelope[Deleted]:
    await _ops(request).delete_record(record_id, key_actor(principal.name))
    return ok(Deleted(id=record_id), "Record deleted")
