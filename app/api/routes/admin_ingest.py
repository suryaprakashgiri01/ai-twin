"""Admin ingestion routes. Every submit is asynchronous: `202` with `source_id`, then poll
`GET /admin/v1/sources/{id}`. Submitting the same content again returns the same source."""

from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime
from typing import Annotated, Any, Literal

from beanie import PydanticObjectId
from fastapi import APIRouter, Depends, File, Form, Request, UploadFile, status
from pydantic import BaseModel, ConfigDict, Field

from app.auth.dependencies import Principal, require_scopes
from app.core.config import Settings, ingest_max_bytes
from app.db.documents import IngestMeta, JobStatus, SourceFields, SourceKind
from app.db.documents import Tier as TierName
from app.exceptions import AppError, ErrorCode
from app.kb.ingestion.fetch.github import check_allowed, parse_allowlist
from app.kb.ingestion.fetch.url import check_host, check_scheme, rewrite_google, system_resolver
from app.kb.ingestion.jobs import IngestionService
from app.kb.ingestion.labelling import CATEGORIES
from app.kb.ingestion.models import IngestError, IngestErrorCode
from app.kb.ingestion.parsers import dispatch
from app.kb.store import SourceView
from app.services.audit import AuditLog
from app.storage.blobs import BlobStore

router = APIRouter(tags=["admin: ingestion"])

AdminKey = Annotated[Principal, Depends(require_scopes("admin"))]
Category = Literal[CATEGORIES]  # type: ignore[valid-type]
MAX_URLS = 20


# --- shapes -----------------------------------------------------------------------------------


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


class _MetaIn(BaseModel):
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


class TextIn(_MetaIn):
    title: str = Field(min_length=1, max_length=300)
    body: str = Field(min_length=1, max_length=500_000)


class UrlsIn(_MetaIn):
    urls: list[str] = Field(min_length=1, max_length=MAX_URLS)


class GitHubIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repos: list[str] = Field(min_length=1, max_length=50)
    tags: list[str] = Field(default_factory=list, max_length=20)


# --- helpers ----------------------------------------------------------------------------------


def _service(request: Request) -> IngestionService:
    ingestion = getattr(request.app.state, "ingestion", None)
    if ingestion is None:
        raise AppError(ErrorCode.DEPENDENCY_UNAVAILABLE, "Ingestion is not running")
    service: IngestionService = ingestion.service
    return service


def _blobs(request: Request) -> BlobStore:
    blobs: BlobStore = request.app.state.resources.blobs
    return blobs


def _audit(request: Request) -> AuditLog:
    audit: AuditLog = request.app.state.resources.audit
    return audit


def _settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def to_app_error(exc: IngestError, **details: Any) -> AppError:
    code = (
        ErrorCode.PAYLOAD_TOO_LARGE
        if exc.code == IngestErrorCode.TOO_LARGE
        else ErrorCode.VALIDATION_ERROR
    )
    extra = {"reason": exc.code.value, **({"detected": exc.detected} if exc.detected else {})}
    return AppError(code, exc.message, details={**details, **extra})


async def _submit(request: Request, principal: Principal, fields: SourceFields) -> Submitted:
    source, queued = await _service(request).submit(fields)
    duplicate = not queued and source.status != "failed"
    await _audit(request).record(
        actor=f"key:{principal.name}",
        action="ingest.submit",
        target=str(source.id),
        details={"kind": fields.kind, "duplicate": duplicate},
    )
    status_now: JobStatus = "queued" if queued else source.status
    return Submitted(
        source_id=str(source.id),
        status=status_now,
        duplicate=duplicate,
        filename=source.filename,
        uri=source.uri,
    )


async def _read_capped(upload: UploadFile, limit: int) -> bytes:
    data = await upload.read(limit + 1)
    if len(data) > limit:
        raise AppError(
            ErrorCode.PAYLOAD_TOO_LARGE,
            f"{upload.filename or 'file'} is over the {limit // (1024 * 1024)} MB limit",
            details={"filename": upload.filename},
        )
    return data


def _form_meta(
    category: str, tags: str, tier: str | None, occurred_at: datetime | None, summary: str | None
) -> _MetaIn:
    try:
        return _MetaIn.model_validate(
            {
                "category": category,
                "tags": [t.strip() for t in tags.split(",") if t.strip()],
                "tier": tier or None,
                "occurred_at": occurred_at,
                "summary": summary or None,
            }
        )
    except ValueError as exc:
        raise AppError(ErrorCode.VALIDATION_ERROR, "Invalid form fields", details=str(exc)) from exc


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --- routes -----------------------------------------------------------------------------------


@router.post("/ingest/text", status_code=status.HTTP_202_ACCEPTED, summary="Ingest a typed note")
async def ingest_text(body: TextIn, principal: AdminKey, request: Request) -> Submitted:
    data = body.body.encode("utf-8")
    ref = await asyncio.to_thread(_blobs(request).put, data, "text/markdown")
    fields = SourceFields(kind="text", checksum=ref.sha256, blob=ref, meta=body.meta(body.title))
    return await _submit(request, principal, fields)


