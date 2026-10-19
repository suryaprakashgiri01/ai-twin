"""Digest an owner conversation: one structured call returns its title, description, rolling
summary and memory candidates.

The first question gives a conversation its starting name (`naming.py`); a chat that wandered
deserves a better one, and a long one needs a summary so its beginning is not lost.

- **When.** In the background every `SUMMARY_EVERY_TURNS` new turns (summary and candidates
  only), and when the owner leaves (`/exit` → POST …/title: everything). A periodic run is
  skipped while another one runs for the same conversation; it never delays a reply.
- **Tier.** The model runs at the conversation's own tier: an intake conversation, or one that
  touched confidential sources, is digested by a local model only. With no eligible model
  nothing changes.
- **Rolling.** The model gets the summary so far and the turns after it, so the input stays
  bounded however long the conversation grows.
- **Truthful.** Replies are given by their real outcome (`done`, `error`, `not found`) from
  metadata; a summary that hides a failed action gets a note saying it failed.
- **Title.** The description is always refreshed; the name only while the owner has never
  renamed it, and it stays unique. A conversation that has not grown is left alone.
- **Memories.** Candidates go to the learner (`MemoryService.learn`), which keeps only what the
  owner's own messages support and stores it as pending.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol, cast

import structlog
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from app.agents.prompts.owner import says_not_in_vault
from app.db.documents import Tier as TierLabel
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
SUMMARY_MAX = 1200
MAX_CANDIDATES = 8
# A periodic run waits this long for the turn that triggered it to reach MongoDB.
PERSIST_WAIT_SECONDS = 5.0
PERSIST_POLL_SECONDS = 0.2

PROMPT_VERSION = "digest-v1"
DIGEST_PROMPT_V1 = """You keep the record of a private conversation between the owner and \
their assistant. You get the summary so far (if any), then every message the owner sent after \
it, in order, and each assistant reply's outcome.
Return:
- `title`: 2 to 5 plain words for the topic MOST of the conversation is about. Weigh the whole \
conversation, the summary so far included. No punctuation, dates or names of people, e.g. \
"system design interview prep" or "magnus eval tech stack".
- `description`: one sentence of at most 100 characters on what the owner asked or did.
- `summary`: at most 1000 characters. The summary so far merged with the new messages: the \
topics, what was decided, and what happened.
- `memories`: what the OWNER asked of the assistant that should last beyond this conversation, \
taken from the owner's messages only, never from replies. Each has `kind` and `text` (one \
short sentence) and `confidence` (0 to 1, how sure you are the owner meant it to last):
  - `instruction`: a standing rule, e.g. "Show dates as 12 Oct 2026".
  - `preference`: how they like answers, e.g. "Concise bullets with sources first".
  - `alias`: a name they use, e.g. "magnus-eval = Magnus Eval project".
  Facts about the owner's jobs, interviews, projects or life are NOT memories. Most \
conversations have none: return [].
Rules: say an action happened only when its outcome is `done`; for `error` or `failed` say it \
was tried and failed; for `not found` say the vault did not have it; never invent results. \
Describe only what is below."""

FAILED_OUTCOMES = frozenset({"error", "failed"})
FAILURE_WORDS = re.compile(
    r"\b(fail\w*|error\w*|could ?n[o']t|did ?n[o']t|not (saved|done|added|created|stored)|"
    r"unsuccessful|was tried)\b",
    re.I,
)

RefreshStatus = Literal["updated", "unchanged", "skipped", "scheduled"]


class MemoryCandidate(BaseModel):
    """What the model thinks the owner wants remembered; code decides what is kept."""

    kind: str = Field(max_length=40)
    text: str = Field(max_length=1000)
    confidence: float = Field(default=0.5, ge=0, le=1)


class Digest(BaseModel):
    title: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1, max_length=300)
    summary: str = Field(min_length=1, max_length=4000)
    memories: list[MemoryCandidate] = Field(default_factory=list)


class TitleRefresh(BaseModel):
    """What happened. `reason` is set when skipped: too_short, up_to_date or no_model."""

    status: RefreshStatus
    name: str | None = None
    description: str | None = None
    renamed: bool = False
    reason: str | None = None
    summary_updated: bool = False
    # Learned memories now waiting for the owner's review (pending), from this refresh.
    new_memories: int = 0


class Learner(Protocol):
    async def learn(
        self,
        candidates: Sequence[MemoryCandidate],
        *,
        owner_text: str,
        conversation_id: str,
        tier: Tier,
    ) -> int:
        """Store what the owner's own words support; returns how many new items are pending."""
        ...


