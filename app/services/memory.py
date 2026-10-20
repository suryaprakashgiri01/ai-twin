"""Owner memories: a small, editable set of preferences, standing instructions and aliases.

- **Learned** items come from the digester's candidates. Code keeps a candidate only when it is
  a known kind, short enough and supported by the owner's own messages; identifiers are masked;
  a duplicate (same normalised text, or embedding similarity ≥ MEMORY_SIMILARITY) bumps the
  existing item instead of adding one. New items are `pending`: nothing learned changes how the
  agents behave until the owner accepts it (`MEMORY_AUTO_ACCEPT=true` activates confidence ≥ 0.8).
- **Explicit** items (`/remember`, intake `remember`, `make memory add`, POST) are `active` at
  once: the owner said it.
- Every change is audited with ids, kinds and statuses only. No memory text is logged.
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from datetime import datetime
from typing import Any, Protocol, cast

import structlog
from pydantic import ConfigDict, Field
from pymongo.errors import DuplicateKeyError

from app.api.envelope import ApiModel
from app.core.config import Settings
from app.db.documents import (
    MEMORY_KINDS,
    MemoryKind,
    MemoryStatus,
    MemoryTier,
    OwnerMemory,
    OwnerMemoryFields,
    utcnow,
)
from app.exceptions import AppError, ErrorCode
from app.kb.embedding import Embedder
from app.observability.masking import mask_identifiers
from app.services.audit import AuditLog
from app.services.digest import MemoryCandidate
from llm_orchestrator import Tier

logger = structlog.get_logger(__name__)

TEXT_MAX = 200
AUTO_ACCEPT_CONFIDENCE = 0.8
# Share of a candidate's words that must appear in the owner's messages.
SUPPORT_SHARE = 0.6
STEM = 5
LEARN_ACTOR = "agent:digest"
_WORD = re.compile(r"[a-z0-9]+")


def normalise(text: str) -> str:
    """Case, punctuation and spacing folded: what duplicates are matched on."""
    return " ".join(_WORD.findall(text.casefold()))


# A sentence that asks for something lasting; only such sentences can support a memory.
_LASTING = re.compile(
    r"\b(always|never|from now on|going forward|in future|every time|each time|i prefer|"
    r"i'd prefer|i like (it|my|answers|replies|when)|i want (you|answers|replies)|"
    r"please (answer|reply|use|keep|show|format|write|call|give|include|cite|avoid|don't)|"
    r"when i say|i mean|stands for|call it|don't|do not)\b",
    re.I,
)
_SENTENCE = re.compile(r"(?<=[.!?\n])\s+")


def lasting_text(owner_text: str) -> str:
    """The owner's sentences that ask for something lasting; a passing request is not a
    preference ("publish the note" is not "always publish notes")."""
    return "\n".join(s for s in _SENTENCE.split(owner_text) if _LASTING.search(s))


def supported(text: str, owner_text: str) -> bool:
    """True when most words of `text` are the owner's own (a shared stem counts), so a
    candidate cannot come from the assistant's replies or the model's imagination."""
    want = _WORD.findall(text.casefold())
    have = set(_WORD.findall(owner_text.casefold()))
    if not want:
        return False

    def seen(token: str) -> bool:
        return token in have or (len(token) >= STEM and any(h[:STEM] == token[:STEM] for h in have))

    return sum(1 for t in want if seen(t)) / len(want) >= SUPPORT_SHARE


_ALIAS_FORM = re.compile(r"=|\bmeans\b|\bstands for\b|\brefers to\b|\bis short for\b", re.I)
# Words a lasting rule or preference uses; a candidate without any is usually a fact.
_CUES = frozenset(
    (
        "always", "never", "prefer", "prefers", "preferred", "like", "likes", "want", "wants",
        "use", "show", "answer", "answers", "reply", "replies", "keep", "avoid", "don't", "dont",
        "do", "format", "write", "call", "give", "include", "list", "cite", "bullet", "bullets",
        "concise", "short", "shorter", "brief", "detailed", "tables", "table", "spelling",
        "dates", "date", "sources", "tone", "language", "english", "first", "last", "plain",
        "simple", "examples", "code", "steps", "summary", "summaries",
    )
)  # fmt: skip
# A candidate that narrates what happened ("learned about raft") is a fact, not a preference.
_NARRATION = frozenset(
    (
        "learned", "asked", "discussed", "had", "applied", "interviewed", "did", "was", "were",
        "went", "met", "worked", "talked", "they", "user", "i'm", "the owner", "i am",
    )
)  # fmt: skip


_PRONOUNS = frozenset({"it", "this", "that", "they", "these", "those", "he", "she", "them"})


def preference_shaped(kind: str, text: str) -> bool:
    """An alias reads "short = long"; a rule or preference carries a directive word and does not
    narrate an event."""
    if kind == "alias":
        match = _ALIAS_FORM.search(text)
        # "it = the Lumenrail note" names something only this conversation knew.
        return match is not None and text[: match.start()].strip().casefold() not in _PRONOUNS
    words = text.casefold().replace(",", " ").split()
    if not words or words[0] in _NARRATION or " ".join(words[:2]) in _NARRATION:
        return False
    return any(w.strip(".!") in _CUES for w in words)


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0


class MemoryOut(ApiModel):
    id: str
    kind: MemoryKind
    text: str
    status: MemoryStatus
    tier: MemoryTier
    confidence: float
    source_conversation_id: str | None = None
    uses: int
    created_at: datetime
    updated_at: datetime


class MemoryIn(ApiModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=TEXT_MAX)
    kind: MemoryKind = "preference"
    # Confidential memories reach only local-model calls.
    tier: MemoryTier = "private"


class MemoryPatch(ApiModel):
    model_config = ConfigDict(extra="forbid")

    text: str | None = Field(default=None, min_length=1, max_length=TEXT_MAX)
    kind: MemoryKind | None = None
    tier: MemoryTier | None = None
    # accept: active · forget: archived
    status: MemoryStatus | None = None


class MemoryStore(Protocol):
    async def by_status(self, statuses: Sequence[MemoryStatus]) -> list[MemoryOut]: ...

    async def get(self, memory_id: str) -> MemoryOut | None: ...

    async def find_live(self, norm: str) -> MemoryOut | None: ...

    async def insert(self, fields: OwnerMemoryFields) -> MemoryOut:
        """Raises DuplicateKeyError when a live item has the same `norm`."""
        ...

    async def update(self, memory_id: str, changes: dict[str, Any]) -> MemoryOut | None: ...

    async def bump(self, memory_id: str, activate: bool) -> MemoryOut | None: ...

    async def delete(self, memory_id: str) -> bool: ...


def _out(doc: OwnerMemory) -> MemoryOut:
    return MemoryOut(id=doc.uid, **doc.model_dump(exclude={"id", "uid", "revision_id", "norm"}))


LIVE: tuple[MemoryStatus, ...] = ("pending", "active")


class MongoMemoryStore:
    async def by_status(self, statuses: Sequence[MemoryStatus]) -> list[MemoryOut]:
        found = (
            await OwnerMemory.find({"status": {"$in": list(statuses)}})
            .sort("-updated_at")
            .to_list()
        )
        return [_out(d) for d in found]

    async def get(self, memory_id: str) -> MemoryOut | None:
        found = await OwnerMemory.find_one({"uid": memory_id})
        return _out(found) if found is not None else None

    async def find_live(self, norm: str) -> MemoryOut | None:
        found = await OwnerMemory.find_one({"norm": norm, "status": {"$in": list(LIVE)}})
        return _out(found) if found is not None else None

    async def insert(self, fields: OwnerMemoryFields) -> MemoryOut:
        doc = OwnerMemory(**fields.model_dump())
        await doc.insert()
        return _out(doc)

    async def update(self, memory_id: str, changes: dict[str, Any]) -> MemoryOut | None:
        await OwnerMemory.get_pymongo_collection().update_one(
            {"uid": memory_id}, {"$set": {**changes, "updated_at": utcnow()}}
        )
        return await self.get(memory_id)

    async def bump(self, memory_id: str, activate: bool) -> MemoryOut | None:
        changes: dict[str, Any] = {"updated_at": utcnow()}
        if activate:
            changes["status"] = "active"
        await OwnerMemory.get_pymongo_collection().update_one(
            {"uid": memory_id}, {"$set": changes, "$inc": {"uses": 1}}
        )
        return await self.get(memory_id)

    async def delete(self, memory_id: str) -> bool:
        result = await OwnerMemory.get_pymongo_collection().delete_one({"uid": memory_id})
        return result.deleted_count == 1


class MemoryService:
    def __init__(
        self,
        store: MemoryStore,
        audit: AuditLog,
        *,
        embedder: Embedder | None = None,
        auto_accept: bool = False,
        similarity: float = 0.90,
    ) -> None:
        self._store = store
        self._audit = audit
        self._embedder = embedder
        self._auto_accept = auto_accept
        self._similarity = similarity

    # --- reading -------------------------------------------------------------------------------

    async def listing(self, status: MemoryStatus | None = None) -> list[MemoryOut]:
        """One status, or every live (pending and active) item when None."""
        return await self._store.by_status([status] if status else list(LIVE))

    async def active(self) -> list[MemoryOut]:
        return await self._store.by_status(["active"])

    async def get(self, memory_id: str) -> MemoryOut:
        found = await self._store.get(memory_id)
        if found is None:
            raise AppError(ErrorCode.NOT_FOUND, "Memory not found")
        return found

    async def pending_count(self, conversation_id: str) -> int:
        pending = await self._store.by_status(["pending"])
        return sum(1 for m in pending if m.source_conversation_id == conversation_id)

    # --- learning ------------------------------------------------------------------------------

    async def learn(
        self,
        candidates: Sequence[MemoryCandidate],
        *,
        owner_text: str,
        conversation_id: str,
        tier: Tier,
    ) -> int:
        """Store what the owner's own messages support; returns how many new items are pending."""
        owner_text = lasting_text(owner_text)
        if not owner_text:
            logger.info(
                "memories learned", conversation_id=conversation_id, dropped=len(candidates)
            )
            return 0
        live = await self._store.by_status(list(LIVE))
        vectors = await self._vectors([m.text for m in live])
        label: MemoryTier = "confidential" if tier >= Tier.CONFIDENTIAL else "private"
        counts = {"pending": 0, "active": 0, "bumped": 0, "dropped": 0}
        for candidate in candidates:
            kind, text = self._keep(candidate, owner_text)
            if kind is None:
                counts["dropped"] += 1
                continue
            embedded = await self._vectors([text]) if vectors is not None else None
            vector = embedded[0] if embedded else None
            twin = await self._twin(text, vector, live, vectors)
            if twin is not None:
                await self._bump(twin, activate=False, actor=LEARN_ACTOR)
                counts["bumped"] += 1
                continue
            status: MemoryStatus = (
                "active"
                if self._auto_accept and candidate.confidence >= AUTO_ACCEPT_CONFIDENCE
                else "pending"
            )
            stored = await self._insert(
                kind, text, status, label, candidate.confidence, conversation_id, LEARN_ACTOR
            )
            if stored is None:
                counts["bumped"] += 1
                continue
            counts[status] += 1
            live.append(stored)
            if vectors is not None and vector is not None:
                vectors.append(vector)
        logger.info("memories learned", conversation_id=conversation_id, **counts)
        return counts["pending"]

    @staticmethod
    def _keep(candidate: MemoryCandidate, owner_text: str) -> tuple[MemoryKind | None, str]:
        kind = candidate.kind.strip().lower()
        text = " ".join(candidate.text.split())
        if kind not in MEMORY_KINDS or not text or len(text) > TEXT_MAX:
            return None, ""
        if not supported(text, owner_text) or not preference_shaped(kind, text):
            return None, ""
        return cast(MemoryKind, kind), mask_identifiers(text)

    async def _vectors(self, texts: list[str]) -> list[list[float]] | None:
        """Dense embeddings, or None when the local model is unavailable (exact match only)."""
        if self._embedder is None:
            return None
        if not texts:
            return []
        try:
            return [e.dense for e in await self._embedder.embed(texts)]
        except Exception as exc:
            logger.warning("memory embedding failed", error_type=type(exc).__name__)
            return None

    async def _twin(
        self,
        text: str,
        vector: list[float] | None,
        live: list[MemoryOut],
        vectors: list[list[float]] | None,
    ) -> MemoryOut | None:
        norm = normalise(text)
        same = next((m for m in live if normalise(m.text) == norm), None)
        if same is not None:
            return same
        if vector is None or vectors is None:
            return await self._store.find_live(norm)
        best = max(
            ((m, _cosine(vector, v)) for m, v in zip(live, vectors, strict=True)),
            key=lambda pair: pair[1],
            default=None,
        )
        return best[0] if best is not None and best[1] >= self._similarity else None

    # --- explicit changes ----------------------------------------------------------------------

    async def add(
        self, body: MemoryIn, actor: str, source_conversation_id: str | None = None
    ) -> tuple[MemoryOut, bool]:
        """Active at once. Returns the item and whether it is new (a duplicate is bumped and
        activated instead)."""
        text = mask_identifiers(" ".join(body.text.split()))
        if len(text) > TEXT_MAX:
            raise AppError(ErrorCode.INVALID_INPUT, f"A memory is at most {TEXT_MAX} characters")
        existing = await self._store.find_live(normalise(text))
        if existing is not None:
            return await self._bump(existing, activate=True, actor=actor), False
        stored = await self._insert(
            body.kind, text, "active", body.tier, 1.0, source_conversation_id, actor
        )
        if stored is None:  # a concurrent add won the race
            raced = await self._store.find_live(normalise(text))
            if raced is None:
                raise AppError(ErrorCode.CONFLICT, "That memory changed meanwhile; try again")
            return raced, False
        return stored, True

    async def update(self, memory_id: str, patch: MemoryPatch, actor: str) -> MemoryOut:
        current = await self.get(memory_id)
        changes: dict[str, Any] = patch.model_dump(exclude_none=True)
        if "text" in changes:
            changes["text"] = mask_identifiers(" ".join(changes["text"].split()))
            changes["norm"] = normalise(changes["text"])
        reviving = changes.get("status") in LIVE and current.status == "archived"
        if "norm" in changes or reviving:
            clash = await self._store.find_live(changes.get("norm") or normalise(current.text))
            if clash is not None and clash.id != memory_id:
                raise AppError(ErrorCode.CONFLICT, "The same memory exists already")
        try:
            updated = await self._store.update(memory_id, changes)
        except DuplicateKeyError as exc:
            raise AppError(ErrorCode.CONFLICT, "The same memory exists already") from exc
        if updated is None:
            raise AppError(ErrorCode.NOT_FOUND, "Memory not found")
        await self._audit.record(
            actor=actor,
            action=_action(patch),
            target=memory_id,
            details={"fields": sorted(changes.keys() - {"norm"}), "status": updated.status},
        )
        return updated

    async def accept(self, memory_id: str, actor: str) -> MemoryOut:
        return await self.update(memory_id, MemoryPatch(status="active"), actor)

    async def archive(self, memory_id: str, actor: str) -> MemoryOut:
        return await self.update(memory_id, MemoryPatch(status="archived"), actor)

    async def delete(self, memory_id: str, actor: str) -> None:
        found = await self.get(memory_id)
        if not await self._store.delete(memory_id):
            raise AppError(ErrorCode.NOT_FOUND, "Memory not found")
        await self._audit.record(
            actor=actor,
            action="memory.delete",
            target=memory_id,
            details={"kind": found.kind, "status": found.status},
        )

    # --- writes ----------------------------------------------------------------------------------

    async def _insert(
        self,
        kind: MemoryKind,
        text: str,
        status: MemoryStatus,
        tier: MemoryTier,
        confidence: float,
        source: str | None,
        actor: str,
    ) -> MemoryOut | None:
        fields = OwnerMemoryFields(
            kind=kind,
            text=text,
            status=status,
            tier=tier,
            confidence=confidence,
            source_conversation_id=source,
            norm=normalise(text),
        )
        try:
            stored = await self._store.insert(fields)
        except DuplicateKeyError:
            existing = await self._store.find_live(fields.norm)
            if existing is not None:
                await self._bump(existing, activate=status == "active", actor=actor)
            return None
        await self._audit.record(
            actor=actor,
            action="memory.learn" if actor == LEARN_ACTOR else "memory.add",
            target=stored.id,
            details={"kind": kind, "status": status, "tier": tier, "conversation": source},
        )
        return stored

    async def _bump(self, item: MemoryOut, *, activate: bool, actor: str) -> MemoryOut:
        bumped = await self._store.bump(item.id, activate=activate)
        await self._audit.record(
            actor=actor,
            action="memory.bump",
            target=item.id,
            details={"status": (bumped or item).status},
        )
        return bumped or item


def _action(patch: MemoryPatch) -> str:
    if patch.status == "active" and patch.text is None:
        return "memory.accept"
    if patch.status == "archived":
        return "memory.archive"
    return "memory.edit"


def build_memory(settings: Settings, audit: AuditLog, embedder: Embedder | None) -> MemoryService:
    return MemoryService(
        MongoMemoryStore(),
        audit,
        embedder=embedder,
        auto_accept=settings.memory_auto_accept,
        similarity=settings.memory_similarity,
    )