@router.post(
    "/ingest/files",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Ingest a batch of files (type detected from content)",
)
async def ingest_files(
    principal: AdminKey,
    request: Request,
    files: Annotated[
        list[UploadFile],
        File(description="pdf, docx, xlsx, pptx, csv, md, txt, html, png, jpg, webp"),
    ],
    category: Annotated[str, Form()],
    tags: Annotated[str, Form(description="comma-separated")] = "",
    tier: Annotated[str | None, Form()] = None,
    occurred_at: Annotated[datetime | None, Form()] = None,
    summary: Annotated[str | None, Form()] = None,
) -> SubmittedBatch:
    """Up to `INGEST_MAX_FILES` files of `INGEST_MAX_FILE_MB` each. The whole batch is checked
    first; one bad file rejects the batch and nothing is stored."""
    settings = _settings(request)
    if len(files) > settings.ingest_max_files:
        raise AppError(
            ErrorCode.VALIDATION_ERROR, f"At most {settings.ingest_max_files} files per batch"
        )
    meta = _form_meta(category, tags, tier, occurred_at, summary)
    checked: list[tuple[UploadFile, bytes, str]] = []
    for upload in files:
        data = await _read_capped(upload, ingest_max_bytes(settings))
        try:
            kind = dispatch.resolve(data, upload.filename)
        except IngestError as exc:
            raise to_app_error(exc, filename=upload.filename) from exc
        checked.append((upload, data, kind))
    blobs = _blobs(request)
    submitted = []
    for upload, data, kind in checked:
        ref = await asyncio.to_thread(blobs.put, data, upload.content_type or kind)
        fields = SourceFields(
            kind="file", filename=upload.filename, checksum=ref.sha256, blob=ref, meta=meta.meta()
        )
        submitted.append(await _submit(request, principal, fields))
    return SubmittedBatch(sources=submitted)


@router.post(
    "/ingest/urls", status_code=status.HTTP_202_ACCEPTED, summary="Ingest public web pages"
)
async def ingest_urls(body: UrlsIn, principal: AdminKey, request: Request) -> SubmittedBatch:
    """http(s) only; hosts must resolve to public addresses (checked again on every redirect
    when the page is fetched). Public Google Docs/Sheets/Slides links use their export URL."""
    targets = []
    for raw in body.urls:
        url = rewrite_google(raw.strip())
        try:
            host, port = check_scheme(url)
            await check_host(host, port, system_resolver)
        except IngestError as exc:
            raise to_app_error(exc, url=raw) from exc
        targets.append(url)
    submitted = []
    for url in targets:
        fields = SourceFields(
            kind="url", uri=url, checksum=_sha(f"url:{url}".encode()), meta=body.meta()
        )
        submitted.append(await _submit(request, principal, fields))
    return SubmittedBatch(sources=submitted)


@router.post(
    "/ingest/github",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Sync allowlisted GitHub repositories",
)
async def ingest_github(body: GitHubIn, principal: AdminKey, request: Request) -> SubmittedBatch:
    """Only repositories listed in `GITHUB_SYNC_REPOS` are accepted."""
    allowlist = parse_allowlist(_settings(request).github_sync_repos)
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
        submitted.append(await _submit(request, principal, fields))
    return SubmittedBatch(sources=submitted)


@router.post(
    "/ingest/linkedin-export",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Ingest the LinkedIn data export ZIP",
)
async def ingest_linkedin(
    principal: AdminKey,
    request: Request,
    file: Annotated[UploadFile, File(description='LinkedIn "Get a copy of your data" ZIP')],
) -> Submitted:
    """Profile, positions, education, skills, certifications and projects become one document
    each; messages and connections are never read."""
    data = await _read_capped(file, ingest_max_bytes(_settings(request)))
    detected = dispatch.detect(data)
    if detected != "zip":
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            f"Expected a ZIP archive, detected {detected}",
            details={"detected": detected},
        )
    ref = await asyncio.to_thread(_blobs(request).put, data, "application/zip")
    fields = SourceFields(
        kind="linkedin_export",
        filename=file.filename,
        checksum=ref.sha256,
        blob=ref,
        meta=IngestMeta(category="persona"),
    )
    return await _submit(request, principal, fields)


@router.get("/sources/{source_id}", summary="Status of one ingestion job")
async def source_status(source_id: str, _principal: AdminKey, request: Request) -> SourceStatus:
    source = await _service(request).store.get_source(parse_id(source_id))
    if source is None:
        raise AppError(ErrorCode.NOT_FOUND, "Source not found")
    return _status(source)


def parse_id(value: str) -> PydanticObjectId:
    try:
        return PydanticObjectId(value)
    except Exception as exc:
        raise AppError(ErrorCode.NOT_FOUND, "Not found") from exc


def _status(source: SourceView) -> SourceStatus:
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
