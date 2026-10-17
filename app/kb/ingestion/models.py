"""Types every stage shares: the item flowing through the pipeline and the error taxonomy."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field

from app.db.documents import JobStatus, TierLabel
from app.db.documents import Tier as TierName

__all__ = ["ChunkDraft", "IngestError", "IngestErrorCode", "IngestItem", "JobStatus", "Parsed"]


class IngestErrorCode(StrEnum):
    UNSUPPORTED_TYPE = "UNSUPPORTED_TYPE"
    TYPE_MISMATCH = "TYPE_MISMATCH"
    TOO_LARGE = "TOO_LARGE"
    EMPTY_CONTENT = "EMPTY_CONTENT"
    PARSE_FAILED = "PARSE_FAILED"
    FETCH_BLOCKED = "FETCH_BLOCKED"
    FETCH_FAILED = "FETCH_FAILED"
    NOT_ALLOWED = "NOT_ALLOWED"
    INTERNAL = "INTERNAL"


class IngestError(Exception):
    """An expected ingestion failure. The message is fixed text, never document content."""

    def __init__(self, code: IngestErrorCode, message: str, *, detected: str | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detected = detected


class Parsed(BaseModel):
    """One document's worth of parsed Markdown, before redaction."""

    markdown: str
    detected: str
    title: str | None = None
    category: str | None = None
    ocr_used: bool = False
    # Stable key within a multi-document source (a LinkedIn entity, a repo).
    key: str | None = None


class ChunkDraft(BaseModel):
    ordinal: int
    text: str
    heading: str = ""
    canary: bool = False


class IngestItem(BaseModel):
    """One document moving through redact -> guard -> chunk -> label -> embed."""

    source_id: str
    source_kind: str
    title: str
    category: str
    tags: list[str] = Field(default_factory=list)
    occurred_at: datetime | None = None
    tier_hint: TierName | None = None
    summary: str | None = None
    detected: str = "text"
    # Raw parsed text until `redacted` is True; redacted text after.
    markdown: str
    redacted: bool = False
    redaction_report: dict[str, int] = Field(default_factory=dict)
    quarantine_rules: list[str] = Field(default_factory=list)
    chunks: list[ChunkDraft] = Field(default_factory=list)
    rule_tier: TierName = "private"
    rule_reason: str = ""
    label: TierLabel | None = None
