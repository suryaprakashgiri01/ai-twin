"""Owner review: the queue, document edits, approve, publish/unpublish, release, delete.

Every write is audited with the document id and what changed (never document text).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, status

from app.auth.dependencies import Principal, require_scopes
from app.services.admin_ops import (
    AdminOps,
    DocumentOut,
    DocumentPatch,
    PublishResult,
    ReviewList,
    key_actor,
)

__all__ = ["DocumentOut", "DocumentPatch", "PublishResult", "ReviewList", "router"]

router = APIRouter(tags=["admin: review"])

AdminKey = Annotated[Principal, Depends(require_scopes("admin"))]


def _ops(request: Request) -> AdminOps:
    return AdminOps.from_state(request.app.state)


@router.get("/review", summary="Pending and quarantined documents, newest first")
async def review_queue(
    _principal: AdminKey, request: Request, limit: Annotated[int, Query(ge=1, le=200)] = 50
) -> ReviewList:
    return await _ops(request).review_queue(limit)


@router.get("/documents/{document_id}", summary="One document with its redacted text")
async def get_document(document_id: str, _principal: AdminKey, request: Request) -> DocumentOut:
    return await _ops(request).get_document(document_id)


@router.patch("/documents/{document_id}", summary="Edit title, category, tags or tier")
async def patch_document(
    document_id: str, body: DocumentPatch, principal: AdminKey, request: Request
) -> DocumentOut:
    """A tier change moves the vectors to that tier's collection, unpublishes the document and
    resets approval. Restricted removes the text, chunks and vectors."""
    return await _ops(request).update_document(document_id, body, key_actor(principal.name))


@router.post("/documents/{document_id}/approve", summary="Approve at the current tier")
async def approve(document_id: str, principal: AdminKey, request: Request) -> DocumentOut:
    return await _ops(request).approve(document_id, key_actor(principal.name))


@router.post(
    "/documents/{document_id}/publish", summary="Copy an approved public document to kb_public"
)
async def publish(document_id: str, principal: AdminKey, request: Request) -> PublishResult:
    """Refused (409) unless the document is tier `public` and approved. Canaries never copy."""
    return await _ops(request).publish(document_id, key_actor(principal.name))


@router.post("/documents/{document_id}/unpublish", summary="Remove a document from kb_public")
async def unpublish(document_id: str, principal: AdminKey, request: Request) -> DocumentOut:
    return await _ops(request).unpublish(document_id, key_actor(principal.name))


@router.post(
    "/documents/{document_id}/release", summary="Release a quarantined document for indexing"
)
async def release(document_id: str, principal: AdminKey, request: Request) -> PublishResult:
    return await _ops(request).release(document_id, key_actor(principal.name))


@router.delete(
    "/documents/{document_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Soft delete; vectors removed from every collection",
)
async def delete_document(document_id: str, principal: AdminKey, request: Request) -> None:
    await _ops(request).delete_document(document_id, key_actor(principal.name))
