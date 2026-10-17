from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from app.auth.keys import hash_key, issue_key
from app.cache.redis import conversation_key, conversation_seq_key
from app.core.config import Settings
from app.core.resources import Resources, open_resources
from app.db.documents import AuditEntry, Conversation, Message, utcnow
from app.db.migrations import discover
from app.services.conversations import MongoConversationStore, Turn
from app.services.model_overrides import OverrideIn
from app.vector.qdrant import COLLECTIONS, ensure_collections

pytestmark = pytest.mark.integration


async def _indexes(res: Resources, settings: Settings) -> dict[str, Any]:
    db = res.mongo[settings.mongodb_database]
    return {name: await db[name].index_information() for name in await db.list_collection_names()}


async def test_second_start_changes_nothing(resources: Resources, settings: Settings) -> None:
    first = await _indexes(resources, settings)
    assert {"api_keys", "messages", "conversations", "documents"} <= set(first)
    assert "expires_at_ttl" in first["messages"]
    assert first["documents"]["title_text"]["key"] == [("_fts", "text"), ("_ftsx", 1)]

    async with open_resources(settings) as again:
        assert await _indexes(again, settings) == first
        assert await again.mongo[settings.mongodb_database]["schema_migrations"].count_documents(
            {}
        ) == len(discover())
        assert await ensure_collections(again.qdrant, "vault", settings.embedding_dim) == []
    for name in COLLECTIONS["vault"]:
        assert await resources.qdrant.collection_exists(name)


async def test_history_after_redis_flush_comes_from_mongo(resources: Resources) -> None:
    svc = resources.conversations
    conv = await svc.start("public", "198.51.100.23")
    for i in range(3):
        await svc.append_turn(conv, Turn(role="user", content=f"question {i}"))
    await svc.drain()
    cached = await svc.history(conv.id)

    await resources.redis.delete(conversation_key(conv.id), conversation_seq_key(conv.id))
    reloaded = await svc.history(conv.id)
    assert [t.model_dump() for t in reloaded] == [t.model_dump() for t in cached]

    stored = await Conversation.get(conv.id)
    assert stored is not None and stored.message_count == 3
    assert stored.expires_at is not None
    assert stored.expires_at - utcnow() == pytest.approx(
        timedelta(days=90), abs=timedelta(minutes=1)
    )
    assert stored.visitor_hash and "198.51.100.23" not in stored.model_dump_json()
    messages = await Message.find(Message.conversation_id == conv.id).to_list()
    assert len(messages) == 3 and all(m.expires_at for m in messages)


async def test_owner_conversation_has_no_expiry_and_saves_are_idempotent(
    resources: Resources,
) -> None:
    svc = resources.conversations
    conv = await svc.start("owner")
    turn = await svc.append_turn(conv, Turn(role="user", content="note to self"))
    await svc.drain()
    await MongoConversationStore().save_turn(conv, turn, None)  # a retry that already landed
    stored = await Conversation.get(conv.id)
    assert stored is not None and stored.expires_at is None
    assert await Message.find(Message.conversation_id == conv.id).count() == 1


async def test_api_key_lifecycle(resources: Resources) -> None:
    store = resources.api_keys
    record, plaintext = await issue_key(store, "it-key", ["admin"])
    found = await store.find_by_hash(hash_key(plaintext))
    assert found is not None and found.id == record.id
    raw = await resources.mongo["ai_twin_it"]["api_keys"].find_one({})
    assert raw is not None and plaintext not in str(raw)
    now = utcnow()
    await store.touch(record.id, now)
    assert await store.revoke("it-key", now) == 1
    listed = await store.list()
    assert listed[0].last_used_at is not None and not listed[0].active


async def test_override_store_and_audit(resources: Resources) -> None:
    await resources.overrides.put("fake:7b", OverrideIn(enabled=False, max_tier="private"))
    stored = await resources.overrides.put("fake:7b", OverrideIn(max_tier="public"))
    assert stored.enabled is None  # PUT replaces
    assert [o.id for o in await resources.overrides.list()] == ["fake:7b"]
    await resources.audit.record("key:it", "model_override.put", "fake:7b", {"after": {}})
    assert await AuditEntry.find(AuditEntry.target == "fake:7b").count() == 1
