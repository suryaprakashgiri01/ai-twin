from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from fakeredis import FakeAsyncRedis

from app.cache.redis import conversation_key, conversation_seq_key
from app.exceptions import AppError, ErrorCode
from app.services.conversations import CACHE_TTL, ConversationService, Turn, hash_visitor
from tests.fakes import BlockedStore, FailingStore, MemoryConversationStore

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _service(
    store: MemoryConversationStore,
    redis: FakeAsyncRedis,
    *,
    cache_turns: int = 5,
    retry_attempts: int = 4,
) -> ConversationService:
    return ConversationService(
        redis,
        store,
        visitor_salt="test-salt",
        cache_turns=cache_turns,
        retry_attempts=retry_attempts,
        retry_base_delay=0,
        clock=lambda: NOW,
    )


@pytest.fixture
def redis() -> FakeAsyncRedis:
    return FakeAsyncRedis(decode_responses=True)


async def test_append_returns_before_mongo_and_history_reads_redis(redis: FakeAsyncRedis) -> None:
    store = BlockedStore()
    svc = _service(store, redis)
    conv = await svc.start("owner")
    first = await svc.append_turn(conv, Turn(role="user", content="hello"))
    second = await svc.append_turn(conv, Turn(role="assistant", content="hi"))
    assert (first.seq, second.seq) == (1, 2)
    assert store.saves == 0  # nothing persisted yet, the caller already has its answer
    assert [t.content for t in await svc.history(conv.id)] == ["hello", "hi"]
    store.release.set()
    await svc.drain()
    assert sorted(store.turns[conv.id]) == [1, 2]


async def test_failed_persist_is_retried_counted_and_never_raised(redis: FakeAsyncRedis) -> None:
    store = FailingStore(failures=None)
    svc = _service(store, redis, retry_attempts=3)
    conv = await svc.start("public", "203.0.113.9")
    turn = await svc.append_turn(conv, Turn(role="user", content="q"))
    await svc.drain()
    assert store.attempts == 3
    assert svc.persist_failures == 1
    assert [t.seq for t in await svc.history(conv.id)] == [turn.seq]  # still served from Redis


async def test_transient_failure_recovers_on_retry(redis: FakeAsyncRedis) -> None:
    store = FailingStore(failures=2)
    svc = _service(store, redis, retry_attempts=3)
    conv = await svc.start("owner")
    await svc.append_turn(conv, Turn(role="user", content="q"))
    await svc.drain()
    assert svc.persist_failures == 0
    assert list(store.turns[conv.id]) == [1]


async def test_history_after_flush_reloads_from_mongo_and_numbering_continues(
    redis: FakeAsyncRedis,
) -> None:
    store = MemoryConversationStore()
    svc = _service(store, redis)
    conv = await svc.start("public")
    for i in range(3):
        await svc.append_turn(conv, Turn(role="user", content=f"m{i}"))
    await svc.drain()
    before = await svc.history(conv.id)

    await redis.flushall()
    after = await svc.history(conv.id)
    assert [t.model_dump() for t in after] == [t.model_dump() for t in before]
    assert await redis.ttl(conversation_key(conv.id)) > 0  # repopulated with a TTL

    await redis.flushall()
    nxt = await svc.append_turn(conv, Turn(role="assistant", content="later"))
    assert nxt.seq == 4


async def test_cache_is_capped_and_ttl_follows_channel(redis: FakeAsyncRedis) -> None:
    svc = _service(MemoryConversationStore(), redis, cache_turns=3)
    owner = await svc.start("owner")
    public = await svc.start("public")
    for i in range(5):
        await svc.append_turn(owner, Turn(role="user", content=str(i)))
    await svc.append_turn(public, Turn(role="user", content="p"))
    await svc.drain()
    assert [t.content for t in await svc.history(owner.id)] == ["2", "3", "4"]
    owner_ttl = await redis.ttl(conversation_seq_key(owner.id))
    public_ttl = await redis.ttl(conversation_key(public.id))
    assert owner_ttl == pytest.approx(CACHE_TTL["owner"].total_seconds(), abs=5)
    assert public_ttl == pytest.approx(CACHE_TTL["public"].total_seconds(), abs=5)


async def test_public_expires_after_retention_owner_never(redis: FakeAsyncRedis) -> None:
    store = MemoryConversationStore()
    svc = _service(store, redis)
    public = await svc.start("public")
    owner = await svc.start("owner")
    await svc.append_turn(public, Turn(role="user", content="p"))
    await svc.append_turn(owner, Turn(role="user", content="o"))
    await svc.drain()
    assert store.conversations[public.id]["expires_at"] == NOW + timedelta(days=90)
    assert store.conversations[owner.id]["expires_at"] is None


async def test_visitor_is_a_salted_hash_never_the_address(redis: FakeAsyncRedis) -> None:
    store = MemoryConversationStore()
    svc = _service(store, redis)
    conv = await svc.start("public", "203.0.113.5")
    await svc.append_turn(conv, Turn(role="user", content="x"))
    await svc.drain()
    stored = store.conversations[conv.id]["visitor_hash"]
    assert stored == hash_visitor("203.0.113.5", "test-salt")
    assert stored != hash_visitor("203.0.113.5", "other-salt")
    assert "203.0.113.5" not in repr(store.conversations) + repr(store.turns)


async def test_unknown_conversation_has_empty_history(redis: FakeAsyncRedis) -> None:
    svc = _service(MemoryConversationStore(), redis)
    assert await svc.history("missing") == []


async def test_cold_reload_failure_is_a_dependency_error(redis: FakeAsyncRedis) -> None:
    svc = _service(FailingStore(failures=None), redis)
    with pytest.raises(AppError) as err:
        await svc.history("anything")
    assert err.value.code == ErrorCode.DEPENDENCY_UNAVAILABLE


def test_empty_salt_is_refused(redis: FakeAsyncRedis) -> None:
    with pytest.raises(ValueError, match="VISITOR_HASH_SALT"):
        ConversationService(redis, MemoryConversationStore(), visitor_salt="")


async def test_drain_with_nothing_pending_returns(redis: FakeAsyncRedis) -> None:
    await asyncio.wait_for(_service(MemoryConversationStore(), redis).drain(), 1)
