"""Beanie documents: one class per MongoDB collection, indexes declared on each.

Startup (`init_beanie`) creates any missing index, so declaring one here is the whole change.
Ids are ObjectIds unless a collection has a natural key (`model_overrides`, `profile`) or the id
is minted before the first write (`conversations`, so the Redis cache can key on it).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, ClassVar, Literal

from beanie import Document, PydanticObjectId
from pydantic import BaseModel, Field
from pymongo import ASCENDING, DESCENDING, TEXT, IndexModel

Tier = Literal["public", "private", "confidential", "restricted"]
Channel = Literal["public", "owner"]
Role = Literal["user", "assistant", "system", "tool"]
SourceKind = Literal["text", "file", "url", "github", "linkedin_export"]
ReviewStatus = Literal["pending", "approved", "rejected"]
Scope = Literal["admin", "public:read", "leads:write"]


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_id() -> str:
    return uuid.uuid4().hex


# A TTL index with expireAfterSeconds=0 deletes each document at its own `expires_at`.
def _ttl(field: str) -> IndexModel:
    return IndexModel([(field, ASCENDING)], name=f"{field}_ttl", expireAfterSeconds=0)


class BlobRef(BaseModel):
    """Pointer into the encrypted blob store: SHA-256 of the plaintext plus its metadata."""

    sha256: str
    size: int
    content_type: str


class TierLabel(BaseModel):
    tier: Tier
    reason: str
    by: str
    confidence: float = Field(ge=0, le=1)


class ApiKey(Document):
    name: str
    key_hash: str
    scopes: list[Scope]
    created_at: datetime = Field(default_factory=utcnow)
    revoked_at: datetime | None = None
    last_used_at: datetime | None = None

    class Settings:
        name = "api_keys"
        indexes: ClassVar[list[IndexModel]] = [
            IndexModel([("key_hash", ASCENDING)], name="key_hash_unique", unique=True)
        ]


class Source(Document):
    kind: SourceKind
    uri: str | None = None
    filename: str | None = None
    checksum: str
    blob: BlobRef | None = None
    status: str = "new"
    created_at: datetime = Field(default_factory=utcnow)

    class Settings:
        name = "sources"
        indexes: ClassVar[list[IndexModel]] = [
            IndexModel(
                [("checksum", ASCENDING), ("kind", ASCENDING)],
                name="checksum_kind_unique",
                unique=True,
            )
        ]


class KbDocument(Document):
    source_id: PydanticObjectId
    title: str
    category: str
    tags: list[str] = Field(default_factory=list)
    domain: str = "career"
    tier: Tier = "private"
    label: TierLabel | None = None
    review_status: ReviewStatus = "pending"
    # Default deny: nothing is public until the owner approves it.
    published: bool = False
    published_at: datetime | None = None
    occurred_at: datetime | None = None
    deleted_at: datetime | None = None

    class Settings:
        name = "documents"
        indexes: ClassVar[list[IndexModel]] = [
            IndexModel([("review_status", ASCENDING)], name="review_status"),
            IndexModel([("tier", ASCENDING)], name="tier"),
            IndexModel([("published", ASCENDING)], name="published"),
            IndexModel([("title", TEXT)], name="title_text"),
        ]


class Chunk(Document):
    document_id: PydanticObjectId
    ordinal: int
    text: str  # redacted before it is stored
    tier: Tier
    qdrant_point_id: str
    canary: bool = False
    published: bool = False

    class Settings:
        name = "chunks"
        indexes: ClassVar[list[IndexModel]] = [
            IndexModel(
                [("document_id", ASCENDING), ("ordinal", ASCENDING)],
                name="document_ordinal_unique",
                unique=True,
            )
        ]


class Record(Document):
    kind: Literal["application", "interview"]
    company: str
    role: str
    stage: str
    date: datetime
    notes: str | None = None

    class Settings:
        name = "records"
        indexes: ClassVar[list[IndexModel]] = [
            IndexModel([("kind", ASCENDING), ("date", DESCENDING)], name="kind_date")
        ]


PROFILE_ID = "profile"


class Profile(Document):
    """Singleton (`_id` = "profile"); created by the first migration."""

    id: str = Field(default=PROFILE_ID)  # type: ignore[assignment]
    availability: str = "not looking"
    notice_period: str | None = None
    preferred_roles: list[str] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list)
    resume_blob: BlobRef | None = None
    updated_at: datetime = Field(default_factory=utcnow)

    class Settings:
        name = "profile"


class Lead(Document):
    name: str
    email: str
    company: str | None = None
    role: str | None = None
    message: str
    summary: str | None = None
    triage: str | None = None
    conversation_id: str | None = None
    emailed_at: datetime | None = None
    created_at: datetime = Field(default_factory=utcnow)

    class Settings:
        name = "leads"
        indexes: ClassVar[list[IndexModel]] = [
            IndexModel([("created_at", DESCENDING)], name="created_at")
        ]


class Conversation(Document):
    id: str = Field(default_factory=new_id)  # type: ignore[assignment]
    channel: Channel
    visitor_hash: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    message_count: int = 0
    # Set for public conversations only; the TTL index deletes them then.
    expires_at: datetime | None = None

    class Settings:
        name = "conversations"
        indexes: ClassVar[list[IndexModel]] = [
            _ttl("expires_at"),
            IndexModel(
                [("channel", ASCENDING), ("updated_at", DESCENDING)], name="channel_updated_at"
            ),
        ]


class Message(Document):
    conversation_id: str
    seq: int
    role: Role
    content: str
    intent: str | None = None
    guards: dict[str, Any] = Field(default_factory=dict)
    max_tier: Tier | None = None
    model: str | None = None
    trace_id: str | None = None
    tokens: int | None = None
    latency_ms: float | None = None
    created_at: datetime = Field(default_factory=utcnow)
    expires_at: datetime | None = None

    class Settings:
        name = "messages"
        indexes: ClassVar[list[IndexModel]] = [
            IndexModel(
                [("conversation_id", ASCENDING), ("seq", ASCENDING)],
                name="conversation_seq_unique",
                unique=True,
            ),
            _ttl("expires_at"),
        ]


class SecurityEvent(Document):
    kind: str
    severity: Literal["low", "medium", "high", "critical"]
    details: dict[str, Any] = Field(default_factory=dict)
    visitor_hash: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    alerted_at: datetime | None = None

    class Settings:
        name = "security_events"
        indexes: ClassVar[list[IndexModel]] = [
            IndexModel([("kind", ASCENDING), ("created_at", DESCENDING)], name="kind_time")
        ]


class AuditEntry(Document):
    actor: str
    action: str
    target: str
    details: dict[str, Any] = Field(default_factory=dict)
    at: datetime = Field(default_factory=utcnow)

    class Settings:
        name = "audit_log"
        indexes: ClassVar[list[IndexModel]] = [IndexModel([("at", DESCENDING)], name="at")]


class ModelOverride(Document):
    """`_id` is a model id, or `role:<role>` for a role's fallback chain."""

    id: str  # type: ignore[assignment]
    enabled: bool | None = None
    max_tier: Literal["public", "private", "confidential"] | None = None
    chain: list[str] | None = None
    updated_at: datetime = Field(default_factory=utcnow)

    class Settings:
        name = "model_overrides"


class AppliedMigration(Document):
    """Marker written once a data migration has run; its `_id` is the migration version."""

    id: str  # type: ignore[assignment]
    description: str
    applied_at: datetime = Field(default_factory=utcnow)

    class Settings:
        name = "schema_migrations"


# Every collection Beanie manages; startup creates their indexes.
DOCUMENT_MODELS: list[type[Document]] = [
    ApiKey,
    Source,
    KbDocument,
    Chunk,
    Record,
    Profile,
    Lead,
    Conversation,
    Message,
    SecurityEvent,
    AuditEntry,
    ModelOverride,
    AppliedMigration,
]
