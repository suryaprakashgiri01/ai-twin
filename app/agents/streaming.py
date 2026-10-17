"""Token streaming with pre-first-token fallback, and Server-Sent Events encoding.

A model that fails or stalls before its first token is skipped for the next eligible one. Once
a token has gone out, switching models would splice two answers together, so a later failure
raises `MidStreamError` and the caller keeps the partial answer, marked incomplete.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Sequence
from typing import Any

import structlog
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage, BaseMessageChunk
from langchain_core.runnables import RunnableConfig

from app.agents.generation import Generation, MidStreamError
from llm_orchestrator import (
    AllModelsFailedError,
    Attempt,
    ModelSpec,
    Orchestrator,
    Prefer,
    Role,
    Tier,
    TokenUsage,
)
from llm_orchestrator.executor import CallContext, classify_error, log_attempt

logger = structlog.get_logger(__name__)


def sse(event: str, data: dict[str, Any]) -> str:
    """One SSE frame. JSON never contains a raw newline, so a single `data:` line suffices."""
    return f"event: {event}\ndata: {json.dumps(data, default=str, ensure_ascii=False)}\n\n"


class StreamingGenerator:
    def __init__(self, orchestrator: Orchestrator, *, idle_timeout_seconds: float = 30.0) -> None:
        self._orchestrator = orchestrator
        self._idle_timeout = idle_timeout_seconds

    async def stream(
        self,
        tier: Tier,
        messages: Sequence[BaseMessage],
        out: Generation,
        *,
        model: str | None = None,
        prefer: Prefer | None = None,
    ) -> AsyncIterator[str]:
        candidates = await self._orchestrator.chat_models(
            Role.GENERATOR, tier, model=model, prefer=prefer, privileged=True
        )
        ctx = CallContext(role=Role.GENERATOR, tier=tier)
        for index, (spec, chat) in enumerate(candidates):
            started = time.perf_counter()
            streamed = False
            try:
                async for chunk in self._chunks(chat, spec, messages, ctx):
                    self._count(out, chunk)
                    text = chunk.text
                    if not text:
                        continue
                    if not streamed:
                        streamed = True
                        out.model_id, out.fallback_used = spec.id, index > 0
                    yield text
            except Exception as exc:
                self._failed(ctx, spec, started, exc)
                if streamed:
                    raise MidStreamError(spec.id, type(exc).__name__) from exc
                continue
            out.model_id, out.fallback_used = spec.id, index > 0
            usage = TokenUsage(input_tokens=out.input_tokens, output_tokens=out.output_tokens)
            log_attempt(ctx, spec, number=1, outcome="ok", latency_ms=_ms(started), usage=usage)
            return
        raise AllModelsFailedError(Role.GENERATOR, tier, ctx.attempts)

    async def _chunks(
        self,
        chat: BaseChatModel,
        spec: ModelSpec,
        messages: Sequence[BaseMessage],
        ctx: CallContext,
    ) -> AsyncIterator[BaseMessageChunk]:
        """The model's chunks; a gap longer than the idle timeout counts as a failure."""
        config: RunnableConfig = {
            "metadata": {
                "llm_role": ctx.role.value,
                "llm_tier": ctx.tier.label,
                "llm_model": spec.id,
            }
        }
        iterator = aiter(chat.astream(list(messages), config=config))
        while True:
            try:
                async with asyncio.timeout(self._idle_timeout):
                    chunk = await anext(iterator)
            except StopAsyncIteration:
                return
            yield chunk

    @staticmethod
    def _count(out: Generation, chunk: BaseMessageChunk) -> None:
        usage = getattr(chunk, "usage_metadata", None)
        if usage:
            out.input_tokens += usage.get("input_tokens", 0)
            out.output_tokens += usage.get("output_tokens", 0)

    @staticmethod
    def _failed(ctx: CallContext, spec: ModelSpec, started: float, exc: Exception) -> None:
        transient, status = classify_error(exc)
        attempt = Attempt(
            model_id=spec.id,
            number=1,
            outcome="transient_error" if transient else "permanent_error",
            latency_ms=_ms(started),
            error_type=type(exc).__name__,
            status_code=status,
        )
        ctx.attempts.append(attempt)
        log_attempt(
            ctx,
            spec,
            number=1,
            outcome=attempt.outcome,
            latency_ms=attempt.latency_ms,
            error_type=attempt.error_type,
            status_code=status,
        )


def _ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 1)
