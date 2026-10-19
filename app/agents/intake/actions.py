"""The intake action catalog: each action's fields, its validating model, whether it writes or
destroys, how its draft reads, and the `AdminOps` call that carries it out.

Payload keys are the owner-facing field names. Document and record actions name their target
(`document`, `record`) in the owner's words; code resolves it to an id before the draft is shown
(`document_id`/`document_title`, `record_id`/`record_label`/`record_company`).
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.agents.intake.drafts import InboxError, resolve_inbox, show_day
from app.db.documents import TERMINAL_STATUSES
from app.exceptions import AppError, ErrorCode
from app.kb.ingestion.labelling import CATEGORIES
from app.services.admin_ops import (
    AdminOps,
    DocumentOut,
    DocumentPatch,
    FileIn,
    GitHubIn,
    MetaIn,
    SourceStatus,
    TextIn,
    UrlsIn,
)
from app.services.memory import MemoryIn, MemoryOut, MemoryService
from app.services.profile import ProfileIn, RecordIn, RecordOut, RecordPatch

Payload = dict[str, Any]
Outcome = dict[str, Any]

# An id anywhere in the reference: the model often copies a whole line from an earlier reply.
_OBJECT_ID = re.compile(r"\b[0-9a-f]{24}\b")
PRONOUNS = frozenset(
    {
        "it",
        "that",
        "this",
        "the note",
        "that note",
        "this note",
        "the document",
        "that document",
        "the file",
        "that file",
        "the record",
        "that record",
        "the interview",
        "that interview",
        "the last one",
        "last one",
    }
)
REVIEW_SCAN = 200
MIN_NAME = 3
POLL_SECONDS = 0.5
BODY_PREVIEW = 300
LEADS_SHOWN = 10


class InboxFileIn(MetaIn):
    model_config = ConfigDict(extra="forbid")

    filename: str = Field(min_length=1, max_length=255)


class RecordChanges(RecordPatch):
    """`update_record` validates the changes only; the target is resolved separately."""


@dataclass(frozen=True)
class RunContext:
    ops: AdminOps
    actor: str
    inbox: Path
    wait_seconds: float
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    memory: MemoryService | None = None


Runner = Callable[[RunContext, Payload], Awaitable[Outcome]]


@dataclass(frozen=True)
class ActionSpec:
    name: str
    label: str
    description: str
    # Field -> hint for the extraction prompt.
    fields: Mapping[str, str]
    required: tuple[str, ...] = ()
    # At least one of these must be set (a change to make).
    needs_any: tuple[str, ...] = ()
    model: type[BaseModel] | None = None
    write: bool = True
    destructive: bool = False
    target: str | None = None  # "document" | "record" | "memory"
    date_fields: tuple[str, ...] = ()
    list_fields: tuple[str, ...] = ()
    lower_fields: tuple[str, ...] = ()
    # Fields the model may infer rather than copy (a category, a record's kind, a note's title).
    inferred: tuple[str, ...] = ()
    resolved: tuple[str, ...] = ()
    run: Runner | None = None
    # Payload keys passed to the model; None means every field.
    model_keys: tuple[str, ...] | None = None
    hidden: frozenset[str] = field(default_factory=frozenset)

    def model_input(self, payload: Payload) -> Payload:
        keys = self.model_keys if self.model_keys is not None else tuple(self.fields)
        return {k: v for k, v in payload.items() if k in keys}

    def summary(self, payload: Payload) -> str:
        lines = [self.label]
        if self.target == "document" and payload.get("document_title"):
            lines.append(f"  document: {payload['document_title']} ({payload.get('document_id')})")
        if self.name == "publish" and payload.get("document_tier") == "private":
            lines.append("  tier: private → public (changed first, then approved and published)")
        if self.target == "record" and payload.get("record_label"):
            lines.append(f"  record: {payload['record_label']}")
        if self.target == "memory" and payload.get("memory_text"):
            lines.append(f"  memory: {payload['memory_text']} ({payload.get('memory_id')})")
        for key in self.fields:
            if key in (self.target or "",) or key in self.hidden or payload.get(key) is None:
                continue
            lines.append(f"  {key}: {_show(key, payload[key], self)}")
        return "\n".join(lines)

    def confirm_text(self, payload: Payload) -> str | None:
        """What the owner must retype to confirm a delete."""
        if not self.destructive:
            return None
        return str(payload.get("document_title") or payload.get("record_company") or "")


def _show(key: str, value: Any, spec: ActionSpec) -> str:
    if key in spec.date_fields:
        return show_day(value)
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    text = str(value)
    if key == "body" and len(text) > BODY_PREVIEW:
        return text[: BODY_PREVIEW - 1] + "…"
    return text


def json_safe(payload: Payload) -> Payload:
    out: Payload = {}
    for key, value in payload.items():
        if isinstance(value, datetime):
            out[key] = (
                value.date().isoformat()
                if value == datetime(value.year, value.month, value.day)
                else value.isoformat()
            )
        elif isinstance(value, date):
            out[key] = value.isoformat()
        else:
            out[key] = value
    return out


# --- outcomes ----------------------------------------------------------------------------------


def _doc_info(doc: DocumentOut) -> dict[str, Any]:
    return {
        "id": doc.id,
        "title": doc.title,
        "tier": doc.tier,
        "review_status": doc.review_status,
        "published": doc.published,
        "reason": doc.label.reason if doc.label else None,
        "quarantine_rules": doc.quarantine_rules,
    }


async def _wait(ctx: RunContext, source_id: str) -> SourceStatus:
    started = time.monotonic()
    while True:
        status = await ctx.ops.source_status(source_id)
        if status.status in TERMINAL_STATUSES or time.monotonic() - started >= ctx.wait_seconds:
            return status
        await ctx.sleep(POLL_SECONDS)


def _source_line(status: SourceStatus, docs: list[dict[str, Any]], duplicate: bool) -> str:
    name = status.filename or status.uri or "note"
    head = f"{name}: source {status.id}, status {status.status}"
    if duplicate:
        head += " (already submitted earlier; nothing new created)"
    parts = [head]
    for doc in docs:
        line = f"  “{doc['title']}” ({doc['id']}): proposed tier {doc['tier']}"
        if doc.get("reason"):
            line += f" because {doc['reason']}"
        line += f"; review {doc['review_status']}"
        if doc["quarantine_rules"]:
            line += f"; quarantined by {', '.join(doc['quarantine_rules'])}"
        parts.append(line)
    if status.status == "failed":
        parts.append(f"  failed: {status.error_message or status.error_code}")
    elif status.status not in TERMINAL_STATUSES:
        parts.append(f"  still {status.status}; ask me for the status of source {status.id} later")
    return "\n".join(parts)


async def _ingest_outcome(ctx: RunContext, action: str, submitted: list[Any]) -> Outcome:
    sources = []
    lines = []
    for item in submitted:
        status = await _wait(ctx, item.source_id)
        docs = [_doc_info(await ctx.ops.get_document(d)) for d in status.document_ids]
        sources.append(
            {
                "source_id": status.id,
                "status": status.status,
                "duplicate": item.duplicate,
                "documents": docs,
            }
        )
        lines.append(_source_line(status, docs, item.duplicate))
    return {"action": action, "message": "\n".join(lines), "sources": sources}


def _documents_of(outcome: Outcome) -> list[dict[str, Any]]:
    return [d for s in outcome.get("sources", []) for d in s["documents"]]


# --- runners -----------------------------------------------------------------------------------


def _meta_fields(payload: Payload) -> Payload:
    return {
        k: payload[k]
        for k in ("category", "tags", "occurred_at", "tier", "summary")
        if k in payload
    }


async def _add_note(ctx: RunContext, p: Payload) -> Outcome:
    body = TextIn.model_validate({**_meta_fields(p), "title": p["title"], "body": p["body"]})
    return await _ingest_outcome(ctx, "add_note", [await ctx.ops.ingest_text(body, ctx.actor)])


async def _ingest_url(ctx: RunContext, p: Payload) -> Outcome:
    body = UrlsIn.model_validate({**_meta_fields(p), "urls": p["urls"]})
    batch = await ctx.ops.ingest_urls(body, ctx.actor)
    return await _ingest_outcome(ctx, "ingest_url", batch.sources)


async def _ingest_file(ctx: RunContext, p: Payload) -> Outcome:
    try:
        path = resolve_inbox(ctx.inbox, str(p["filename"]))
    except InboxError as exc:
        raise AppError(ErrorCode.VALIDATION_ERROR, str(exc)) from exc
    limit = ctx.ops.max_file_bytes()
    if path.stat().st_size > limit:
        raise AppError(
            ErrorCode.PAYLOAD_TOO_LARGE,
            f"{path.name} is over the {limit // (1024 * 1024)} MB limit",
        )
    data = await asyncio.to_thread(path.read_bytes)
    meta = MetaIn.model_validate(_meta_fields(p))
    upload = FileIn(filename=path.name, content_type=None, data=data)
    batch = await ctx.ops.ingest_files([upload], meta, ctx.actor)
    return await _ingest_outcome(ctx, "ingest_file", batch.sources)


async def _ingest_github(ctx: RunContext, p: Payload) -> Outcome:
    body = GitHubIn.model_validate({"repos": p["repos"], "tags": p.get("tags", [])})
    batch = await ctx.ops.ingest_github(body, ctx.actor)
    return await _ingest_outcome(ctx, "ingest_github", batch.sources)


def _record_line(record: RecordOut) -> str:
    return (
        f"{record.kind} {record.company} · {record.role} · {record.stage} · "
        f"{show_day(record.date)} ({record.id})"
    )


async def _add_record(ctx: RunContext, p: Payload) -> Outcome:
    record = await ctx.ops.create_record(RecordIn.model_validate(p), ctx.actor)
    return {
        "action": "add_record",
        "message": f"Saved {_record_line(record)}",
        "record": record.model_dump(mode="json"),
    }


async def _update_record(ctx: RunContext, p: Payload) -> Outcome:
    changes = RecordChanges.model_validate(UPDATE_RECORD.model_input(p))
    record = await ctx.ops.update_record(str(p["record_id"]), changes, ctx.actor)
    return {
        "action": "update_record",
        "message": f"Updated {_record_line(record)}",
        "record": record.model_dump(mode="json"),
    }


async def _delete_record(ctx: RunContext, p: Payload) -> Outcome:
    await ctx.ops.delete_record(str(p["record_id"]), ctx.actor)
    return {
        "action": "delete_record",
        "message": f"Deleted {p.get('record_label')}",
        "record_id": p["record_id"],
    }


async def _update_profile(ctx: RunContext, p: Payload) -> Outcome:
    current = await ctx.ops.get_profile()
    merged = current.model_dump(include=set(ProfileIn.model_fields))
    merged.update({k: v for k, v in p.items() if k in PROFILE_FIELDS})
    profile = await ctx.ops.put_profile(ProfileIn.model_validate(merged), ctx.actor)
    return {
        "action": "update_profile",
        "message": f"Profile updated: {', '.join(sorted(k for k in p if k in PROFILE_FIELDS))}",
        "profile": profile.model_dump(mode="json", exclude={"resume_blob"}),
    }


async def _show_review(ctx: RunContext, p: Payload) -> Outcome:
    queue = await ctx.ops.review_queue(20)
    if not queue.items:
        return {"action": "show_review", "message": "Nothing waits for review.", "documents": []}
    lines = [f"{len(queue.items)} waiting for review:"]
    lines += [
        f"  “{d.title}” ({d.id}): {d.category}, tier {d.tier}, {d.review_status}"
        for d in queue.items
    ]
    return {
        "action": "show_review",
        "message": "\n".join(lines),
        "documents": [_doc_info(d) for d in queue.items],
    }


async def _show_leads(ctx: RunContext, p: Payload) -> Outcome:
    leads = await ctx.ops.list_leads(limit=LEADS_SHOWN)
    if not leads:
        return {"action": "show_leads", "message": "No leads yet.", "leads": []}
    lines = [f"{len(leads)} most recent leads:"]
    lines += [
        f"  {show_day(lead.created_at)}: {lead.name}, {lead.role} at {lead.company} "
        f"({lead.status}, {lead.triage or 'untriaged'}) ({lead.id})"
        for lead in leads
    ]
    return {
        "action": "show_leads",
        "message": "\n".join(lines),
        "leads": [{"id": lead.id, "status": lead.status, "triage": lead.triage} for lead in leads],
    }


async def _source_status(ctx: RunContext, p: Payload) -> Outcome:
    status = await ctx.ops.source_status(str(p["source_id"]))
    docs = [_doc_info(await ctx.ops.get_document(d)) for d in status.document_ids]
    return {
        "action": "source_status",
        "message": _source_line(status, docs, False),
        "sources": [
            {"source_id": status.id, "status": status.status, "duplicate": False, "documents": docs}
        ],
    }


def _doc_outcome(action: str, verb: str, doc: DocumentOut, **extra: Any) -> Outcome:
    return {
        "action": action,
        "message": f"{verb} “{doc.title}” ({doc.id}): tier {doc.tier}, review "
        f"{doc.review_status}, {'published' if doc.published else 'not published'}",
        "documents": [_doc_info(doc)],
        **extra,
    }


async def _approve(ctx: RunContext, p: Payload) -> Outcome:
    return _doc_outcome("approve", "Approved", await ctx.ops.approve(p["document_id"], ctx.actor))


async def _publish(ctx: RunContext, p: Payload) -> Outcome:
    """The owner asked to publish and confirmed a draft that said what would change: a private
    document is made public and approved first. Confidential and restricted ones never get here
    (refused while resolving the draft)."""
    doc_id = p["document_id"]
    doc = await ctx.ops.get_document(doc_id)
    steps = []
    if doc.tier == "private":
        await ctx.ops.update_document(doc_id, DocumentPatch(tier="public"), ctx.actor)
        steps.append("made public")
    if doc.review_status != "approved":
        await ctx.ops.approve(doc_id, ctx.actor)
        steps.append("approved")
    result = await ctx.ops.publish(doc_id, ctx.actor)
    label = ", ".join([*steps, "published"]).capitalize()
    return _doc_outcome("publish", label, result.document, chunks=result.chunks)


async def _approve_publish(ctx: RunContext, p: Payload) -> Outcome:
    await ctx.ops.approve(p["document_id"], ctx.actor)
    result = await ctx.ops.publish(p["document_id"], ctx.actor)
    outcome = _doc_outcome("approve_publish", "Approved and published", result.document)
    return outcome | {"chunks": result.chunks}


async def _unpublish(ctx: RunContext, p: Payload) -> Outcome:
    doc = await ctx.ops.unpublish(p["document_id"], ctx.actor)
    return _doc_outcome("unpublish", "Unpublished", doc)


async def _relabel(ctx: RunContext, p: Payload) -> Outcome:
    patch = DocumentPatch.model_validate(RELABEL.model_input(p))
    doc = await ctx.ops.update_document(p["document_id"], patch, ctx.actor)
    return _doc_outcome("relabel", "Updated", doc)


async def _delete_document(ctx: RunContext, p: Payload) -> Outcome:
    await ctx.ops.delete_document(p["document_id"], ctx.actor)
    return {
        "action": "delete_document",
        "message": f"Deleted “{p.get('document_title')}” ({p['document_id']})",
        "document_id": p["document_id"],
    }


def _memory(ctx: RunContext) -> MemoryService:
    if ctx.memory is None:
        raise AppError(ErrorCode.DEPENDENCY_UNAVAILABLE, "Owner memory is not running")
    return ctx.memory


def _memory_line(item: MemoryOut) -> str:
    return f"  {item.kind}: {item.text} ({item.id})"


async def _remember(ctx: RunContext, p: Payload) -> Outcome:
    body = MemoryIn(text=str(p["text"]), kind=p.get("kind") or "preference")
    item, created = await _memory(ctx).add(body, ctx.actor)
    verb = "Remembered" if created else "Already remembered; marked active"
    return {
        "action": "remember",
        "message": f"{verb}: {item.text} ({item.kind}, {item.id})",
        "memory": {"id": item.id, "kind": item.kind, "status": item.status},
    }


async def _forget(ctx: RunContext, p: Payload) -> Outcome:
    item = await _memory(ctx).archive(str(p["memory_id"]), ctx.actor)
    return {
        "action": "forget",
        "message": f"Forgotten (archived): {item.text} ({item.id})",
        "memory": {"id": item.id, "kind": item.kind, "status": item.status},
    }


async def _show_memory(ctx: RunContext, p: Payload) -> Outcome:
    items = await _memory(ctx).listing()
    active = [m for m in items if m.status == "active"]
    pending = [m for m in items if m.status == "pending"]
    if not items:
        return {
            "action": "show_memory",
            "message": "I don't remember anything yet.",
            "memories": [],
        }
    lines = [f"Active ({len(active)}):", *(_memory_line(m) for m in active)] if active else []
    if pending:
        lines += [f"Waiting for your review ({len(pending)}):", *map(_memory_line, pending)]
        lines.append('Accept one with: make memory ARGS="accept <id>"')
    return {
        "action": "show_memory",
        "message": "\n".join(lines),
        "memories": [{"id": m.id, "kind": m.kind, "status": m.status} for m in items],
    }


# --- the catalog -------------------------------------------------------------------------------

_CATEGORY_HINT = "one of: " + ", ".join(CATEGORIES)
_META = {
    "category": _CATEGORY_HINT,
    "tags": "short labels, comma-separated",
    "occurred_at": "when it happened, in the owner's words (e.g. yesterday, 12 Oct)",
    "tier": "only if the owner asks: public, private, confidential or restricted",
    "summary": "optional one-line summary",
}
_DOC = {"document": "the document's title or id, or 'it' for the one just discussed"}
_DOC_RESOLVED = ("document_id", "document_title", "document_tier")
_RECORD_RESOLVED = ("record_id", "record_label", "record_company")
PROFILE_FIELDS = ("availability", "notice_period", "preferred_roles", "locations")

UPDATE_RECORD = ActionSpec(
    name="update_record",
    label="Update a record",
    description="change an existing job-search record",
    fields={
        "record": "the company (or record id) of the record to change",
        "company": "new company name",
        "role": "new role",
        "stage": "new stage",
        "date": "new date, in the owner's words",
        "notes": "new notes",
    },
    required=("record",),
    needs_any=("company", "role", "stage", "date", "notes"),
    model=RecordChanges,
    model_keys=("company", "role", "stage", "date", "notes"),
    target="record",
    date_fields=("date",),
    resolved=_RECORD_RESOLVED,
    run=_update_record,
)

RELABEL = ActionSpec(
    name="relabel",
    label="Change a document's tier or labels",
    description="change a document's tier, category, tags or title",
    fields={
        **_DOC,
        "tier": "public, private, confidential or restricted",
        "category": _CATEGORY_HINT,
        "tags": "new labels",
        "title": "new title",
    },
    required=("document",),
    needs_any=("tier", "category", "tags", "title"),
    model=DocumentPatch,
    model_keys=("tier", "category", "tags", "title"),
    inferred=("category",),
    target="document",
    list_fields=("tags",),
    lower_fields=("tier",),
    resolved=_DOC_RESOLVED,
    run=_relabel,
)


def _doc_action(name: str, label: str, description: str, run: Runner, **kw: Any) -> ActionSpec:
    return ActionSpec(
        name=name,
        label=label,
        description=description,
        fields=_DOC,
        required=("document",),
        target="document",
        resolved=_DOC_RESOLVED,
        run=run,
        **kw,
    )


_SPECS = [
    ActionSpec(
        name="add_note",
        label="Add a note",
        description="save a typed note: an interview experience, an issue solved, a feature "
        "built, a learning",
        fields={"title": "a short title", "body": "the note text, in the owner's words", **_META},
        required=("title", "body", "category"),
        model=TextIn,
        inferred=("title", "category"),
        date_fields=("occurred_at",),
        list_fields=("tags",),
        lower_fields=("category", "tier"),
        run=_add_note,
    ),
    ActionSpec(
        name="ingest_url",
        label="Ingest web pages",
        description="fetch public web pages (http/https links) into the vault",
        fields={"urls": "the links", **_META},
        required=("urls", "category"),
        model=UrlsIn,
        inferred=("category",),
        date_fields=("occurred_at",),
        list_fields=("urls", "tags"),
        lower_fields=("category", "tier"),
        run=_ingest_url,
    ),
    ActionSpec(
        name="ingest_file",
        label="Ingest a file from the inbox",
        description="ingest a file the owner dropped in the inbox folder, by its file name",
        fields={"filename": "the file name only, e.g. resume.pdf", **_META},
        required=("filename", "category"),
        model=InboxFileIn,
        inferred=("category",),
        date_fields=("occurred_at",),
        list_fields=("tags",),
        lower_fields=("category", "tier"),
        run=_ingest_file,
    ),
    ActionSpec(
        name="ingest_github",
        label="Sync GitHub repositories",
        description="sync allowlisted GitHub repositories (owner/name)",
        fields={"repos": "owner/name, comma-separated", "tags": "short labels"},
        required=("repos",),
        model=GitHubIn,
        list_fields=("repos", "tags"),
        run=_ingest_github,
    ),
    ActionSpec(
        name="add_record",
        label="Add a record",
        description=(
            "track a job-search step: an application, an assessment (take-home assignment or "
            "online test), an interview, an offer, a rejection or a withdrawal "
            "(company, role, stage, date)"
        ),
        fields={
            "kind": "application, assessment, interview, offer, rejection or withdrawal",
            "company": "the company",
            "role": "the role applied or interviewed for",
            "stage": (
                "e.g. applied, take-home sent, screen, round 2, onsite, verbal offer, after HR"
            ),
            "date": "when, in the owner's words (e.g. yesterday, last Friday, 12 Oct)",
            "notes": "anything else the owner said about it",
        },
        required=("kind", "company", "role", "stage", "date"),
        model=RecordIn,
        inferred=("kind",),
        date_fields=("date",),
        lower_fields=("kind",),
        run=_add_record,
    ),
    UPDATE_RECORD,
    ActionSpec(
        name="delete_record",
        label="Delete a record",
        description="delete a job-search record",
        fields={"record": "the company (or record id) of the record to delete"},
        required=("record",),
        destructive=True,
        target="record",
        resolved=_RECORD_RESOLVED,
        run=_delete_record,
    ),
    ActionSpec(
        name="update_profile",
        label="Update the profile",
        description="change availability, notice period, preferred roles or locations",
        fields={
            "availability": "e.g. open to offers, not looking",
            "notice_period": "e.g. 30 days",
            "preferred_roles": "comma-separated",
            "locations": "comma-separated",
        },
        needs_any=PROFILE_FIELDS,
        model=ProfileIn,
        model_keys=PROFILE_FIELDS,
        list_fields=("preferred_roles", "locations"),
        run=_update_profile,
    ),
    ActionSpec(
        name="show_review",
        label="Show the review queue",
        description="list documents waiting for review",
        fields={},
        write=False,
        run=_show_review,
    ),
    ActionSpec(
        name="show_leads",
        label="Show recent leads",
        description="list the latest opportunities visitors left (read only)",
        fields={},
        write=False,
        run=_show_leads,
    ),
    ActionSpec(
        name="source_status",
        label="Ingestion status",
        description="the status of an ingestion job (source id)",
        fields={"source_id": "the source id, or empty for the last one"},
        required=("source_id",),
        write=False,
        run=_source_status,
    ),
    _doc_action(
        "approve", "Approve a document", "approve a document at its current tier", _approve
    ),
    _doc_action(
        "publish",
        "Publish a document",
        "publish an approved public document to the portfolio",
        _publish,
    ),
    _doc_action(
        "unpublish", "Unpublish a document", "remove a document from the portfolio", _unpublish
    ),
    RELABEL,
    _doc_action(
        "delete_document",
        "Delete a document",
        "delete a document from the vault",
        _delete_document,
        destructive=True,
    ),
    ActionSpec(
        name="remember",
        label="Remember this",
        description="remember how the owner likes answers, a standing rule, or a name they use "
        '("remember that I prefer ...", "always ...", "when I say X I mean Y"); not career facts',
        fields={
            "text": "what to remember, one short sentence in the owner's words",
            "kind": "instruction (a standing rule), preference (how answers should be) or alias "
            "(a name the owner uses)",
        },
        required=("text", "kind"),
        inferred=("kind",),
        lower_fields=("kind",),
        run=_remember,
    ),
    ActionSpec(
        name="forget",
        label="Forget a memory",
        description="stop following something the assistant remembers",
        fields={"memory": "the memory's words or its id"},
        required=("memory",),
        target="memory",
        resolved=("memory_id", "memory_text"),
        run=_forget,
    ),
    ActionSpec(
        name="show_memory",
        label="Show what I remember",
        description="list the owner's remembered preferences, rules and aliases (read only)",
        fields={},
        write=False,
        run=_show_memory,
    ),
    ActionSpec(
        name="answer",
        label="Answer a question",
        description="answer a question about the vault, records or profile (read only)",
        fields={"question": "the owner's question"},
        write=False,
        inferred=("question",),
    ),
]

# Not offered to the model: only a suggestion creates it.
APPROVE_PUBLISH = _doc_action(
    "approve_publish",
    "Approve and publish",
    "approve then publish a public document",
    _approve_publish,
)

ACTIONS: dict[str, ActionSpec] = {s.name: s for s in [*_SPECS, APPROVE_PUBLISH]}
MODEL_ACTIONS = tuple(s for s in _SPECS)


def catalog_text() -> str:
    """The action list the extraction prompt shows the model."""
    lines = []
    for spec in MODEL_ACTIONS:
        fields = "; ".join(f"{k}: {v}" for k, v in spec.fields.items()) or "no fields"
        lines.append(f"- {spec.name}: {spec.description}. Fields: {fields}")
    return "\n".join(lines)


# --- resolving the target ----------------------------------------------------------------------


@dataclass
class Resolution:
    payload: Payload
    # A question for the owner when the target is unknown or ambiguous.
    problem: str | None = None
    # Something the owner should know that doesn't block the draft (shown above it).
    note: str | None = None


def _norm(text: str) -> str:
    return " ".join(text.casefold().split())


def _pick[T](items: list[T], key: Callable[[T], str], ref: str) -> list[T]:
    wanted = _norm(ref)
    exact = [i for i in items if _norm(key(i)) == wanted]
    inside = [i for i in items if wanted in _norm(key(i))]
    # "the Acme Widgets interview record" names the company inside a longer phrase.
    around = [i for i in items if len(key(i)) >= MIN_NAME and _norm(key(i)) in wanted]
    return exact or inside or around


async def resolve_target(
    spec: ActionSpec,
    payload: Payload,
    ops: AdminOps,
    recent: list[dict[str, Any]],
    inbox: Path,
    memory: MemoryService | None = None,
) -> Resolution:
    if spec.target == "memory":
        return await _resolve_memory(payload, memory)
    if spec.name == "ingest_file" and payload.get("filename"):
        # Checked before the draft is shown, from metadata only; the file is read on "yes".
        try:
            resolve_inbox(inbox, str(payload["filename"]))
        except InboxError as exc:
            rest = {k: v for k, v in payload.items() if k != "filename"}
            return Resolution(rest, str(exc))
    if spec.target == "document":
        resolution = await _resolve_document(payload, ops, recent)
        if spec.name == "publish" and resolution.problem is None:
            return await _check_publishable(resolution, ops)
        return resolution
    if spec.target == "record":
        return await _resolve_record(payload, ops, recent)
    if spec.name == "ingest_github" and payload.get("repos"):
        # Said before the draft, not after "yes": an unlisted repo would only fail later.
        allowed = ops.github_allowlist()
        repos = [str(r).lower() for r in payload["repos"]]
        blocked = [r for r in repos if r not in allowed]
        if blocked:
            kept = [r for r in repos if r in allowed]
            listed = ", ".join(blocked)
            message = (
                f"{listed} {'is' if len(blocked) == 1 else 'are'} not in GITHUB_SYNC_REPOS, so "
                "I can't sync it; add it there in .env and restart the API to include it."
            )
            if kept:
                return Resolution({**payload, "repos": kept}, note=message)
            return Resolution({k: v for k, v in payload.items() if k != "repos"}, message)
    if spec.name == "source_status" and not payload.get("source_id"):
        last = next((r for r in reversed(recent) if r.get("source_id")), None)
        if last is not None:
            return Resolution({**payload, "source_id": last["source_id"]})
    return Resolution(payload)


PUBLISHABLE_AFTER_CHANGE = frozenset({"private"})


async def _check_publishable(resolution: Resolution, ops: AdminOps) -> Resolution:
    """Show the tier a publish would change, or refuse one that would expose sensitive text."""
    doc_id = resolution.payload.get("document_id")
    if not doc_id:
        return resolution
    doc = await ops.get_document(str(doc_id))
    payload = {**resolution.payload, "document_tier": doc.tier}
    if doc.tier == "public" or doc.tier in PUBLISHABLE_AFTER_CHANGE:
        return Resolution(payload)
    return Resolution(
        {k: v for k, v in payload.items() if not k.startswith("document")},
        f"“{doc.title}” is {doc.tier}; I won't publish that from here. If it really is fine "
        "to show anyone, first say: make it public (a relabel you confirm on its own).",
    )


async def _resolve_document(
    payload: Payload, ops: AdminOps, recent: list[dict[str, Any]]
) -> Resolution:
    if payload.get("document_id"):
        return Resolution(payload)
    ref = str(payload.get("document") or "").strip()
    recent_docs = [r for r in recent if r.get("kind") == "document"]
    if not ref or _norm(ref) in PRONOUNS:
        if not recent_docs:
            return Resolution(payload)  # still missing: the owner is asked which document
        last = recent_docs[-1]
        return Resolution({**payload, "document_id": last["id"], "document_title": last["title"]})
    if found_id := _OBJECT_ID.search(ref):
        try:
            doc = await ops.get_document(found_id.group(0))
        except AppError:
            return Resolution(payload, f"No document with id {found_id.group(0)}.")
        return Resolution({**payload, "document_id": doc.id, "document_title": doc.title})
    queue = await ops.review_queue(REVIEW_SCAN)
    candidates: dict[str, str] = {d.id: d.title for d in queue.items}
    candidates |= {r["id"]: r["title"] for r in recent_docs}
    found = _pick(list(candidates.items()), lambda item: item[1], ref)
    if len(found) == 1:
        doc_id, title = found[0]
        return Resolution({**payload, "document_id": doc_id, "document_title": title})
    if not found:
        return Resolution(
            payload,
            f"I can't find a document titled “{ref}” in the review queue or this conversation. "
            "Give its id (see: show the review queue).",
        )
    names = "; ".join(f"“{t}” ({i})" for i, t in found[:8])
    return Resolution(payload, f"Several documents match “{ref}”: {names}. Which one (id)?")


async def _resolve_record(
    payload: Payload, ops: AdminOps, recent: list[dict[str, Any]]
) -> Resolution:
    if payload.get("record_id"):
        return Resolution(payload)
    ref = str(payload.get("record") or "").strip()
    records = await ops.list_records(None)
    if not ref or _norm(ref) in PRONOUNS:
        last = next((r for r in reversed(recent) if r.get("kind") == "record"), None)
        match = [r for r in records if last and r.id == last["id"]]
    elif found_id := _OBJECT_ID.search(ref):
        match = [r for r in records if r.id == found_id.group(0)]
    else:
        match = _pick(records, lambda r: r.company, ref)
    if len(match) == 1:
        record = match[0]
        return Resolution(
            {
                **payload,
                "record_id": record.id,
                "record_label": _record_line(record),
                "record_company": record.company,
            }
        )
    if not ref:
        return Resolution(payload)
    if not match:
        return Resolution(payload, f"I can't find a record for “{ref}”.")
    names = "; ".join(_record_line(r) for r in match[:8])
    return Resolution(payload, f"Several records match “{ref}”: {names}. Which one (id)?")


async def _resolve_memory(payload: Payload, memory: MemoryService | None) -> Resolution:
    if payload.get("memory_id") or memory is None:
        return Resolution(payload)
    ref = str(payload.get("memory") or "").strip()
    if not ref:
        return Resolution(payload)
    items = await memory.listing()
    if found_id := _OBJECT_ID.search(ref):
        match = [m for m in items if m.id == found_id.group(0)]
    else:
        match = _pick(items, lambda m: m.text, ref)
    if len(match) == 1:
        return Resolution({**payload, "memory_id": match[0].id, "memory_text": match[0].text})
    if not match:
        return Resolution(payload, f"I don't remember anything like “{ref}” (see: show memory).")
    names = "; ".join(f"“{m.text}” ({m.id})" for m in match[:8])
    return Resolution(payload, f"Several memories match “{ref}”: {names}. Which one (id)?")


def recent_items(outcome: Outcome) -> list[dict[str, Any]]:
    """What a later "it" can refer to: documents and records this result touched."""
    items: list[dict[str, Any]] = []
    for source in outcome.get("sources", []):
        items.append({"kind": "source", "source_id": source["source_id"]})
        items += [
            {"kind": "document", "id": d["id"], "title": d["title"]} for d in source["documents"]
        ]
    if not outcome.get("sources"):
        items += [
            {"kind": "document", "id": d["id"], "title": d["title"]}
            for d in outcome.get("documents", [])[:1]
        ]
    if record := outcome.get("record"):
        items.append({"kind": "record", "id": record["id"], "company": record["company"]})
    return items


def documents_of(outcome: Outcome) -> list[dict[str, Any]]:
    return _documents_of(outcome)
