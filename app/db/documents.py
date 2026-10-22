"""Beanie documents: one class per MongoDB collection, indexes declared on each.

Startup (`init_beanie`) creates any missing index, so declaring one here is the whole change.
Ids are ObjectIds unless a collection has a natural key (`model_overrides`, `profile`) or the id
is minted before the first write (`conversations`, so the Redis cache can key on it).

Sources, documents, records, leads, owner memories and security events also carry a `uid`
(UUID v4): the only id the API shows or accepts. Internal references keep the ObjectId.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from typing import Any, ClassVar, Literal

from beanie import Document, PydanticObjectId
from pydantic import BaseModel, Field
from pymongo import ASCENDING, DESCENDING, TEXT, IndexModel

Tier = Literal["public", "private", "confidential", "restricted"]
Channel = Literal["public", "owner"]
# Owner conversations: `ask` questions the vault, `intake` adds knowledge through the assistant.
ConversationMode = Literal["ask", "intake", "prep", "super"]
Role = Literal["user", "assistant", "system", "tool"]
SourceKind = Literal["text", "file", "url", "github", "linkedin_export"]
ReviewStatus = Literal["pending", "approved", "rejected", "quarantined"]
# An ingestion job moves left to right; the last three are terminal.
JobStatus = Literal[
    "queued", "parsing", "redacting", "labelling", "embedding", "done", "quarantined", "failed"
]
TERMINAL_STATUSES: frozenset[str] = frozenset({"done", "quarantined", "failed"})
Scope = Literal["admin", "public:read", "leads:write"]
# The job-search pipeline, in order: an invite from a recruiter or job platform, apply, an HR or
# recruiter call, take-home or online assessment, interviews, outcome.
RecordKind = Literal[
    "invite",
    "application",
    "hr_call",
    "assessment",
    "interview",
    "offer",
    "rejection",
    "withdrawal",
]
RECORD_KINDS: tuple[str, ...] = (
    "invite",
    "application",
    "hr_call",
    "assessment",
    "interview",
    "offer",
    "rejection",
    "withdrawal",
)
# Where the role is based: on-site, remote, or a mix.
WorkMode = Literal["onsite", "remote", "hybrid"]
WORK_MODES: tuple[str, ...] = ("onsite", "remote", "hybrid")
# What kind of employer the owner tagged (product / service / research / …).
CompanyType = Literal["product", "service", "research", "startup", "other"]
COMPANY_TYPES: tuple[str, ...] = ("product", "service", "research", "startup", "other")
# Owner memories: how the owner likes answers, standing rules, and names they use.
MemoryKind = Literal["instruction", "preference", "alias"]
MEMORY_KINDS: tuple[str, ...] = ("instruction", "preference", "alias")
MemoryStatus = Literal["pending", "active", "archived"]
MemoryTier = Literal["private", "confidential"]


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_id() -> str:
    return uuid.uuid4().hex


def new_uid() -> str:
    return str(uuid.uuid4())


# A public id anywhere in free text (the owner may paste one into the intake chat).
UID_PATTERN = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b", re.I
)


# Partial, so startup can create it before migration 0007 has given older rows a `uid`.
UID_INDEX = IndexModel(
    [("uid", ASCENDING)],
    name="uid_unique",
    unique=True,
    partialFilterExpression={"uid": {"$type": "string"}},
)


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


class IngestMeta(BaseModel):
    """What the owner said about a source when submitting it; applied to its documents."""

    title: str | None = None
    category: str | None = None
    tags: list[str] = Field(default_factory=list)
    occurred_at: datetime | None = None
    tier_hint: Tier | None = None
    summary: str | None = None


class SourceFields(BaseModel):
    """A submitted source and its ingestion job. Shared by the document and the store views."""

    uid: str = Field(default_factory=new_uid)
    kind: SourceKind
    uri: str | None = None
    filename: str | None = None
    checksum: str
    blob: BlobRef | None = None
    meta: IngestMeta = Field(default_factory=IngestMeta)
    status: JobStatus = "queued"
    # A code and a fixed message only: never document content.
    error_code: str | None = None
    error_message: str | None = None
    attempts: int = 0
    document_ids: list[PydanticObjectId] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class Source(SourceFields, Document):
    class Settings:
        name = "sources"
        indexes: ClassVar[list[IndexModel]] = [
            IndexModel(
                [("checksum", ASCENDING), ("kind", ASCENDING)],
                name="checksum_kind_unique",
                unique=True,
            ),
            IndexModel([("status", ASCENDING)], name="status"),
            UID_INDEX,
        ]


class KbDocumentFields(BaseModel):
    """One knowledge-base document: its label, review state and redacted text."""

    uid: str = Field(default_factory=new_uid)
    source_id: PydanticObjectId
    source_uid: str | None = None
    title: str
    category: str
    tags: list[str] = Field(default_factory=list)
    domain: str = "career"
    tier: Tier = "private"
    label: TierLabel | None = None
    review_status: ReviewStatus = "pending"
    # Default deny: nothing is public until the owner approves it.
    published: bool = False
    # A quarantined document the owner released for their own use: indexed at confidential
    # (local models only) and never publishable while set.
    local_only: bool = False
    published_at: datetime | None = None
    approved_at: datetime | None = None
    occurred_at: datetime | None = None
    deleted_at: datetime | None = None
    # Redacted Markdown, shown in review. None for restricted documents (metadata only).
    text: str | None = None
    summary: str | None = None
    # A short model-written summary, indexed as the document's card point (see kb.ingestion.cards).
    card: str | None = None
    # Denylist rule names that matched (never the matched text).
    quarantine_rules: list[str] = Field(default_factory=list)
    # Placeholder type -> count; values are never stored.
    redaction_report: dict[str, int] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class KbDocument(KbDocumentFields, Document):
    class Settings:
        name = "documents"
        indexes: ClassVar[list[IndexModel]] = [
            IndexModel([("review_status", ASCENDING)], name="review_status"),
            IndexModel([("source_id", ASCENDING)], name="source_id"),
            IndexModel([("tier", ASCENDING)], name="tier"),
            IndexModel([("published", ASCENDING)], name="published"),
            IndexModel([("title", TEXT)], name="title_text"),
            UID_INDEX,
        ]


class ChunkFields(BaseModel):
    document_id: PydanticObjectId
    ordinal: int
    text: str  # redacted before it is stored
    tier: Tier
    qdrant_point_id: str
    canary: bool = False
    published: bool = False


class Chunk(ChunkFields, Document):
    class Settings:
        name = "chunks"
        indexes: ClassVar[list[IndexModel]] = [
            IndexModel(
                [("document_id", ASCENDING), ("ordinal", ASCENDING)],
                name="document_ordinal_unique",
                unique=True,
            )
        ]


class Canary(Document):
    """A random marker planted in one chunk of a private or confidential document.

    The public output guard treats any marker seen in a public answer as a critical incident.
    """

    document_id: PydanticObjectId
    marker: str
    created_at: datetime = Field(default_factory=utcnow)

    class Settings:
        name = "canaries"
        indexes: ClassVar[list[IndexModel]] = [
            IndexModel([("marker", ASCENDING)], name="marker_unique", unique=True),
            IndexModel([("document_id", ASCENDING)], name="document_id"),
        ]


class Record(Document):
    uid: str = Field(default_factory=new_uid)
    kind: RecordKind
    company: str
    role: str
    stage: str
    date: datetime
    notes: str | None = None
    # Where it came from: a job platform (Naukri, LinkedIn, ...), a referral, a recruiter.
    platform: str | None = None
    # The HR or recruiter contact as the owner gave it (name, phone, e-mail). Third-party
    # details: shown to the owner and the local intake model only, never in ask-chat answers.
    contact: str | None = None
    # Where the role is based, as the owner said it (city, region, country).
    location: str | None = None
    work_mode: WorkMode | None = None
    # Employer shape as the owner tagged it (product-based, service-based, …).
    company_type: CompanyType | None = None
    # Extra attributes the owner labelled (referral, salary range, …); at most 10 short keys.
    extras: dict[str, str] = Field(default_factory=dict)

    class Settings:
        name = "records"
        indexes: ClassVar[list[IndexModel]] = [
            IndexModel([("kind", ASCENDING), ("date", DESCENDING)], name="kind_date"),
            UID_INDEX,
        ]


PROFILE_ID = "profile"


class Profile(Document):
    """Singleton (`_id` = "profile"); created by the first migration."""

    id: str = Field(default=PROFILE_ID)  # type: ignore[assignment]
    availability: str = "not looking"
    notice_period: str | None = None
    preferred_roles: list[str] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list)
    # How soon the owner usually replies ("within two working days"); shown after a lead.
    reply_time: str | None = None
    resume_blob: BlobRef | None = None
    resume_document_id: PydanticObjectId | None = None
    resume_document_uid: str | None = None
    updated_at: datetime = Field(default_factory=utcnow)

    class Settings:
        name = "profile"


class LeadDetails(BaseModel):
    location: str | None = None
    employment_type: str | None = None
    # Stored as text: never fetched, never rendered as a link.
    link: str | None = None


class LeadFields(BaseModel):
    """An opportunity from the public assistant or the form endpoint.

    `email` is stored as given (validated); logs carry only a salted hash of it. `email_key` and
    `company_key` are the case-folded forms duplicates are matched on. Delivery: a `new` lead with
    no `emailed_at` is due for its email at `next_attempt_at`; after the last attempt it becomes
    `failed`. A `suppressed` (spam) lead is never emailed; it goes into the daily digest.
    """

    uid: str = Field(default_factory=new_uid)
    name: str
    email: str
    company: str | None = None
    role: str | None = None
    message: str
    details: LeadDetails = Field(default_factory=LeadDetails)
    source: Literal["assistant", "form"] = "form"
    status: Literal["new", "reviewed", "replied", "archived", "suppressed", "failed"] = "new"
    visitor_hash: str | None = None
    email_key: str | None = None
    company_key: str | None = None
    summary: str | None = None
    triage: str | None = None
    triage_confidence: float | None = None
    fit_note: str | None = None
    conversation_id: str | None = None
    # Later submissions of the same email + company within a day, merged into this lead.
    duplicates: int = 0
    attempts: int = 0
    last_error_code: str | None = None
    next_attempt_at: datetime | None = None
    emailed_at: datetime | None = None
    digested_at: datetime | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    # LEAD_RETENTION_DAYS after creation; the TTL index deletes the lead then.
    expires_at: datetime | None = None


class Lead(LeadFields, Document):
    class Settings:
        name = "leads"
        indexes: ClassVar[list[IndexModel]] = [
            IndexModel([("created_at", DESCENDING)], name="created_at"),
            IndexModel(
                [("email_key", ASCENDING), ("company_key", ASCENDING), ("created_at", DESCENDING)],
                name="dedupe",
            ),
            IndexModel([("status", ASCENDING), ("created_at", DESCENDING)], name="status_time"),
            _ttl("expires_at"),
            UID_INDEX,
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
    # Owner conversations only: a short unique slug and a one-line description.
    name: str | None = None
    description: str | None = None
    mode: ConversationMode = "ask"
    # True once the owner renames it; the automatic title refresh then never changes the name.
    name_locked: bool = False
    # message_count when the title was last refreshed, so an unchanged chat is not re-titled.
    titled_turns: int = 0
    # A rolling summary of the conversation (local model when the content requires), the
    # message_count it covers, and the highest tier of what it was made from.
    summary: str | None = None
    summarized_turns: int = 0
    summary_tier: Tier | None = None

    class Settings:
        name = "conversations"
        indexes: ClassVar[list[IndexModel]] = [
            _ttl("expires_at"),
            IndexModel(
                [("channel", ASCENDING), ("updated_at", DESCENDING)], name="channel_updated_at"
            ),
            IndexModel([("channel", ASCENDING), ("name", ASCENDING)], name="channel_name"),
        ]


class OwnerMemoryFields(BaseModel):
    """One short statement about how the owner likes to work; never a career fact.

    Learned items start `pending` and reach a prompt only once the owner accepts them. `norm` is
    the normalised text duplicates are matched on; it is unique among items not archived.
    """

    uid: str = Field(default_factory=new_uid)
    kind: MemoryKind
    text: str = Field(min_length=1, max_length=200)
    status: MemoryStatus = "pending"
    tier: MemoryTier = "private"
    confidence: float = Field(default=1.0, ge=0, le=1)
    source_conversation_id: str | None = None
    norm: str
    uses: int = 1
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class OwnerMemory(OwnerMemoryFields, Document):
    class Settings:
        name = "owner_memories"
        indexes: ClassVar[list[IndexModel]] = [
            IndexModel([("status", ASCENDING), ("kind", ASCENDING)], name="status_kind"),
            IndexModel(
                [("norm", ASCENDING)],
                name="norm_live_unique",
                unique=True,
                partialFilterExpression={"status": {"$in": ["pending", "active"]}},
            ),
            UID_INDEX,
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
    route: str | None = None
    # Document ids cited by an assistant turn.
    citations: list[str] = Field(default_factory=list)
    # True when generation failed after streaming started; `content` is the partial answer.
    incomplete: bool = False
    # Sources left out because no eligible model could see their tier.
    withheld: int = 0
    # A public answer served from the answer cache.
    cache_hit: bool = False
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
    uid: str = Field(default_factory=new_uid)
    kind: str
    severity: Literal["low", "medium", "high", "critical"]
    details: dict[str, Any] = Field(default_factory=dict)
    visitor_hash: str | None = None
    # Where an auth failure happened: `admin` or `public`; None for other kinds.
    surface: Literal["admin", "public"] | None = None
    created_at: datetime = Field(default_factory=utcnow)
    # Set by the alert worker once the event went out in an alert email, or in a daily digest.
    alerted_at: datetime | None = None
    digested_at: datetime | None = None

    class Settings:
        name = "security_events"
        indexes: ClassVar[list[IndexModel]] = [
            IndexModel([("kind", ASCENDING), ("created_at", DESCENDING)], name="kind_time"),
            IndexModel([("alerted_at", ASCENDING), ("created_at", DESCENDING)], name="alert_time"),
            IndexModel(
                [("digested_at", ASCENDING), ("created_at", DESCENDING)], name="digest_time"
            ),
            IndexModel([("created_at", DESCENDING)], name="time"),
            UID_INDEX,
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


class CheckpointEntry(Document):
    """One LangGraph checkpoint of the intake assistant, keyed by conversation (`thread_id`).

    `checkpoint` and `metadata` are LangGraph-serialised bytes. Finished threads are pruned after
    the retention period; a thread waiting on a confirmation is kept.
    """

    thread_id: str
    checkpoint_ns: str = ""
    checkpoint_id: str
    parent_checkpoint_id: str | None = None
    checkpoint_type: str
    checkpoint: bytes
    metadata_type: str
    metadata: bytes
    updated_at: datetime = Field(default_factory=utcnow)

    class Settings:
        name = "checkpoints"
        indexes: ClassVar[list[IndexModel]] = [
            IndexModel(
                [
                    ("thread_id", ASCENDING),
                    ("checkpoint_ns", ASCENDING),
                    ("checkpoint_id", DESCENDING),
                ],
                name="thread_checkpoint",
                unique=True,
            ),
            IndexModel([("updated_at", ASCENDING)], name="updated_at"),
        ]


class CheckpointWrite(Document):
    """A pending write recorded against one checkpoint (task output, interrupt, resume value)."""

    thread_id: str
    checkpoint_ns: str = ""
    checkpoint_id: str
    task_id: str
    idx: int
    channel: str
    value_type: str
    value: bytes
    task_path: str = ""
    updated_at: datetime = Field(default_factory=utcnow)

    class Settings:
        name = "checkpoint_writes"
        indexes: ClassVar[list[IndexModel]] = [
            IndexModel(
                [
                    ("thread_id", ASCENDING),
                    ("checkpoint_ns", ASCENDING),
                    ("checkpoint_id", ASCENDING),
                    ("task_id", ASCENDING),
                    ("idx", ASCENDING),
                ],
                name="thread_checkpoint_task",
                unique=True,
            ),
        ]


# Every collection Beanie manages; startup creates their indexes.
DOCUMENT_MODELS: list[type[Document]] = [
    ApiKey,
    Source,
    KbDocument,
    Chunk,
    Canary,
    Record,
    Profile,
    Lead,
    Conversation,
    Message,
    OwnerMemory,
    SecurityEvent,
    AuditEntry,
    ModelOverride,
    AppliedMigration,
    CheckpointEntry,
    CheckpointWrite,
]
