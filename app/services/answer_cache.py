"""The public answer cache: guarded single-turn answers, keyed by question and KB version.

The key is SHA-256 of the normalised question plus `kb_public_version`, which every publish and
unpublish bumps, so an answer cached before the public KB changed is never served after. Only
answers that passed the output guard (or the fixed no-context reply) are stored. TTL is
`PUBLIC_ANSWER_CACHE_TTL_SECONDS`; 0 turns the cache off.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from typing import Any

from pydantic import BaseModel, Field
from redis.asyncio import Redis

from app.cache.redis import KB_PUBLIC_VERSION_KEY, answer_cache_key
from app.kb.retrieval.context import Source


class CachedAnswer(BaseModel):
    intent: str
    answer: str
    sources: list[Source] = Field(default_factory=list)
    model: str | None = None
    guards: dict[str, Any] = Field(default_factory=dict)


def normalise_question(text: str) -> str:
    folded = unicodedata.normalize("NFKC", text).casefold()
    return re.sub(r"\s+", " ", folded).strip().rstrip("?!. ")


def question_hash(text: str) -> str:
    return hashlib.sha256(normalise_question(text).encode()).hexdigest()


async def public_version(redis: Redis) -> int:
    return int(await redis.get(KB_PUBLIC_VERSION_KEY) or 0)


async def bump_public_version(redis: Redis) -> int:
    return int(await redis.incr(KB_PUBLIC_VERSION_KEY))


class AnswerCache:
    def __init__(self, redis: Redis, ttl_seconds: int) -> None:
        self._redis = redis
        self._ttl = ttl_seconds

    @property
    def enabled(self) -> bool:
        return self._ttl > 0

    async def lookup(self, question: str) -> tuple[int, CachedAnswer | None]:
        """The current version (to store a miss under later) and the cached answer, if any."""
        version = await public_version(self._redis)
        raw = await self._redis.get(answer_cache_key(version, question_hash(question)))
        return version, CachedAnswer.model_validate_json(raw) if raw else None

    async def store(self, version: int, question: str, answer: CachedAnswer) -> None:
        key = answer_cache_key(version, question_hash(question))
        await self._redis.set(key, answer.model_dump_json(), ex=self._ttl)
