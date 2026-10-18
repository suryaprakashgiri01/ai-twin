"""Admin ingestion routes. Every submit is asynchronous: `202` with `source_id`, then poll
`GET /admin/v1/sources/{id}`. Submitting the same content again returns the same source."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile, status

from app.auth.dependencies import Principal, require_scopes
from app.exceptions import AppError, ErrorCode
from app.kb.ingestion.fetch.url import system_resolver
from app.services.admin_ops import (
    MAX_URLS,
    AdminOps,
    Category,
    FileIn,
    GitHubIn,
    MetaIn,
    SourceStatus,
    Submitted,
    SubmittedBatch,
    TextIn,
    UrlsIn,
    key_actor,
    parse_id,
    to_app_error,
)

__all__ = [
    "MAX_URLS",
    "Category",
    "GitHubIn",
    "SourceStatus",
    "Submitted",
    "SubmittedBatch",
    "TextIn",
    "UrlsIn",
    "parse_id",
    "router",
    "to_app_error",
]

router = APIRouter(tags=["admin: ingestion"])

AdminKey = Annotated[Principal, Depends(require_scopes("admin"))]


def _ops(request: Request) -> AdminOps:
    return AdminOps.from_state(request.app.state)


async def _read_capped(upload: UploadFile, limit: int) -> FileIn:
    data = await upload.read(limit + 1)
    if len(data) > limit:
        raise AppError(
            ErrorCode.PAYLOAD_TOO_LARGE,
            f"{upload.filename or 'file'} is over the {limit // (1024 * 1024)} MB limit",
            details={"filename": upload.filename},
        )
    return FileIn(filename=upload.filename, content_type=upload.content_type, data=data)


def _form_meta(
    category: str, tags: str, tier: str | None, occurred_at: datetime | None, summary: str | None
) -> MetaIn:
    try:
        return MetaIn.model_validate(
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


@router.post("/ingest/text", status_code=status.HTTP_202_ACCEPTED, summary="Ingest a typed note")
async def ingest_text(body: TextIn, principal: AdminKey, request: Request) -> Submitted:
    return await _ops(request).ingest_text(body, key_actor(principal.name))


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
    ops = _ops(request)
    ops.check_file_count(len(files))
    meta = _form_meta(category, tags, tier, occurred_at, summary)
    checked: list[tuple[FileIn, str]] = []
    for upload in files:
        read = await _read_capped(upload, ops.max_file_bytes())
        checked.append((read, ops.resolve_file(read)))
    return await ops.store_files(checked, meta, key_actor(principal.name))


@router.post(
    "/ingest/urls", status_code=status.HTTP_202_ACCEPTED, summary="Ingest public web pages"
)
async def ingest_urls(body: UrlsIn, principal: AdminKey, request: Request) -> SubmittedBatch:
    """http(s) only; hosts must resolve to public addresses (checked again on every redirect
    when the page is fetched). Public Google Docs/Sheets/Slides links use their export URL."""
    # Looked up here on each call, so a test can swap the module's resolver.
    return await _ops(request).ingest_urls(body, key_actor(principal.name), system_resolver)


@router.post(
    "/ingest/github",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Sync allowlisted GitHub repositories",
)
async def ingest_github(body: GitHubIn, principal: AdminKey, request: Request) -> SubmittedBatch:
    """Only repositories listed in `GITHUB_SYNC_REPOS` are accepted."""
    return await _ops(request).ingest_github(body, key_actor(principal.name))


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
    ops = _ops(request)
    upload = await _read_capped(file, ops.max_file_bytes())
    return await ops.ingest_linkedin(upload, key_actor(principal.name))


@router.get("/sources/{source_id}", summary="Status of one ingestion job")
async def source_status(source_id: str, _principal: AdminKey, request: Request) -> SourceStatus:
    return await _ops(request).source_status(source_id)