def conversation_tier(meta: ConversationMeta, turns: list[Turn]) -> Tier:
    """Intake drafts may hold anything the owner typed, so intake is always confidential;
    otherwise the highest tier any answer (or the summary so far) used, private at least."""
    if meta.mode == "intake":
        return Tier.CONFIDENTIAL
    tiers = [Tier.parse(t.max_tier) for t in turns if t.max_tier]
    if meta.summary_tier:
        tiers.append(Tier.parse(meta.summary_tier))
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


def transcript(turns: list[Turn], summary: str | None = None) -> str:
    """The summary so far, every owner message after it (long stretches keep the start and the
    end) and each reply's outcome."""
    asked, cut_asked = _spread([t for t in turns if t.role == "user"], OWNER_MESSAGES, HEAD)
    replies, cut_replies = _spread(
        [t for t in turns if t.role == "assistant"], OWNER_MESSAGES, HEAD
    )
    lines = [f"Summary so far: {summary}"] if summary else []
    lines.append("Owner messages, in order:" if not summary else "Owner messages since then:")
    start = len(lines)
    lines += [f"{i}. {_clip(t.content, OWNER_CHARS)}" for i, t in enumerate(asked, 1)]
    if not asked:
        lines.append("(none)")
    if cut_asked:
        lines.insert(start + HEAD, f"(… {cut_asked} more messages …)")
    lines.append("Assistant replies, in order (outcome: first words):")
    lines += [
        f"{i}. {outcome(t)}: {_clip(t.content, REPLY_CHARS)}" for i, t in enumerate(replies, 1)
    ]
    if cut_replies:
        lines.append(f"(… {cut_replies} earlier replies left out …)")
    return "\n".join(lines)


def truthful_summary(summary: str, turns: list[Turn]) -> str:
    """At most SUMMARY_MAX characters, and never silent about a failed action: when replies
    failed and the summary does not say so, a note is added."""
    failed = sum(1 for t in turns if t.role == "assistant" and outcome(t) in FAILED_OUTCOMES)
    text = " ".join(summary.split())
    note = ""
    if failed and not FAILURE_WORDS.search(text):
        what = f"{failed} attempted actions" if failed > 1 else "1 attempted action"
        note = f" Note: {what} failed; nothing was saved by {'them' if failed > 1 else 'it'}."
    room = SUMMARY_MAX - len(note)
    if len(text) > room:
        text = text[: room - 1].rstrip() + "…"
    return text + note


def owner_text(turns: list[Turn]) -> str:
    """The owner's own messages: the only text a memory may come from."""
    return "\n".join(t.content for t in turns if t.role == "user")


