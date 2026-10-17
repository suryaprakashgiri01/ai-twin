"""The owner's profile: availability, notice period, preferred roles, locations, and which
ingested resume is the downloadable one."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from app.api.routes.admin_ingest import parse_id
from app.auth.dependencies import Principal, require_scopes
from app.db.documents import BlobRef
from app.exceptions import AppError, ErrorCode
from app.kb.store import KbStore
from app.services.audit import AuditLog
from app.services.profile import ProfileIn, ProfileOut, ProfileStore

router = APIRouter(prefix="/profile", tags=["admin: profile"])

AdminKey = Annotated[Principal, Depends(require_scopes("admin"))]


def _store(request: Request) -> ProfileStore:
    store: ProfileStore = request.app.state.resources.profile
    return store


async def _resume_blob(kb: KbStore, document_id: str) -> BlobRef:
    doc = await kb.get_document(parse_id(document_id))
    if doc is None or doc.deleted_at is not None:
        raise AppError(ErrorCode.VALIDATION_ERROR, "resume_document_id is not a document")
    source = await kb.get_source(doc.source_id)
    if source is None or source.blob is None:
        raise AppError(ErrorCode.VALIDATION_ERROR, "That document has no stored file")
    return source.blob


@router.get("", summary="The owner's profile")
async def get_profile(_principal: AdminKey, request: Request) -> ProfileOut:
    return await _store(request).get()


@router.put("", summary="Replace the owner's profile (audited)")
async def put_profile(body: ProfileIn, principal: AdminKey, request: Request) -> ProfileOut:
    """`resume_document_id` names an ingested document; its original file becomes the
    downloadable resume."""
    blob = None
    if body.resume_document_id:
        blob = await _resume_blob(request.app.state.resources.kb, body.resume_document_id)
    profile = await _store(request).put(body, blob)
    audit: AuditLog = request.app.state.resources.audit
    await audit.record(
        actor=f"key:{principal.name}",
        action="profile.put",
        target="profile",
        details={"fields": sorted(body.model_fields_set)},
    )
    return profile
