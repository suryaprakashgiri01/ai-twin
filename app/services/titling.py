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

from app.agents.prompts.owner import says_not_in_vault
from app.exceptions import AppError, ErrorCode
from app.observability.tracing import tracing
from app.services.conversations import Turn
from app.services.naming import describe, slugify_title, unique_name
from app.services.owner_chat import ConversationIndex, ConversationMeta
from llm_orchestrator import AllModelsFailedError, NoEligibleModelError, Orchestrator, Role, Tier

logger = structlog.get_logger(__name__)

MIN_TURNS = 2
# Owner messages carry the topic; replies only their outcome and a glimpse.
OWNER_MESSAGES = 20
HEAD = 6
OWNER_CHARS = 220
REPLY_CHARS = 120

PROMPT_VERSION = "title-v2"
TITLE_PROMPT_V2 = """You name a private conversation between the owner and their assistant.
You get every message the owner sent, in order, and each assistant reply's outcome.
Return:
- `title`: 2 to 5 plain words for the topic MOST of the owner's messages are about. Weigh the \
whole conversation; early messages count as much as late ones. No punctuation, dates or names \
of people, e.g. "system design interview prep" or "magnus eval tech stack".
- `description`: one sentence of at most 100 characters on what the owner asked or did.
Rules: say an action happened only when its outcome is `done`; for `error` say it was tried or \
failed; never invent results. Describe only what is below."""

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


def _clip(text: str, limit: int) -> str:
    line = " ".join(text.split())
    return line if len(line) <= limit else line[: limit - 1] + "…"


def outcome(turn: Turn) -> str:
    """What an assistant reply amounted to, from metadata rather than its wording."""
    route = turn.route or ""
    if route.startswith("intake:"):
        return route.split(":", 1)[1]  # done, error, draft, asked, answer, chat
    if turn.incomplete:
        return "failed"
    if says_not_in_vault(turn.content):
        return "not found"
    return "answered"


def _spread[T](items: list[T], keep: int, head: int) -> tuple[list[T], int]:
    """The first `head` and the latest items when there are too many; returns what was cut."""
    if len(items) <= keep:
        return items, 0
    tail = keep - head
    return [*items[:head], *items[-tail:]], len(items) - keep


def transcript(turns: list[Turn]) -> str:
    """Every owner message (long chats keep the start and the end) and each reply's outcome."""
    asked, cut_asked = _spread([t for t in turns if t.role == "user"], OWNER_MESSAGES, HEAD)
    replies, cut_replies = _spread(
        [t for t in turns if t.role == "assistant"], OWNER_MESSAGES, HEAD
    )
    lines = ["Owner messages, in order:"]
    lines += [f"{i}. {_clip(t.content, OWNER_CHARS)}" for i, t in enumerate(asked, 1)]
    if cut_asked:
        lines.insert(HEAD + 1, f"(… {cut_asked} more messages …)")
    lines.append("Assistant replies, in order (outcome: first words):")
    lines += [
        f"{i}. {outcome(t)}: {_clip(t.content, REPLY_CHARS)}" for i, t in enumerate(replies, 1)
    ]
    if cut_replies:
        lines.append(f"(… {cut_replies} earlier replies left out …)")
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
        with tracing().run(
            "title-refresh",
            "intake" if meta.mode == "intake" else "owner",
            session_id=conversation_id,
            metadata={"tier": tier.label, "turns": len(turns), "prompt_version": PROMPT_VERSION},
        ) as trace:
            suggestion = await self._suggest(turns, tier)
            trace.tag("outcome:no_model" if suggestion is None else "outcome:suggested")
            trace.output(suggestion.model_dump() if suggestion is not None else None)
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
            SystemMessage(content=TITLE_PROMPT_V2),
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
