"""In-memory stand-ins for the storage protocols, so unit tests need no running stack."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import datetime
from types import SimpleNamespace
from typing import Any

from qdrant_client import models

from app.auth.keys import KeyRecord
from app.db.documents import Scope, utcnow
from app.services.conversations import ConversationRef, StoredConversation, Turn
from app.services.model_overrides import Override, OverrideIn


class MemoryConversationStore:
    def __init__(self) -> None:
        self.conversations: dict[str, dict[str, Any]] = {}
        self.turns: dict[str, dict[int, Turn]] = {}
        self.saves = 0

    async def save_turn(
        self, conversation: ConversationRef, turn: Turn, expires_at: datetime | None
    ) -> None:
        self.saves += 1
        meta = self.conversations.setdefault(
            conversation.id,
            {"channel": conversation.channel, "visitor_hash": conversation.visitor_hash},
        )
        meta["expires_at"] = expires_at
        self.turns.setdefault(conversation.id, {})[turn.seq] = turn

    async def load(self, conversation_id: str, limit: int) -> StoredConversation | None:
        meta = self.conversations.get(conversation_id)
        if meta is None or meta.get("deleted_at") is not None:
            return None
        turns = [t for _, t in sorted(self.turns.get(conversation_id, {}).items())]
        return StoredConversation(channel=meta["channel"], turns=turns[-limit:])

    async def soft_delete(self, conversation_id: str, at: datetime) -> bool:
        meta = self.conversations.get(conversation_id)
        if meta is None or meta.get("deleted_at") is not None:
            return False
        meta["deleted_at"] = at
        return True


class FailingStore(MemoryConversationStore):
    """Fails the first `failures` saves (every save when None)."""

    def __init__(self, failures: int | None = None) -> None:
        super().__init__()
        self.failures = failures
        self.attempts = 0

    async def save_turn(
        self, conversation: ConversationRef, turn: Turn, expires_at: datetime | None
    ) -> None:
        self.attempts += 1
        if self.failures is None or self.attempts <= self.failures:
            raise ConnectionError("mongo down")
        await super().save_turn(conversation, turn, expires_at)

    async def load(self, conversation_id: str, limit: int) -> StoredConversation | None:
        if self.failures is None:
            raise ConnectionError("mongo down")
        return await super().load(conversation_id, limit)


class BlockedStore(MemoryConversationStore):
    """Holds every save until `release` is set: proves callers do not wait for MongoDB."""

    def __init__(self) -> None:
        super().__init__()
        self.release = asyncio.Event()

    async def save_turn(
        self, conversation: ConversationRef, turn: Turn, expires_at: datetime | None
    ) -> None:
        await self.release.wait()
        await super().save_turn(conversation, turn, expires_at)


class MemoryApiKeyStore:
    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.touches: list[datetime] = []

    async def add(self, name: str, key_hash: str, scopes: Sequence[Scope]) -> KeyRecord:
        key_id = f"k{len(self.rows) + 1}"
        self.rows[key_id] = {
            "name": name,
            "key_hash": key_hash,
            "scopes": list(scopes),
            "created_at": utcnow(),
            "revoked_at": None,
            "last_used_at": None,
        }
        return self._record(key_id)

    def _record(self, key_id: str) -> KeyRecord:
        row = {k: v for k, v in self.rows[key_id].items() if k != "key_hash"}
        return KeyRecord(id=key_id, **row)

    async def find_by_hash(self, key_hash: str) -> KeyRecord | None:
        for key_id, row in self.rows.items():
            if row["key_hash"] == key_hash:
                return self._record(key_id)
        return None

    async def touch(self, key_id: str, at: datetime) -> None:
        self.rows[key_id]["last_used_at"] = at
        self.touches.append(at)

    async def list(self) -> list[KeyRecord]:
        return [self._record(k) for k in self.rows]

    async def revoke(self, name: str, at: datetime) -> int:
        hits = [r for r in self.rows.values() if r["name"] == name and r["revoked_at"] is None]
        for row in hits:
            row["revoked_at"] = at
        return len(hits)


class MemoryOverrideStore:
    def __init__(self) -> None:
        self.rows: dict[str, Override] = {}

    async def list(self) -> list[Override]:
        return [self.rows[k] for k in sorted(self.rows)]

    async def get(self, override_id: str) -> Override | None:
        return self.rows.get(override_id)

    async def put(self, override_id: str, body: OverrideIn) -> Override:
        self.rows[override_id] = Override(id=override_id, updated_at=utcnow(), **body.model_dump())
        return self.rows[override_id]


class MemoryAuditLog:
    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []

    async def record(self, actor: str, action: str, target: str, details: dict[str, Any]) -> None:
        self.entries.append({"actor": actor, "action": action, "target": target, **details})


class FakeQdrant:
    """Enough of AsyncQdrantClient for `ensure_collections`, recording every write."""

    def __init__(self) -> None:
        self.collections: dict[str, dict[str, Any]] = {}
        self.writes: list[str] = []

    async def collection_exists(self, name: str) -> bool:
        return name in self.collections

    async def create_collection(
        self,
        collection_name: str,
        vectors_config: dict[str, models.VectorParams],
        sparse_vectors_config: dict[str, models.SparseVectorParams],
    ) -> bool:
        self.collections[collection_name] = {
            "vectors": vectors_config,
            "sparse": sparse_vectors_config,
            "payload": {},
        }
        self.writes.append(f"create {collection_name}")
        return True

    async def get_collection(self, name: str) -> Any:
        data = self.collections[name]
        params = SimpleNamespace(vectors=data["vectors"], sparse_vectors=data["sparse"])
        return SimpleNamespace(
            config=SimpleNamespace(params=params), payload_schema=dict(data["payload"])
        )

    async def create_payload_index(
        self, name: str, field_name: str, field_schema: models.PayloadSchemaType, wait: bool
    ) -> None:
        self.collections[name]["payload"][field_name] = field_schema
        self.writes.append(f"index {name}.{field_name}")
