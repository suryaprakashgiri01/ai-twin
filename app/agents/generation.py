"""How a graph's generate step gets text from a model, behind one small protocol.

`stream` yields answer text and fills a `Generation` with the model used and token counts.
Selection errors (`NoEligibleModelError`, `OverrideRejectedError`) and `AllModelsFailedError`
are raised before the first chunk; a failure after it raises `MidStreamError`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from langchain_core.messages import BaseMessage

from llm_orchestrator import Need, Orchestrator, Prefer, Role, Tier

Thinking = Callable[[str], None]


@dataclass
class Generation:
    model_id: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    fallback_used: bool = False
    # A reasoning model's thinking (owner channels show it; never part of the answer).
    thinking: list[str] = field(default_factory=list)


class MidStreamError(Exception):
    """A model failed after streaming had started. Carries no content."""

    def __init__(self, model_id: str, error_type: str) -> None:
        super().__init__(f"{model_id} failed mid-stream: {error_type}")
        self.model_id = model_id
        self.error_type = error_type


class AnswerGenerator(Protocol):
    def stream(
        self,
        tier: Tier,
        messages: Sequence[BaseMessage],
        out: Generation,
        *,
        model: str | None = None,
        prefer: Prefer | None = None,
        need: Need | None = None,
        on_thinking: Thinking | None = None,
    ) -> AsyncIterator[str]: ...


class ChatGenerator:
    """One non-streaming call through the orchestrator's retries and fallbacks."""

    def __init__(self, orchestrator: Orchestrator) -> None:
        self._orchestrator = orchestrator

    async def stream(
        self,
        tier: Tier,
        messages: Sequence[BaseMessage],
        out: Generation,
        *,
        model: str | None = None,
        prefer: Prefer | None = None,
        need: Need | None = None,
        on_thinking: Thinking | None = None,
    ) -> AsyncIterator[str]:
        result = await self._orchestrator.chat(
            Role.GENERATOR, tier, messages, model=model, prefer=prefer, privileged=True, need=need
        )
        if result.thinking:
            out.thinking.append(result.thinking)
            if on_thinking is not None:
                on_thinking(result.thinking)
        out.model_id = result.model_id
        out.input_tokens = result.usage.input_tokens
        out.output_tokens = result.usage.output_tokens
        out.fallback_used = result.fallback_used
        yield result.text
