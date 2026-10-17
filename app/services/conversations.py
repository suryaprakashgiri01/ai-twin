"""Conversation history, cache-aside: Redis serves the hot turns, MongoDB keeps the record.

`append_turn` writes Redis and returns; the MongoDB write runs in the background with retry, so
a slow or failing database never delays a reply. A turn whose persistence finally fails is logged
and counted, and stays readable from Redis until its TTL. `history` reads Redis and, on a miss,
reloads from MongoDB and repopulates the cache.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol

import structlog
from pydantic import BaseModel, Field, field_validator
from pymongo.errors import DuplicateKeyError
from redis.asyncio import Redis

from app.cache.redis import conversation_key, conversation_seq_key
from app.db.documents import Channel, Conversation, Message, Role, Tier, new_id, utcnow
from app.exceptions import AppError, ErrorCode

logger = structlog.get_logger(__name__)

# How long a conversation's turns stay hot in Redis after its last turn.
CACHE_TTL: dict[Channel, timedelta] = {
    "public": timedelta(hours=24),
    "owner": timedelta(days=14),
}

# Shutdown waits this long for background writes.
DRAIN_SECONDS = 5.0


def _to_millis(value: datetime) -> datetime:
    """MongoDB keeps milliseconds; truncating up front keeps cache and database copies equal."""
    return value.replace(microsecond=value.microsecond // 1000 * 1000)


class Turn(BaseModel):
    """One message in a conversation. `seq` is assigned by `append_turn`."""

    seq: int = 0
    role: Role
    content: str
    intent: str | None = None
    guards: dict[str, Any] = Field(default_factory=dict)
    max_tier: Tier | None = None
    model: str | None = None
    trace_id: str | None = None
    tokens: int | None = None
    latency_ms: float | None = None
    # Owner-agent metadata: the route taken, cited document ids, a partial answer, and how
    # many sources were withheld for tier reasons.
    route: str | None = None
    citations: list[str] = Field(default_factory=list)
    incomplete: bool = False
    withheld: int = 0
    created_at: datetime = Field(default_factory=lambda: _to_millis(utcnow()))

    @field_validator("created_at")
    @classmethod
    def _millis(cls, value: datetime) -> datetime:
        return _to_millis(value)


@dataclass(frozen=True)
class ConversationRef:
    id: str
    channel: Channel
    visitor_hash: str | None = None


@dataclass(frozen=True)
class StoredConversation:
    channel: Channel
    turns: list[Turn]


class ConversationStore(Protocol):
    """The durable side. `save_turn` must be idempotent per (conversation, seq)."""

    async def save_turn(
        self, conversation: ConversationRef, turn: Turn, expires_at: datetime | None
    ) -> None: ...

    async def load(self, conversation_id: str, limit: int) -> StoredConversation | None: ...


def hash_visitor(address: str, salt: str) -> str:
    """Keyed hash of a visitor address: stable per salt, not reversible without it."""
    return hmac.new(salt.encode(), address.encode(), hashlib.sha256).hexdigest()


class MongoConversationStore:
    async def save_turn(
        self, conversation: ConversationRef, turn: Turn, expires_at: datetime | None
    ) -> None:
        now = utcnow()
        await Conversation.get_pymongo_collection().update_one(
            {"_id": conversation.id},
            {
                "$setOnInsert": {
                    "channel": conversation.channel,
                    "visitor_hash": conversation.visitor_hash,
                    "created_at": turn.created_at,
                },
                "$set": {"updated_at": now, "expires_at": expires_at},
                "$max": {"message_count": turn.seq},
            },
            upsert=True,
        )
        message = Message(
            conversation_id=conversation.id,
            expires_at=expires_at,
            **turn.model_dump(),
        )
        # DuplicateKeyError: an earlier attempt landed after timing out on our side.
        with contextlib.suppress(DuplicateKeyError):
            await message.insert()

    async def load(self, conversation_id: str, limit: int) -> StoredConversation | None:
        conversation = await Conversation.get(conversation_id)
        if conversation is None:
            return None
        messages = (
            await Message.find(Message.conversation_id == conversation_id)
            .sort(-Message.seq)  # type: ignore[arg-type]
            .limit(limit)
            .to_list()
        )
        fields = set(Turn.model_fields)
        turns = [Turn.model_validate(m.model_dump(include=fields)) for m in reversed(messages)]
        return StoredConversation(channel=conversation.channel, turns=turns)


class ConversationService:
    def __init__(
        self,
        redis: Redis,
        store: ConversationStore,
        *,
        visitor_salt: str,
        cache_turns: int = 20,
        public_retention_days: int = 90,
        retry_attempts: int = 4,
        retry_base_delay: float = 0.25,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        if not visitor_salt:
            raise ValueError("VISITOR_HASH_SALT is empty; run the setup script")
        self._redis = redis
        self._store = store
        self._salt = visitor_salt
        self._cache_turns = cache_turns
        self._retention = timedelta(days=public_retention_days)
        self._attempts = retry_attempts
        self._base_delay = retry_base_delay
        self._clock = clock
        self._pending: set[asyncio.Task[None]] = set()
        # Turns whose MongoDB write failed after every retry (still in Redis until the TTL).
        self.persist_failures = 0

    def expires_at(self, channel: Channel) -> datetime | None:
        """Public conversations expire after the retention period; owner ones are kept."""
        return self._clock() + self._retention if channel == "public" else None

    async def start(self, channel: Channel, visitor_address: str | None = None) -> ConversationRef:
        visitor = hash_visitor(visitor_address, self._salt) if visitor_address else None
        ref = ConversationRef(id=new_id(), channel=channel, visitor_hash=visitor)
        # A known-new conversation: seed the counter so the first turn never reads MongoDB.
        ttl = CACHE_TTL[channel]
        await self._redis.set(conversation_seq_key(ref.id), 0, ex=ttl, nx=True)
        return ref

    async def append_turn(self, conversation: ConversationRef, turn: Turn) -> Turn:
        """Write to Redis and return; persistence to MongoDB continues in the background."""
        list_key = conversation_key(conversation.id)
        seq_key = conversation_seq_key(conversation.id)
        if not await self._redis.exists(seq_key):
            await self._warm(conversation.id, conversation.channel)
        seq = int(await self._redis.incr(seq_key))
        stamped = turn.model_copy(update={"seq": seq})
        ttl = CACHE_TTL[conversation.channel]
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.rpush(list_key, stamped.model_dump_json())
            pipe.ltrim(list_key, -self._cache_turns, -1)
            pipe.expire(list_key, ttl)
            pipe.expire(seq_key, ttl)
            await pipe.execute()
        self._spawn(self._persist(conversation, stamped))
        return stamped

    async def history(self, conversation_id: str) -> list[Turn]:
        cached = await self._redis.lrange(conversation_key(conversation_id), 0, -1)
        if cached:
            return [Turn.model_validate_json(raw) for raw in cached]
        stored = await self._warm(conversation_id)
        return stored.turns if stored else []

    async def drain(self) -> None:
        """Wait for in-flight writes (shutdown); whatever is left stays in Redis."""
        if self._pending:
            await asyncio.wait(set(self._pending), timeout=DRAIN_SECONDS)

    async def _warm(
        self, conversation_id: str, channel: Channel | None = None
    ) -> StoredConversation | None:
        """Repopulate Redis from MongoDB, restoring the seq counter so numbering continues."""
        try:
            stored = await self._store.load(conversation_id, self._cache_turns)
        except Exception as exc:
            logger.warning("conversation reload failed", error_type=type(exc).__name__)
            raise AppError(
                ErrorCode.DEPENDENCY_UNAVAILABLE, "Conversation store unavailable"
            ) from exc
        if stored is None:
            if channel is not None:
                await self._redis.set(
                    conversation_seq_key(conversation_id), 0, ex=CACHE_TTL[channel], nx=True
                )
            return None
        ttl = CACHE_TTL[stored.channel]
        last_seq = stored.turns[-1].seq if stored.turns else 0
        list_key = conversation_key(conversation_id)
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.delete(list_key)
            if stored.turns:
                pipe.rpush(list_key, *(t.model_dump_json() for t in stored.turns))
                pipe.expire(list_key, ttl)
            pipe.set(conversation_seq_key(conversation_id), last_seq, ex=ttl, nx=True)
            await pipe.execute()
        return stored

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> None:
        task = asyncio.create_task(coro)
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def _persist(self, conversation: ConversationRef, turn: Turn) -> None:
        expires = self.expires_at(conversation.channel)
        for attempt in range(1, self._attempts + 1):
            try:
                await self._store.save_turn(conversation, turn, expires)
                return
            except Exception as exc:
                logger.warning(
                    "conversation persist failed",
                    conversation_id=conversation.id,
                    seq=turn.seq,
                    attempt=attempt,
                    error_type=type(exc).__name__,
                )
                if attempt < self._attempts:
                    await asyncio.sleep(self._base_delay * 2 ** (attempt - 1))
        self.persist_failures += 1
        logger.error(
            "conversation turn not persisted",
            conversation_id=conversation.id,
            seq=turn.seq,
            failures=self.persist_failures,
        )
