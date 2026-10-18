"""Refresh an owner conversation's name and description from what was actually discussed.

The first question gives a conversation its starting name (`naming.py`); a chat that wandered
deserves a better one. The terminal client asks for a refresh when the owner leaves.

- The model runs at the conversation's own tier: an intake conversation, or one that touched
  confidential sources, is titled by a local model only. With no eligible model nothing changes.
- The description is always refreshed. The name only while the owner has never renamed it,
  and it stays unique.
- A conversation that has not grown since its last refresh is left alone.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Literal

import structlog
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from app.exceptions import AppError, ErrorCode
from app.services.conversations import Turn
from app.services.naming import describe, slugify_title, unique_name
from app.services.owner_chat import ConversationIndex, ConversationMeta
from llm_orchestrator import AllModelsFailedError, NoEligibleModelError, Orchestrator, Role, Tier

logger = structlog.get_logger(__name__)

MIN_TURNS = 2
TRANSCRIPT_TURNS = 12
TURN_CHARS = 400

TITLE_PROMPT_V1 = """You name a private conversation between the owner and their assistant.
Read the transcript and return:
- `title`: 2 to 5 plain words naming the main topic (no punctuation, no dates, no names of \
people), e.g. "system design interview prep" or "Acme Test offer comparison".
- `description`: one sentence of at most 100 characters saying what the conversation covered.
Describe only what is in the transcript."""

RefreshStatus = Literal["updated", "unchanged", "skipped", "scheduled"]


class TitleSuggestion(BaseModel):
    title: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1, max_length=300)


class TitleRefresh(BaseModel):
    """What happened. `reason` is set when skipped: too_short, up_to_date or no_model."""

    status: RefreshStatus
    name: str | None = None
    description: str | None = None
    renamed: bool = False
    reason: str | None = None


def conversation_tier(meta: ConversationMeta, turns: list[Turn]) -> Tier:
    """Intake drafts may hold anything the owner typed, so intake is always confidential;
    otherwise the highest tier any answer used (private when none recorded one)."""
    if meta.mode == "intake":
        return Tier.CONFIDENTIAL
    tiers = [Tier.parse(t.max_tier) for t in turns if t.max_tier]
    return max([Tier.PRIVATE, *tiers])


def transcript(turns: list[Turn]) -> str:
    lines = []
    for turn in turns[-TRANSCRIPT_TURNS:]:
        who = "owner" if turn.role == "user" else "assistant"
        text = " ".join(turn.content.split())[:TURN_CHARS]
        lines.append(f"{who}: {text}")
    return "\n".join(lines)


@dataclass
class ConversationTitler:
    index: ConversationIndex
    orchestrator: Orchestrator
    # Refreshes still running after the caller stopped waiting; held so they are not collected.
    _running: set[asyncio.Task[TitleRefresh]] = field(default_factory=set)

    async def refresh(self, conversation_id: str) -> TitleRefresh:
        meta = await self.index.get(conversation_id)
        if meta is None or meta.channel != "owner":
            raise AppError(ErrorCode.NOT_FOUND, "Conversation not found")
        if meta.message_count < MIN_TURNS:
            return self._skip(meta, "too_short")
        if meta.titled_turns >= meta.message_count:
            return self._skip(meta, "up_to_date")
        turns = await self.index.turns(conversation_id, limit=1000)
        tier = conversation_tier(meta, turns)
        suggestion = await self._suggest(turns, tier)
        if suggestion is None:
            return self._skip(meta, "no_model")
        name = None if meta.name_locked else await self._new_name(meta, suggestion.title)
        description = describe(suggestion.description)
        await self.index.save_refresh(conversation_id, name, description, meta.message_count)
        changed = name is not None or description != meta.description
        logger.info(
            "conversation title refreshed",
            conversation_id=conversation_id,
            tier=tier.label,
            renamed=name is not None,
            locked=meta.name_locked,
        )
        return TitleRefresh(
            status="updated" if changed else "unchanged",
            name=name or meta.name,
            description=description,
            renamed=name is not None,
        )

    async def refresh_within(self, conversation_id: str, wait_seconds: float) -> TitleRefresh:
        """Wait up to `wait_seconds`; a slow (local) model finishes in the background."""
        task = asyncio.create_task(self.refresh(conversation_id))
        self._running.add(task)
        task.add_done_callback(self._finished)
        done, _ = await asyncio.wait({task}, timeout=wait_seconds)
        if task in done:
            return task.result()
        return TitleRefresh(status="scheduled")

    def _finished(self, task: asyncio.Task[TitleRefresh]) -> None:
        self._running.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logger.warning(
                "conversation title refresh failed", error_type=type(task.exception()).__name__
            )

    async def _suggest(self, turns: list[Turn], tier: Tier) -> TitleSuggestion | None:
        messages = [
            SystemMessage(content=TITLE_PROMPT_V1),
            HumanMessage(content=f"Transcript:\n{transcript(turns)}"),
        ]
        try:
            return await self.orchestrator.structured(Role.ROUTER, tier, TitleSuggestion, messages)
        except (NoEligibleModelError, AllModelsFailedError) as exc:
            logger.info("conversation title not refreshed", reason=type(exc).__name__)
            return None

    async def _new_name(self, meta: ConversationMeta, title: str) -> str | None:
        base = slugify_title(title, meta.name or "")
        if not base or base == meta.name:
            return None
        taken = await self.index.names_like(base)
        taken.discard(meta.name or "")
        name = unique_name(base, taken)
        return None if name == meta.name else name

    @staticmethod
    def _skip(meta: ConversationMeta, reason: str) -> TitleRefresh:
        return TitleRefresh(
            status="skipped", name=meta.name, description=meta.description, reason=reason
        )
