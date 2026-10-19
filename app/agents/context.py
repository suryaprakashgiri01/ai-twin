"""Prompt blocks the owner agents share: the owner's preferences and the conversation summary.

- **Owner preferences**: active memories only (pending ones never), at most
  `MEMORY_CONTEXT_ITEMS` within `MEMORY_CONTEXT_TOKENS`, ordered instructions → preferences →
  aliases, then most used. A confidential memory is shown only to a call at confidential tier
  (a local model).
- **Earlier in this conversation**: the rolling summary, only when the conversation is longer
  than the recent turns the prompt carries, within `SUMMARY_CONTEXT_TOKENS`, and only to a call
  whose tier covers what the summary was made from.

The public agent never builds an `OwnerContext`: it has no dependency on any of this.
Everything here is plain data, so the intake graph can keep it in its checkpointed state.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

import structlog
from pydantic import BaseModel, Field

from llm_orchestrator import Tier

logger = structlog.get_logger(__name__)

CHARS_PER_TOKEN = 4
KIND_ORDER = {"instruction": 0, "preference": 1, "alias": 2}
KIND_LABEL = {"instruction": "Rule", "preference": "Preference", "alias": "Alias"}
PREFERENCES_HEAD = (
    "Owner preferences (the owner set these; follow them unless the current message says "
    "otherwise):"
)
EARLIER_HEAD = "Earlier in this conversation (a summary of turns no longer shown):"


class MemoryLine(BaseModel):
    kind: str
    text: str
    tier: str = "private"
    uses: int = 1


class OwnerContext(BaseModel):
    memories: list[MemoryLine] = Field(default_factory=list)
    summary: str | None = None
    summary_tier: str = "private"
    # How many turns (messages) the conversation has, this one excluded.
    turns: int = 0
    memory_items: int = 20
    memory_tokens: int = 400
    summary_tokens: int = 300

    def preferences(self, tier: Tier) -> str:
        allowed = [m for m in self.memories if Tier.parse(m.tier) <= tier]
        allowed.sort(key=lambda m: (KIND_ORDER.get(m.kind, 9), -m.uses))
        budget = self.memory_tokens * CHARS_PER_TOKEN - len(PREFERENCES_HEAD)
        lines: list[str] = []
        for item in allowed[: self.memory_items]:
            line = f"- {KIND_LABEL.get(item.kind, 'Note')}: {item.text}"
            if len(line) + 1 > budget:
                break
            lines.append(line)
            budget -= len(line) + 1
        return "\n".join([PREFERENCES_HEAD, *lines]) if lines else ""

    def earlier(self, tier: Tier, shown_turns: int) -> str:
        """The summary when older turns are no longer in the prompt and `tier` may read it."""
        if not self.summary or self.turns <= shown_turns:
            return ""
        if Tier.parse(self.summary_tier) > tier:
            return ""
        limit = self.summary_tokens * CHARS_PER_TOKEN
        text = self.summary if len(self.summary) <= limit else self.summary[: limit - 1] + "…"
        return f"{EARLIER_HEAD}\n{text}"

    def system(self, base: str, tier: Tier, shown_turns: int) -> str:
        """`base` with the preferences and the summary appended, as one system prompt."""
        blocks = [b for b in (self.preferences(tier), self.earlier(tier, shown_turns)) if b]
        return "\n\n".join([base, *blocks])


class MemoryItem(Protocol):
    @property
    def kind(self) -> str: ...

    @property
    def text(self) -> str: ...

    @property
    def tier(self) -> str: ...

    @property
    def uses(self) -> int: ...


class ActiveMemories(Protocol):
    async def active(self) -> Sequence[MemoryItem]: ...


class SummaryMeta(Protocol):
    @property
    def summary(self) -> str | None: ...

    @property
    def summary_tier(self) -> str | None: ...

    @property
    def message_count(self) -> int: ...


class SummaryIndex(Protocol):
    async def get(self, conversation_id: str) -> SummaryMeta | None: ...


class ContextBuilder:
    def __init__(
        self,
        memory: ActiveMemories | None,
        index: SummaryIndex | None,
        *,
        memory_items: int = 20,
        memory_tokens: int = 400,
        summary_tokens: int = 300,
    ) -> None:
        self._memory = memory
        self._index = index
        self._limits = {
            "memory_items": memory_items,
            "memory_tokens": memory_tokens,
            "summary_tokens": summary_tokens,
        }

    async def load(self, conversation_id: str | None, last_seq: int = 0) -> OwnerContext:
        """Never fails a turn: a store error means a prompt without these blocks."""
        context = OwnerContext(turns=last_seq, **self._limits)
        try:
            if self._memory is not None:
                context.memories = [
                    MemoryLine(kind=m.kind, text=m.text, tier=m.tier, uses=m.uses)
                    for m in await self._memory.active()
                ]
            meta = (
                await self._index.get(conversation_id) if self._index and conversation_id else None
            )
        except Exception as exc:
            logger.warning("owner context unavailable", error_type=type(exc).__name__)
            return context
        if meta is not None and meta.summary:
            context.summary = meta.summary
            context.summary_tier = meta.summary_tier or "private"
            context.turns = max(last_seq, meta.message_count)
        return context
