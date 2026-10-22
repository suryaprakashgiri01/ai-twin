"""Conversation digest: one call for title, summary and memory candidates; tier-aware model
choice, name lock, uniqueness, skips, periodic runs and truthful summaries."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, Literal

import pytest

from app.exceptions import AppError
from app.services.conversations import Turn
from app.services.digest import (
    ConversationDigester,
    MemoryCandidate,
    conversation_tier,
    truthful_summary,
)
from app.services.owner_chat import ConversationMeta, ConversationSummary
from llm_orchestrator import Tier
from tests.llm_orchestrator.fakes import Factory, inventory, orchestrator, spec

NOW = datetime(2026, 10, 18, tzinfo=UTC)
SUGGESTION = json.dumps(
    {
        "title": "System design interview prep",
        "description": "Rate limiting and caching prep.",
        "summary": "The owner prepared rate limiting and caching for a system design round.",
        "memories": [],
    }
)


class MemoryIndex:
    def __init__(self) -> None:
        self.metas: dict[str, ConversationMeta] = {}
        self.turn_lists: dict[str, list[Turn]] = {}
        self.saved: list[tuple[str, str | None, str, int]] = []
        self.summaries: list[tuple[str, str, int, str]] = []

    def add(self, cid: str, turns: list[Turn], **meta: Any) -> None:
        values: dict[str, Any] = {
            "id": cid,
            "channel": "owner",
            "created_at": NOW,
            "updated_at": NOW,
            "message_count": len(turns),
            "name": "discuss-system-design",
            **meta,
        }
        self.metas[cid] = ConversationMeta(**values)
        self.turn_lists[cid] = turns

    async def get(self, conversation_id: str) -> ConversationMeta | None:
        return self.metas.get(conversation_id)

    async def list_owner(self, limit: int) -> list[ConversationSummary]:
        return []

    async def list_channel(
        self, limit: int, channel: str, visitor_hash: str | None = None
    ) -> list[ConversationSummary]:
        return []

    async def by_name(self, name: str) -> ConversationMeta | None:
        return next((m for m in self.metas.values() if m.name == name), None)

    async def names_like(self, base: str) -> set[str]:
        return {m.name for m in self.metas.values() if m.name and m.name.startswith(base)}

    async def set_title(
        self, conversation_id: str, name: str | None, description: str | None
    ) -> None:
        return None

    async def lock_name(self, conversation_id: str) -> None:
        self.metas[conversation_id].name_locked = True

    async def save_refresh(
        self, conversation_id: str, name: str | None, description: str, titled_turns: int
    ) -> None:
        self.saved.append((conversation_id, name, description, titled_turns))
        meta = self.metas[conversation_id]
        if name:
            meta.name = name
        meta.description, meta.titled_turns = description, titled_turns

    async def save_summary(
        self, conversation_id: str, summary: str, summarized_turns: int, tier: str
    ) -> None:
        self.summaries.append((conversation_id, summary, summarized_turns, tier))
        meta = self.metas[conversation_id]
        meta.summary, meta.summary_tier = summary, tier  # type: ignore[assignment]
        meta.summarized_turns = max(meta.summarized_turns, summarized_turns)

    async def turns(self, conversation_id: str, limit: int) -> list[Turn]:
        return self.turn_lists[conversation_id][:limit]


def turns(max_tier: Literal["public", "private", "confidential"] | None = "private") -> list[Turn]:
    return [
        Turn(seq=1, role="user", content="What did I discuss in my system design prep?"),
        Turn(
            seq=2,
            role="assistant",
            content="Rate limiting and caching [1].",
            max_tier=max_tier,
        ),
    ]


def titler(
    index: MemoryIndex,
    *,
    cloud: list[Any],
    local: list[Any],
    learner: Any = None,
    every_turns: int = 6,
) -> tuple[ConversationDigester, Factory]:
    inv = inventory(
        spec("cloud", max_tier="private"),
        spec("local", max_tier="confidential", tags=("local",)),
    )
    factory = Factory({"cloud": cloud, "local": local})
    digester = ConversationDigester(
        index, orchestrator(inv, factory), learner=learner, every_turns=every_turns
    )
    return digester, factory


async def test_private_chat_is_retitled_and_renamed_uniquely() -> None:
    index = MemoryIndex()
    index.add("c1", turns())
    index.add("c2", turns(), name="system-design-interview-prep")
    t, factory = titler(index, cloud=[SUGGESTION], local=["unused"])
    result = await t.refresh("c1")
    assert result.status == "updated" and result.renamed
    assert result.name == "system-design-interview-prep-2"
    assert result.description == "Rate limiting and caching prep."
    assert factory.calls("cloud") == 1 and factory.calls("local") == 0
    assert index.saved[-1][3] == 2  # titled_turns recorded


async def test_confidential_or_intake_chats_use_only_the_local_model() -> None:
    index = MemoryIndex()
    index.add("conf", turns("confidential"))
    index.add("intake", turns(None), mode="intake")
    t, factory = titler(index, cloud=["unused"], local=[SUGGESTION, SUGGESTION])
    assert (await t.refresh("conf")).status == "updated"
    assert (await t.refresh("intake")).status == "updated"
    assert factory.calls("cloud") == 0 and factory.calls("local") == 2


async def test_owner_renamed_conversation_keeps_its_name() -> None:
    index = MemoryIndex()
    index.add("c1", turns(), name="my-own-name", name_locked=True)
    t, _ = titler(index, cloud=[SUGGESTION], local=["unused"])
    result = await t.refresh("c1")
    assert result.name == "my-own-name" and not result.renamed
    assert index.metas["c1"].description == "Rate limiting and caching prep."


async def test_skips_short_unchanged_and_modelless() -> None:
    index = MemoryIndex()
    index.add("short", turns()[:1])
    index.add("same", turns(), titled_turns=2, summarized_turns=2)
    index.add("conf", turns("confidential"))
    inv = inventory(spec("cloud", max_tier="private"))
    t = ConversationDigester(index, orchestrator(inv, Factory({"cloud": [SUGGESTION]})))
    assert (await t.refresh("short")).reason == "too_short"
    assert (await t.refresh("same")).reason == "up_to_date"
    assert (await t.refresh("conf")).reason == "no_model"  # no local model: nothing changes
    assert index.saved == []


async def test_unknown_or_public_conversation_is_not_found() -> None:
    index = MemoryIndex()
    index.add("pub", turns(), channel="public")
    t, _ = titler(index, cloud=[SUGGESTION], local=["unused"])
    for cid in ("nope", "pub"):
        with pytest.raises(AppError):
            await t.refresh(cid)


async def test_slow_model_is_scheduled_and_finishes_later() -> None:
    index = MemoryIndex()
    index.add("c1", turns())
    t, _ = titler(index, cloud=[SUGGESTION], local=["unused"])
    gate = asyncio.Event()
    real = t.refresh

    async def slow(cid: str) -> Any:
        await gate.wait()
        return await real(cid)

    t.refresh = slow  # type: ignore[method-assign, assignment]
    assert (await t.refresh_within("c1", 0.01)).status == "scheduled"
    gate.set()
    for _ in range(50):
        if index.saved:
            break
        await asyncio.sleep(0.01)
    assert index.saved and index.metas["c1"].name == "system-design-interview-prep"


def test_tier_rules() -> None:
    meta = ConversationMeta(
        id="x", channel="owner", created_at=NOW, updated_at=NOW, message_count=2
    )
    assert conversation_tier(meta, turns("public")) is Tier.PRIVATE
    assert conversation_tier(meta, turns("confidential")) is Tier.CONFIDENTIAL
    intake = meta.model_copy(update={"mode": "intake"})
    assert conversation_tier(intake, turns()) is Tier.CONFIDENTIAL


async def test_owner_rename_locks_the_name() -> None:
    from app.services.owner_chat import OwnerChatService, TitleUpdate

    index = MemoryIndex()
    index.add("c1", turns())
    service = OwnerChatService(None, None, index)  # type: ignore[arg-type]
    await service.update_title("c1", TitleUpdate(name="raft-notes"))
    assert index.metas["c1"].name_locked is True
    await service.update_title("c1", TitleUpdate(description="Only the description"))
    assert index.metas["c1"].name_locked is True


def test_transcript_keeps_every_owner_message_and_real_outcomes() -> None:
    from app.services.digest import transcript

    chat = []
    for i in range(1, 26):
        chat.append(Turn(seq=2 * i - 1, role="user", content=f"question {i} about magnus eval"))
        route = "intake:error" if i == 25 else "kb_search"
        chat.append(Turn(seq=2 * i, role="assistant", content=f"reply {i}", route=route))
    text = transcript(chat)
    assert "1. question 1 about magnus eval" in text  # the start is never lost
    assert "question 25" in text and "(… 5 more messages …)" in text
    assert "error: reply 25" in text and "answered: reply 24" in text
    not_found = [Turn(seq=2, role="assistant", content="I don't have that in the vault.")]
    assert "not found:" in transcript(not_found)


class Learner:
    def __init__(self, fail: bool = False) -> None:
        self.calls: list[tuple[list[MemoryCandidate], str, str, Tier]] = []
        self.fail = fail

    async def learn(
        self,
        candidates: Sequence[MemoryCandidate],
        *,
        owner_text: str,
        conversation_id: str,
        tier: Tier,
    ) -> int:
        if self.fail:
            raise RuntimeError("store down")
        self.calls.append((list(candidates), owner_text, conversation_id, tier))
        return len(candidates)

    async def pending_count(self, conversation_id: str) -> int:
        if self.fail:
            raise RuntimeError("store down")
        return sum(len(c[0]) for c in self.calls if c[2] == conversation_id)


def long_chat(n: int, *, error_at: int | None = None) -> list[Turn]:
    chat: list[Turn] = []
    for i in range(1, n + 1):
        chat.append(
            Turn(seq=2 * i - 1, role="user", content=f"owner question {i} on raft consensus notes")
        )
        route = "intake:error" if i == error_at else "kb_search"
        chat.append(Turn(seq=2 * i, role="assistant", content=f"assistant reply {i}", route=route))
    return chat


def digest_json(summary: str, memories: list[dict[str, Any]] | None = None) -> str:
    return json.dumps(
        {
            "title": "Raft consensus notes",
            "description": "Questions about raft.",
            "summary": summary,
            "memories": memories or [],
        }
    )


async def test_exit_refreshes_title_summary_and_memories_in_one_call() -> None:
    index = MemoryIndex()
    index.add("c1", long_chat(2))
    learner = Learner()
    candidate = {"kind": "preference", "text": "Concise bullets", "confidence": 0.9}
    t, factory = titler(
        index, cloud=[digest_json("Raft questions.", [candidate])], local=[], learner=learner
    )
    result = await t.refresh("c1")
    assert factory.calls("cloud") == 1  # title, summary and candidates from one call
    assert result.summary_updated and result.new_memories == 1
    assert result.name == "raft-consensus-notes"
    assert index.summaries == [("c1", "Raft questions.", 4, "private")]
    candidates, owner_text, cid, tier = learner.calls[0]
    assert [c.text for c in candidates] == ["Concise bullets"] and cid == "c1"
    assert "owner question 1" in owner_text and "assistant reply" not in owner_text
    assert tier is Tier.PRIVATE


async def test_periodic_summary_after_six_new_turns_in_the_background() -> None:
    index = MemoryIndex()
    index.add("c1", long_chat(2))  # 4 turns: not due yet
    t, factory = titler(index, cloud=[digest_json("Raft so far.")], local=[])
    t.after_turn("c1", 4)
    await t.drain(1)
    assert factory.calls("cloud") == 0 and index.summaries == []
    index.add("c1", long_chat(3))  # 6 turns
    t.after_turn("c1", 6)
    t.after_turn("c1", 6)  # already running for this conversation: skipped
    await t.drain(1)
    assert factory.calls("cloud") == 1
    assert index.summaries == [("c1", "Raft so far.", 6, "private")]
    assert index.saved == []  # the title waits for the exit
    assert index.metas["c1"].summarized_turns == 6


async def test_periodic_summary_rolls_forward_from_the_previous_one() -> None:
    index = MemoryIndex()
    index.add("c1", long_chat(6), summary="Earlier: raft basics.", summarized_turns=6)
    t, factory = titler(index, cloud=[digest_json("Raft basics, then logs.")], local=[])
    t.after_turn("c1", 12)
    await t.drain(1)
    prompt = str(factory.models["cloud"].seen[0][-1].content)
    assert "Summary so far: Earlier: raft basics." in prompt
    assert "owner question 4" in prompt and "owner question 3 " not in prompt  # only new turns
    assert index.summaries[-1][1:3] == ("Raft basics, then logs.", 12)


async def test_periodic_waits_for_the_turn_to_be_stored() -> None:
    index = MemoryIndex()
    index.add("c1", long_chat(3))
    stored = index.turn_lists["c1"]
    index.turn_lists["c1"] = stored[:4]  # the last two turns still on their way to MongoDB
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        index.turn_lists["c1"] = stored

    t, _ = titler(index, cloud=[digest_json("Six turns.")], local=[])
    t.sleep = sleep
    t.after_turn("c1", 6)
    await t.drain(1)
    assert sleeps and index.summaries[-1][2] == 6


async def test_intake_summary_uses_only_the_local_model_or_nothing() -> None:
    index = MemoryIndex()
    index.add("i1", long_chat(3), mode="intake")
    t, factory = titler(index, cloud=["unused"], local=[digest_json("Intake chat.")])
    t.after_turn("i1", 6)
    await t.drain(1)
    assert factory.calls("cloud") == 0 and factory.calls("local") == 1
    assert index.summaries[-1][3] == "confidential"
    cloud_only = MemoryIndex()
    cloud_only.add("i1", long_chat(3), mode="intake")
    inv = inventory(spec("cloud", max_tier="private"))
    t2 = ConversationDigester(cloud_only, orchestrator(inv, Factory({"cloud": ["unused"]})))
    t2.after_turn("i1", 6)
    await t2.drain(1)
    assert cloud_only.summaries == [] and cloud_only.metas["i1"].summarized_turns == 0


async def test_summary_never_states_a_failed_action_as_done() -> None:
    index = MemoryIndex()
    index.add("c1", long_chat(3, error_at=3))
    # A small model glosses over the failure.
    t, factory = titler(index, cloud=[digest_json("Saved the Acme interview record.")], local=[])
    await t.refresh("c1")
    prompt = str(factory.models["cloud"].seen[0][-1].content)
    assert "3. error: assistant reply 3" in prompt  # the model is told the real outcome
    summary = index.summaries[-1][1]
    assert "1 attempted action failed" in summary
    honest = truthful_summary("Tried to save the record; it failed.", long_chat(3, error_at=3))
    assert honest == "Tried to save the record; it failed."  # already says so: no note
    assert len(truthful_summary("x" * 5000, long_chat(2))) == 1200


async def test_learner_failure_keeps_title_and_summary() -> None:
    index = MemoryIndex()
    index.add("c1", long_chat(2))
    candidate = {"kind": "alias", "text": "raft = Raft paper", "confidence": 0.7}
    t, _ = titler(
        index,
        cloud=[digest_json("Raft.", [candidate])],
        local=[],
        learner=Learner(fail=True),
    )
    result = await t.refresh("c1")
    assert result.status == "updated" and result.new_memories == 0
    assert index.summaries and index.saved


async def test_digest_logs_carry_no_conversation_text() -> None:
    from structlog.testing import capture_logs

    index = MemoryIndex()
    index.add("c1", long_chat(2))
    t, _ = titler(index, cloud=[digest_json("Secret raft summary.")], local=[])
    with capture_logs() as logs:
        await t.refresh("c1")
    text = json.dumps(logs, default=str)
    assert "Secret raft summary" not in text and "owner question" not in text
    assert any(e["event"] == "conversation digested" for e in logs)


async def test_a_title_the_owner_never_spoke_of_keeps_the_old_name() -> None:
    index = MemoryIndex()
    index.add("c1", long_chat(2), name="raft-questions")
    copied = json.dumps(
        {
            "title": "System design interview prep",  # the small model copied an example
            "description": "Questions about raft.",
            "summary": "Raft.",
            "memories": [],
        }
    )
    t, _ = titler(index, cloud=[copied], local=[])
    result = await t.refresh("c1")
    assert result.name == "raft-questions" and not result.renamed
    assert result.description == "Questions about raft."


async def test_exit_reports_pending_memories_from_the_whole_conversation() -> None:
    index = MemoryIndex()
    index.add("c1", long_chat(3))
    learner = Learner()
    candidate = {"kind": "preference", "text": "Concise bullets", "confidence": 0.9}
    t, _ = titler(
        index,
        cloud=[digest_json("Raft.", [candidate]), digest_json("Raft again.")],
        local=[],
        learner=learner,
    )
    t.after_turn("c1", 6)  # the periodic run learns one
    await t.drain(1)
    index.add("c1", long_chat(4), summary="Raft.", summarized_turns=6)
    result = await t.refresh("c1")  # learns nothing new itself
    assert result.new_memories == 1
