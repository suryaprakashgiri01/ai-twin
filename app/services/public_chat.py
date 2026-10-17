"""Public chat: the visitor-facing request and response shapes, and the service protocol."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from app.auth.visitor import Visitor
from app.kb.retrieval.context import Source

# A hard ceiling for the request body; the input guard enforces the real, lower cap.
REQUEST_MAX_CHARS = 8000


class PublicChatRequest(BaseModel):
    # No tier, collection, model or filter field: nothing a visitor sends can widen retrieval.
    model_config = ConfigDict(extra="forbid")

    conversation_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")
    message: str = Field(min_length=1, max_length=REQUEST_MAX_CHARS)


class PublicChatResponse(BaseModel):
    conversation_id: str
    seq: int | None = None
    intent: str
    answer: str
    sources: list[Source] = Field(default_factory=list)
    citations: list[str] = Field(default_factory=list)
    cache_hit: bool = False
    latency_ms: float | None = None


@dataclass(frozen=True)
class PublicPrepared:
    request: PublicChatRequest
    visitor: Visitor
    conversation_id: str
    # True for the first message of a conversation: only those use the answer cache.
    single_turn: bool


class PublicChat(Protocol):
    async def prepare(self, request: PublicChatRequest, visitor: Visitor) -> PublicPrepared: ...

    def sse(self, prepared: PublicPrepared) -> AsyncIterator[str]: ...

    async def answer(self, prepared: PublicPrepared) -> PublicChatResponse: ...
