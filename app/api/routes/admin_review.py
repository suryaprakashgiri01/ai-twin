"""Owner review: the queue, document edits, approve, publish/unpublish, release, delete.

Every write is audited with the document id and what changed (never document text).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field

from app.api.routes.admin_ingest import Category, parse_id
from app.auth.dependencies import Principal, require_scopes
from app.db.documents import ReviewStatus, TierLabel
from app.db.documents import Tier as TierName
from app.exceptions import AppError, ErrorCode
from app.kb.publishing import Publisher
from app.kb.store import DocumentView
from app.services.audit import AuditLog

router = APIRouter(tags=["admin: review"])

AdminKey = Annotated[Principal, Depends(require_scopes("admin"))]
PREVIEW_CHARS = 500


class DocumentOut(BaseModel):
    id: str
    source_id: str
    title: str
    category: str
    tags: list[str]
    tier: TierName
    label: TierLabel | None
    review_status: ReviewStatus
    published: bool
    quarantine_rules: list[str]
    redaction_report: dict[str, int]
    summary: str | None
    occurred_at: datetime | None
    created_at: datetime
    updated_at: datetime
    approved_at: datetime | None
    published_at: datetime | None
    # Redacted text: the full text on a single document, a preview in the review list.
    text: str | None


class ReviewList(BaseModel):
    items: list[DocumentOut]


class PublishResult(BaseModel):
    document: DocumentOut
    chunks: int


class DocumentPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=300)
    category: Category | None = None
    tags: list[str] | None = Field(default=None, max_length=20)
    tier: TierName | None = None


def _out(doc: DocumentView, *, preview: bool = False) -> DocumentOut:
    text = doc.text[:PREVIEW_CHARS] if preview and doc.text else doc.text
    return DocumentOut(
        id=str(doc.id),
        source_id=str(doc.source_id),
        title=doc.title,
        category=doc.category,
        tags=doc.tags,
        tier=doc.tier,
        label=doc.label,
        review_status=doc.review_status,
        published=doc.published,
        quarantine_rules=doc.quarantine_rules,
        redaction_report=doc.redaction_report,
        summary=doc.summary,
        occurred_at=doc.occurred_at,
        created_at=doc.created_at,
        updated_at=doc.updated_at,
        approved_at=doc.approved_at,
        published_at=doc.published_at,
        text=text,
    )


def _publisher(request: Request) -> Publisher:
    ingestion = getattr(request.app.state, "ingestion", None)
    if ingestion is None:
        raise AppError(ErrorCode.DEPENDENCY_UNAVAILABLE, "Ingestion is not running")
    resources = request.app.state.resources
    return Publisher(store=resources.kb, vectors=resources.vectors, pipeline=ingestion.pipeline)


async def _audit(
    request: Request, principal: Principal, action: str, target: str, details: dict[str, Any]
) -> None:
    audit: AuditLog = request.app.state.resources.audit
    await audit.record(actor=f"key:{principal.name}", action=action, target=target, details=details)


@router.get("/review", summary="Pending and quarantined documents, newest first")
async def review_queue(
    _principal: AdminKey, request: Request, limit: Annotated[int, Query(ge=1, le=200)] = 50
) -> ReviewList:
    docs = await request.app.state.resources.kb.review_queue(limit)
    return ReviewList(items=[_out(d, preview=True) for d in docs])


@router.get("/documents/{document_id}", summary="One document with its redacted text")
async def get_document(document_id: str, _principal: AdminKey, request: Request) -> DocumentOut:
    return _out(await _publisher(request).get(parse_id(document_id)))


@router.patch("/documents/{document_id}", summary="Edit title, category, tags or tier")
async def patch_document(
    document_id: str, body: DocumentPatch, principal: AdminKey, request: Request
) -> DocumentOut:
    """A tier change moves the vectors to that tier's collection, unpublishes the document and
    resets approval. Restricted removes the text, chunks and vectors."""
    changes = body.model_dump(exclude_none=True)
    if not changes:
        raise AppError(ErrorCode.VALIDATION_ERROR, "Nothing to change")
    publisher = _publisher(request)
    before = await publisher.get(parse_id(document_id))
    after = await publisher.update(before.id, dict(changes))
    await _audit(
        request,
        principal,
        "document.update",
        document_id,
        {
            "changed": sorted(changes),
            "tier": {"before": before.tier, "after": after.tier},
        },
    )
    return _out(after)


@router.post("/documents/{document_id}/approve", summary="Approve at the current tier")
async def approve(document_id: str, principal: AdminKey, request: Request) -> DocumentOut:
    doc = await _publisher(request).approve(parse_id(document_id))
    await _audit(request, principal, "document.approve", document_id, {"tier": doc.tier})
    return _out(doc)


@router.post(
    "/documents/{document_id}/publish", summary="Copy an approved public document to kb_public"
)
async def publish(document_id: str, principal: AdminKey, request: Request) -> PublishResult:
    """Refused (409) unless the document is tier `public` and approved. Canaries never copy."""
    doc, chunks = await _publisher(request).publish(parse_id(document_id))
    await _audit(request, principal, "document.publish", document_id, {"chunks": chunks})
    return PublishResult(document=_out(doc), chunks=chunks)


@router.post("/documents/{document_id}/unpublish", summary="Remove a document from kb_public")
async def unpublish(document_id: str, principal: AdminKey, request: Request) -> DocumentOut:
    doc = await _publisher(request).unpublish(parse_id(document_id))
    await _audit(request, principal, "document.unpublish", document_id, {})
    return _out(doc)


@router.post(
    "/documents/{document_id}/release", summary="Release a quarantined document for indexing"
)
async def release(document_id: str, principal: AdminKey, request: Request) -> PublishResult:
    publisher = _publisher(request)
    before = await publisher.get(parse_id(document_id))
    doc, chunks = await publisher.release(before.id)
    await _audit(
        request,
        principal,
        "document.release",
        document_id,
        {"rules": before.quarantine_rules, "chunks": chunks},
    )
    return PublishResult(document=_out(doc), chunks=chunks)


@router.delete(
    "/documents/{document_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Soft delete; vectors removed from every collection",
)
async def delete_document(document_id: str, principal: AdminKey, request: Request) -> None:
    await _publisher(request).delete(parse_id(document_id))
    await _audit(request, principal, "document.delete", document_id, {})
