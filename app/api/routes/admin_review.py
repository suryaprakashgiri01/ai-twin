"""Owner review: the queue, document edits, approve, publish/unpublish, release, delete.

Every write is audited with the document id and what changed (never document text).
"""

from __future__ import annotations

from typing import Annotated, get_args

from fastapi import APIRouter, Depends, Request

from app.api.envelope import Deleted, Envelope, ok
from app.api.paging import LIST_SCAN_MAX, ListSpec, Page, PageQuery, page_query, paginate
from app.auth.dependencies import Principal, require_scopes
from app.db.documents import Tier
from app.kb.ingestion.labelling import CATEGORIES
from app.kb.store import REVIEW_STATUSES
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

REVIEW = ListSpec(
    sortable=("createdAt", "updatedAt", "occurredAt", "title", "category", "tier"),
    default_sort="createdAt",
    filters={
        "reviewStatus": REVIEW_STATUSES,
        "tier": get_args(Tier),
        "category": CATEGORIES,
        "label": None,
    },
    search=("title", "summary", "tags", "text"),
)


def _ops(request: Request) -> AdminOps:
    return AdminOps.from_state(request.app.state)


@router.get("/review", summary="Pending and quarantined documents, newest first")
async def review_queue(
    _principal: AdminKey,
    request: Request,
    query: Annotated[PageQuery, Depends(page_query(REVIEW))],
) -> Envelope[Page[DocumentOut]]:
    queue = await _ops(request).review_queue(LIST_SCAN_MAX)
    return ok(paginate(queue.items, query, REVIEW))


@router.get("/documents/{document_id}", summary="One document with its redacted text")
async def get_document(
    document_id: str, _principal: AdminKey, request: Request
) -> Envelope[DocumentOut]:
    return ok(await _ops(request).get_document(document_id))


@router.patch("/documents/{document_id}", summary="Edit title, category, tags or tier")
async def patch_document(
    document_id: str, body: DocumentPatch, principal: AdminKey, request: Request
) -> Envelope[DocumentOut]:
    """A tier change moves the vectors to that tier's collection, unpublishes the document and
    resets approval. Restricted removes the text, chunks and vectors."""
    doc = await _ops(request).update_document(document_id, body, key_actor(principal.name))
    return ok(doc, "Document updated")


@router.post("/documents/{document_id}/approve", summary="Approve at the current tier")
async def approve(document_id: str, principal: AdminKey, request: Request) -> Envelope[DocumentOut]:
    return ok(await _ops(request).approve(document_id, key_actor(principal.name)), "Approved")


@router.post(
    "/documents/{document_id}/publish", summary="Copy an approved public document to kb_public"
)
async def publish(
    document_id: str, principal: AdminKey, request: Request
) -> Envelope[PublishResult]:
    """Refused (409) unless the document is tier `public` and approved. Canaries never copy."""
    return ok(await _ops(request).publish(document_id, key_actor(principal.name)), "Published")


@router.post("/documents/{document_id}/unpublish", summary="Remove a document from kb_public")
async def unpublish(
    document_id: str, principal: AdminKey, request: Request
) -> Envelope[DocumentOut]:
    doc = await _ops(request).unpublish(document_id, key_actor(principal.name))
    return ok(doc, "Unpublished")


@router.post(
    "/documents/{document_id}/release", summary="Release a quarantined document for indexing"
)
async def release(
    document_id: str, principal: AdminKey, request: Request
) -> Envelope[PublishResult]:
    return ok(await _ops(request).release(document_id, key_actor(principal.name)), "Released")


@router.delete(
    "/documents/{document_id}", summary="Soft delete; vectors removed from every collection"
)
async def delete_document(
    document_id: str, principal: AdminKey, request: Request
) -> Envelope[Deleted]:
    await _ops(request).delete_document(document_id, key_actor(principal.name))
    return ok(Deleted(id=document_id), "Document deleted")