@dataclass
class ConversationDigester:
    index: ConversationIndex
    orchestrator: Orchestrator
    learner: Learner | None = None
    every_turns: int = 6
    sleep: Callable[[float], Awaitable[None]] = field(default=asyncio.sleep)
    # Runs still going after the caller stopped waiting; held so they are not collected.
    _running: set[asyncio.Task[TitleRefresh]] = field(default_factory=set)
    # Conversations with a periodic summary in progress.
    _summarizing: set[str] = field(default_factory=set)

    # --- on exit -----------------------------------------------------------------------------

    async def refresh(self, conversation_id: str) -> TitleRefresh:
        meta = await self._owner_meta(conversation_id)
        if meta.message_count < MIN_TURNS:
            return self._skip(meta, "too_short")
        if meta.titled_turns >= meta.message_count and meta.summarized_turns >= meta.message_count:
            return self._skip(meta, "up_to_date")
        turns = await self.index.turns(conversation_id, limit=1000)
        result = await self._digest(meta, turns, "exit")
        if result is None:
            return self._skip(meta, "no_model")
        digest, new_memories = result
        name = None if meta.name_locked else await self._new_name(meta, digest.title)
        description = describe(digest.description)
        await self.index.save_refresh(conversation_id, name, description, meta.message_count)
        changed = name is not None or description != meta.description
        logger.info(
            "conversation digested",
            conversation_id=conversation_id,
            trigger="exit",
            renamed=name is not None,
            locked=meta.name_locked,
            new_memories=new_memories,
        )
        return TitleRefresh(
            status="updated" if changed else "unchanged",
            name=name or meta.name,
            description=description,
            renamed=name is not None,
            summary_updated=True,
            new_memories=new_memories,
        )

    async def refresh_within(self, conversation_id: str, wait_seconds: float) -> TitleRefresh:
        """Wait up to `wait_seconds`; a slow (local) model finishes in the background."""
        task = asyncio.create_task(self.refresh(conversation_id))
        self._keep(task)
        done, _ = await asyncio.wait({task}, timeout=wait_seconds)
        if task in done:
            return task.result()
        return TitleRefresh(status="scheduled")

    # --- every few turns ---------------------------------------------------------------------

    def after_turn(self, conversation_id: str, seq: int | None) -> None:
        """Called once a turn is stored. Schedules a summary when enough new turns piled up;
        never waits, never raises."""
        if self.every_turns <= 0 or seq is None or conversation_id in self._summarizing:
            return
        self._summarizing.add(conversation_id)
        task = asyncio.create_task(self._periodic(conversation_id, seq))
        task.add_done_callback(lambda _t: self._summarizing.discard(conversation_id))
        self._keep(task)

    async def _periodic(self, conversation_id: str, seq: int) -> TitleRefresh:
        meta = await self.index.get(conversation_id)
        if meta is None or meta.channel != "owner":
            # The first turns may not have reached MongoDB yet; the next turn tries again.
            return TitleRefresh(status="skipped", reason="not_stored")
        if seq - meta.summarized_turns < self.every_turns:
            return self._skip(meta, "up_to_date")
        turns = await self._turns_upto(conversation_id, seq)
        result = await self._digest(meta, turns, "periodic")
        if result is None:
            return self._skip(meta, "no_model")
        logger.info(
            "conversation digested",
            conversation_id=conversation_id,
            trigger="periodic",
            new_memories=result[1],
        )
        return TitleRefresh(status="updated", summary_updated=True, new_memories=result[1])

    async def _turns_upto(self, conversation_id: str, seq: int) -> list[Turn]:
        """The stored turns, once the one with `seq` has been persisted (or the wait is over)."""
        waited = 0.0
        while True:
            turns = await self.index.turns(conversation_id, limit=1000)
            if (turns and turns[-1].seq >= seq) or waited >= PERSIST_WAIT_SECONDS:
                return turns
            await self.sleep(PERSIST_POLL_SECONDS)
            waited += PERSIST_POLL_SECONDS

    # --- the call ----------------------------------------------------------------------------

    async def _digest(
        self, meta: ConversationMeta, turns: list[Turn], trigger: str
    ) -> tuple[Digest, int] | None:
        tier = conversation_tier(meta, turns)
        fresh = [t for t in turns if t.seq > meta.summarized_turns] if meta.summary else turns
        covered = turns[-1].seq if turns else meta.message_count
        with tracing().run(
            "conversation-digest",
            "intake" if meta.mode == "intake" else "owner",
            session_id=meta.id,
            metadata={
                "tier": tier.label,
                "turns": len(fresh),
                "trigger": trigger,
                "prompt_version": PROMPT_VERSION,
            },
        ) as trace:
            digest = await self._call(fresh, meta.summary, tier)
            trace.tag("outcome:no_model" if digest is None else "outcome:digested")
            trace.output(digest.model_dump() if digest is not None else None)
        if digest is None:
            return None
        summary = truthful_summary(digest.summary, fresh)
        await self.index.save_summary(meta.id, summary, covered, cast(TierLabel, tier.label))
        new_memories = await self._learn(digest, fresh, meta.id, tier)
        return digest, new_memories

    async def _call(self, turns: list[Turn], summary: str | None, tier: Tier) -> Digest | None:
        messages = [
            SystemMessage(content=DIGEST_PROMPT_V1),
            HumanMessage(content=f"Transcript:\n{transcript(turns, summary)}"),
        ]
        try:
            return await self.orchestrator.structured(Role.ROUTER, tier, Digest, messages)
        except (NoEligibleModelError, AllModelsFailedError) as exc:
            logger.info("conversation not digested", reason=type(exc).__name__)
            return None

    async def _learn(
        self, digest: Digest, turns: list[Turn], conversation_id: str, tier: Tier
    ) -> int:
        if self.learner is None or not digest.memories:
            return 0
        candidates = digest.memories[:MAX_CANDIDATES]
        try:
            return await self.learner.learn(
                candidates,
                owner_text=owner_text(turns),
                conversation_id=conversation_id,
                tier=tier,
            )
        except Exception as exc:
            # The summary and title are saved already; a failed learn loses only candidates.
            logger.warning("memory learning failed", error_type=type(exc).__name__)
            return 0

    # --- helpers -----------------------------------------------------------------------------

    async def _owner_meta(self, conversation_id: str) -> ConversationMeta:
        meta = await self.index.get(conversation_id)
        if meta is None or meta.channel != "owner":
            raise AppError(ErrorCode.NOT_FOUND, "Conversation not found")
        return meta

    def _keep(self, task: asyncio.Task[TitleRefresh]) -> None:
        self._running.add(task)
        task.add_done_callback(self._finished)

    def _finished(self, task: asyncio.Task[TitleRefresh]) -> None:
        self._running.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logger.warning("conversation digest failed", error_type=type(task.exception()).__name__)

    async def drain(self, wait_seconds: float) -> None:
        """Wait for running digests (tests, shutdown); whatever is left is abandoned."""
        if self._running:
            await asyncio.wait(set(self._running), timeout=wait_seconds)

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
