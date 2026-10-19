"""Admin operations shared by the admin routes and the owner intake assistant.

One place for ingestion, review, profile and record writes, so validation, errors and audit are
identical whichever caller made the change. The caller names the actor: `key:<name>` for the
admin API, `agent:intake via key:<name>` for the assistant. Audit details never carry document
text, only ids and what changed.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from beanie import PydanticObjectId
from pydantic import BaseModel, ConfigDict, Field

from app.core.config import Settings, ingest_max_bytes
from app.db.documents import (
    BlobRef,
    IngestMeta,
    JobStatus,
    ReviewStatus,
    SourceFields,
    SourceKind,
    TierLabel,
    utcnow,
)
from app.db.documents import Tier as TierName
from app.exceptions import AppError, ErrorCode
from app.kb.ingestion.fetch.github import check_allowed, parse_allowlist
from app.kb.ingestion.fetch.url import (
    Resolver,
    check_host,
    check_scheme,
    rewrite_google,
    system_resolver,
)
from app.kb.ingestion.jobs import IngestionService
from app.kb.ingestion.labelling import CATEGORIES
from app.kb.ingestion.models import IngestError, IngestErrorCode
from app.kb.ingestion.parsers import dispatch
from app.kb.ingestion.pipeline import Pipeline
from app.kb.publishing import Publisher
from app.kb.store import DocumentView, KbStore, SourceView
from app.kb.vectors import VectorIndex
from app.leads.models import OwnerStatus
from app.leads.store import LeadStore, StoredLead
from app.services.answer_cache import bump_public_version
from app.services.audit import AuditLog
from app.services.profile import (
    ProfileIn,
    ProfileOut,
    ProfileStore,
    RecordIn,
    RecordKind,
    RecordOut,
    RecordPatch,
    RecordStore,
)
from app.storage.blobs import BlobStore

Category = Literal[CATEGORIES]  # type: ignore[valid-type]
MAX_URLS = 20
PREVIEW_CHARS = 500


def key_actor(key_name: str) -> str:
    return f"key:{key_name}"


def intake_actor(key_name: str) -> str:
    return f"agent:intake via key:{key_name}"


# --- ingestion shapes -----------------------------------------------------------------------


class Submitted(BaseModel):
    source_id: str
    status: JobStatus
    # True when this content was already submitted: nothing new was created.
    duplicate: bool
    filename: str | None = None
    uri: str | None = None


class SubmittedBatch(BaseModel):
    sources: list[Submitted]


class SourceStatus(BaseModel):
    id: str
    kind: SourceKind
    status: JobStatus
    error_code: str | None
    error_message: str | None
    attempts: int
    filename: str | None
    uri: str | None
    document_ids: list[str]
    created_at: datetime
    updated_at: datetime


class MetaIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: Category
    tags: list[str] = Field(default_factory=list, max_length=20)
    occurred_at: datetime | None = None
    tier: TierName | None = Field(default=None, description="Owner's hint; can only raise the tier")
    summary: str | None = Field(default=None, max_length=1000)

    def meta(self, title: str | None = None) -> IngestMeta:
        return IngestMeta(
            title=title,
            category=self.category,
            tags=self.tags,
            occurred_at=self.occurred_at,
            tier_hint=self.tier,
            summary=self.summary,
        )


class TextIn(MetaIn):
    title: str = Field(min_length=1, max_length=300)
    body: str = Field(min_length=1, max_length=500_000)


class UrlsIn(MetaIn):
    urls: list[str] = Field(min_length=1, max_length=MAX_URLS)


class GitHubIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repos: list[str] = Field(min_length=1, max_length=50)
    tags: list[str] = Field(default_factory=list, max_length=20)


@dataclass(frozen=True)
class FileIn:
    """One uploaded or inbox file, already read and size-checked."""

    filename: str | None
    content_type: str | None
    data: bytes


# --- review shapes --------------------------------------------------------------------------


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


# --- helpers --------------------------------------------------------------------------------


def parse_id(value: str) -> PydanticObjectId:
    try:
        return PydanticObjectId(value)
    except Exception as exc:
        raise AppError(ErrorCode.NOT_FOUND, "Not found") from exc


def to_app_error(exc: IngestError, **details: Any) -> AppError:
    code = (
        ErrorCode.PAYLOAD_TOO_LARGE
        if exc.code == IngestErrorCode.TOO_LARGE
        else ErrorCode.VALIDATION_ERROR
    )
    extra = {"reason": exc.code.value, **({"detected": exc.detected} if exc.detected else {})}
    return AppError(code, exc.message, details={**details, **extra})


def document_out(doc: DocumentView, *, preview: bool = False) -> DocumentOut:
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


def source_status(source: SourceView) -> SourceStatus:
    return SourceStatus(
        id=str(source.id),
        kind=source.kind,
        status=source.status,
        error_code=source.error_code,
        error_message=source.error_message,
        attempts=source.attempts,
        filename=source.filename,
        uri=source.uri,
        document_ids=[str(d) for d in source.document_ids],
        created_at=source.created_at,
        updated_at=source.updated_at,
    )


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --- the service ----------------------------------------------------------------------------


@dataclass(frozen=True)
class AdminDeps:
    """What the operations touch. `ingestion` is None when the workers are not running."""

    settings: Settings
    kb: KbStore
    vectors: VectorIndex
    blobs: BlobStore
    audit: AuditLog
    profile: ProfileStore
    records: RecordStore
    redis: Any
    ingestion: IngestionService | None = None
    pipeline: Pipeline | None = None
    leads: LeadStore | None = None


class LeadOut(BaseModel):
    """A lead as the owner sees it in the vault."""

    id: str
    created_at: datetime
    source: str
    status: str
    name: str
    email: str
    company: str | None
    role: str | None
    message: str
    location: str | None = None
    employment_type: str | None = None
    link: str | None = None
    summary: str | None = None
    triage: str | None = None
    triage_confidence: float | None = None
    fit_note: str | None = None
    duplicates: int = 0
    attempts: int = 0
    last_error_code: str | None = None
    emailed_at: datetime | None = None
    conversation_id: str | None = None

    @classmethod
    def of(cls, lead: StoredLead) -> LeadOut:
        return cls(
            **lead.model_dump(
                include=set(cls.model_fields) - {"location", "employment_type", "link"}
            ),
            **lead.details.model_dump(),
        )


class LeadPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: OwnerStatus


class AdminOps:
    def __init__(self, deps: AdminDeps) -> None:
        self._d = deps

    @classmethod
    def from_state(cls, state: Any) -> AdminOps:
        """Built from `app.state` (settings, resources, ingestion) on each request."""
        resources = state.resources
        ingestion = getattr(state, "ingestion", None)
        return cls(
            AdminDeps(
                settings=state.settings,
                kb=resources.kb,
                vectors=resources.vectors,
                blobs=resources.blobs,
                audit=resources.audit,
                profile=resources.profile,
                records=resources.records,
                redis=resources.redis,
                ingestion=ingestion.service if ingestion is not None else None,
                pipeline=ingestion.pipeline if ingestion is not None else None,
                leads=getattr(resources, "leads", None),
            )
        )

    @property
    def settings(self) -> Settings:
        return self._d.settings

    async def _audit(self, actor: str, action: str, target: str, details: dict[str, Any]) -> None:
        await self._d.audit.record(actor=actor, action=action, target=target, details=details)

    def _service(self) -> IngestionService:
        if self._d.ingestion is None:
            raise AppError(ErrorCode.DEPENDENCY_UNAVAILABLE, "Ingestion is not running")
        return self._d.ingestion

    def _publisher(self) -> Publisher:
        if self._d.pipeline is None:
            raise AppError(ErrorCode.DEPENDENCY_UNAVAILABLE, "Ingestion is not running")
        redis = self._d.redis
        return Publisher(
            store=self._d.kb,
            vectors=self._d.vectors,
            pipeline=self._d.pipeline,
            on_public_change=lambda: bump_public_version(redis),
        )

    # --- ingestion --------------------------------------------------------------------------

    async def _submit(self, actor: str, fields: SourceFields) -> Submitted:
        source, queued = await self._service().submit(fields)
        duplicate = not queued and source.status != "failed"
        await self._audit(
            actor, "ingest.submit", str(source.id), {"kind": fields.kind, "duplicate": duplicate}
        )
        status_now: JobStatus = "queued" if queued else source.status
        return Submitted(
            source_id=str(source.id),
            status=status_now,
            duplicate=duplicate,
            filename=source.filename,
            uri=source.uri,
        )

    async def ingest_text(self, body: TextIn, actor: str) -> Submitted:
        data = body.body.encode("utf-8")
        ref = await asyncio.to_thread(self._d.blobs.put, data, "text/markdown")
        meta = body.meta(body.title)
        fields = SourceFields(kind="text", checksum=ref.sha256, blob=ref, meta=meta)
        return await self._submit(actor, fields)

    def check_file_count(self, count: int) -> None:
        limit = self._d.settings.ingest_max_files
        if count > limit:
            raise AppError(ErrorCode.VALIDATION_ERROR, f"At most {limit} files per batch")

    def max_file_bytes(self) -> int:
        return ingest_max_bytes(self._d.settings)

    @staticmethod
    def resolve_file(upload: FileIn) -> str:
        """The file's type, detected from its content."""
        try:
            return dispatch.resolve(upload.data, upload.filename)
        except IngestError as exc:
            raise to_app_error(exc, filename=upload.filename) from exc

    async def store_files(
        self, checked: list[tuple[FileIn, str]], meta: MetaIn, actor: str
    ) -> SubmittedBatch:
        """Files already passed `resolve_file`, with their detected type."""
        submitted = []
        for upload, kind in checked:
            ref = await asyncio.to_thread(
                self._d.blobs.put, upload.data, upload.content_type or kind
            )
            fields = SourceFields(
                kind="file",
                filename=upload.filename,
                checksum=ref.sha256,
                blob=ref,
                meta=meta.meta(),
            )
            submitted.append(await self._submit(actor, fields))
        return SubmittedBatch(sources=submitted)

    async def ingest_files(self, files: list[FileIn], meta: MetaIn, actor: str) -> SubmittedBatch:
        """The whole batch is checked first; one bad file rejects the batch and nothing is
        stored."""
        self.check_file_count(len(files))
        checked = [(upload, self.resolve_file(upload)) for upload in files]
        return await self.store_files(checked, meta, actor)

    async def ingest_urls(
        self, body: UrlsIn, actor: str, resolver: Resolver = system_resolver
    ) -> SubmittedBatch:
        targets = []
        for raw in body.urls:
            url = rewrite_google(raw.strip())
            try:
                host, port = check_scheme(url)
                await check_host(host, port, resolver)
            except IngestError as exc:
                raise to_app_error(exc, url=raw) from exc
            targets.append(url)
        submitted = []
        for url in targets:
            fields = SourceFields(
                kind="url", uri=url, checksum=_sha(f"url:{url}".encode()), meta=body.meta()
            )
            submitted.append(await self._submit(actor, fields))
        return SubmittedBatch(sources=submitted)

    def github_allowlist(self) -> frozenset[str]:
        """Repositories GITHUB_SYNC_REPOS allows, normalised to `owner/name`."""
        return parse_allowlist(self._d.settings.github_sync_repos)

    async def ingest_github(self, body: GitHubIn, actor: str) -> SubmittedBatch:
        allowlist = parse_allowlist(self._d.settings.github_sync_repos)
        try:
            repos = [check_allowed(repo, allowlist) for repo in body.repos]
        except IngestError as exc:
            raise to_app_error(exc) from exc
        submitted = []
        for repo in repos:
            meta = IngestMeta(category="project", tags=body.tags)
            fields = SourceFields(
                kind="github", uri=repo, checksum=_sha(f"github:{repo}".encode()), meta=meta
            )
            submitted.append(await self._submit(actor, fields))
        return SubmittedBatch(sources=submitted)

    async def ingest_linkedin(self, upload: FileIn, actor: str) -> Submitted:
        detected = dispatch.detect(upload.data)
        if detected != "zip":
            raise AppError(
                ErrorCode.VALIDATION_ERROR,
                f"Expected a ZIP archive, detected {detected}",
                details={"detected": detected},
            )
        ref = await asyncio.to_thread(self._d.blobs.put, upload.data, "application/zip")
        fields = SourceFields(
            kind="linkedin_export",
            filename=upload.filename,
            checksum=ref.sha256,
            blob=ref,
            meta=IngestMeta(category="persona"),
        )
        return await self._submit(actor, fields)

    async def source_status(self, source_id: str) -> SourceStatus:
        source = await self._service().store.get_source(parse_id(source_id))
        if source is None:
            raise AppError(ErrorCode.NOT_FOUND, "Source not found")
        return source_status(source)

    # --- review -----------------------------------------------------------------------------

    async def review_queue(self, limit: int) -> ReviewList:
        docs = await self._d.kb.review_queue(limit)
        return ReviewList(items=[document_out(d, preview=True) for d in docs])

    async def get_document(self, document_id: str) -> DocumentOut:
        return document_out(await self._publisher().get(parse_id(document_id)))

    async def update_document(
        self, document_id: str, patch: DocumentPatch, actor: str
    ) -> DocumentOut:
        """A tier change moves the vectors to that tier's collection, unpublishes the document
        and resets approval. Restricted removes the text, chunks and vectors."""
        changes = patch.model_dump(exclude_none=True)
        if not changes:
            raise AppError(ErrorCode.VALIDATION_ERROR, "Nothing to change")
        publisher = self._publisher()
        before = await publisher.get(parse_id(document_id))
        after = await publisher.update(before.id, dict(changes))
        await self._audit(
            actor,
            "document.update",
            document_id,
            {"changed": sorted(changes), "tier": {"before": before.tier, "after": after.tier}},
        )
        return document_out(after)

    async def approve(self, document_id: str, actor: str) -> DocumentOut:
        doc = await self._publisher().approve(parse_id(document_id))
        await self._audit(actor, "document.approve", document_id, {"tier": doc.tier})
        return document_out(doc)

    async def publish(self, document_id: str, actor: str) -> PublishResult:
        """Refused (409) unless the document is tier `public` and approved."""
        doc, chunks = await self._publisher().publish(parse_id(document_id))
        await self._audit(actor, "document.publish", document_id, {"chunks": chunks})
        return PublishResult(document=document_out(doc), chunks=chunks)

    async def unpublish(self, document_id: str, actor: str) -> DocumentOut:
        doc = await self._publisher().unpublish(parse_id(document_id))
        await self._audit(actor, "document.unpublish", document_id, {})
        return document_out(doc)

    async def release(self, document_id: str, actor: str) -> PublishResult:
        publisher = self._publisher()
        before = await publisher.get(parse_id(document_id))
        doc, chunks = await publisher.release(before.id)
        await self._audit(
            actor,
            "document.release",
            document_id,
            {"rules": before.quarantine_rules, "chunks": chunks},
        )
        return PublishResult(document=document_out(doc), chunks=chunks)

    async def delete_document(self, document_id: str, actor: str) -> None:
        await self._publisher().delete(parse_id(document_id))
        await self._audit(actor, "document.delete", document_id, {})

    # --- profile ----------------------------------------------------------------------------

    async def get_profile(self) -> ProfileOut:
        return await self._d.profile.get()

    async def _resume_blob(self, document_id: str) -> BlobRef:
        kb = self._d.kb
        doc = await kb.get_document(parse_id(document_id))
        if doc is None or doc.deleted_at is not None:
            raise AppError(ErrorCode.VALIDATION_ERROR, "resume_document_id is not a document")
        source = await kb.get_source(doc.source_id)
        if source is None or source.blob is None:
            raise AppError(ErrorCode.VALIDATION_ERROR, "That document has no stored file")
        return source.blob

    async def put_profile(self, body: ProfileIn, actor: str) -> ProfileOut:
        """`resume_document_id` names an ingested document; its original file becomes the
        downloadable resume."""
        blob = None
        if body.resume_document_id:
            blob = await self._resume_blob(body.resume_document_id)
        profile = await self._d.profile.put(body, blob)
        await self._audit(
            actor, "profile.put", "profile", {"fields": sorted(body.model_fields_set)}
        )
        return profile

    # --- records ----------------------------------------------------------------------------

    async def list_records(self, kind: RecordKind | None) -> list[RecordOut]:
        return await self._d.records.list(kind)

    async def create_record(self, body: RecordIn, actor: str) -> RecordOut:
        record = await self._d.records.create(body)
        await self._audit(actor, "record.create", record.id, {})
        return record

    async def update_record(self, record_id: str, body: RecordPatch, actor: str) -> RecordOut:
        changes = body.model_dump(exclude_unset=True)
        if not changes:
            raise AppError(ErrorCode.VALIDATION_ERROR, "Nothing to change")
        record = await self._d.records.update(parse_id(record_id), changes)
        if record is None:
            raise AppError(ErrorCode.NOT_FOUND, "Record not found")
        await self._audit(actor, "record.update", record_id, {})
        return record

    async def delete_record(self, record_id: str, actor: str) -> None:
        if not await self._d.records.delete(parse_id(record_id)):
            raise AppError(ErrorCode.NOT_FOUND, "Record not found")
        await self._audit(actor, "record.delete", record_id, {})

    # --- leads ------------------------------------------------------------------------------

    def _leads(self) -> LeadStore:
        if self._d.leads is None:
            raise AppError(ErrorCode.DEPENDENCY_UNAVAILABLE, "Leads are not available")
        return self._d.leads

    async def list_leads(
        self, status: str | None = None, triage: str | None = None, limit: int = 50
    ) -> list[LeadOut]:
        return [LeadOut.of(lead) for lead in await self._leads().list(status, triage, limit)]

    async def update_lead(self, lead_id: str, body: LeadPatch, actor: str) -> LeadOut:
        store = self._leads()
        before = await store.get(lead_id)
        if before is None:
            raise AppError(ErrorCode.NOT_FOUND, "Lead not found")
        lead = await store.update(lead_id, {"status": body.status, "updated_at": utcnow()})
        assert lead is not None
        await self._audit(
            actor, "lead.update", lead_id, {"status": body.status, "from": before.status}
        )
        return LeadOut.of(lead)
