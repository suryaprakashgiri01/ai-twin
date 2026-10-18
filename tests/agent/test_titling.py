"""Conversation title refresh: tier-aware model choice, name lock, uniqueness, skips."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any, Literal

import pytest

from app.exceptions import AppError
from app.services.conversations import Turn
from app.services.owner_chat import ConversationMeta, ConversationSummary
from app.services.titling import ConversationTitler, conversation_tier
from llm_orchestrator import Tier
from tests.llm_orchestrator.fakes import Factory, inventory, orchestrator, spec

NOW = datetime(2026, 10, 18, tzinfo=UTC)
SUGGESTION = json.dumps(
    {"title": "System design interview prep", "description": "Rate limiting and caching prep."}
)


class MemoryIndex:
    def __init__(self) -> None:
        self.metas: dict[str, ConversationMeta] = {}
        self.turn_lists: dict[str, list[Turn]] = {}
        self.saved: list[tuple[str, str | None, str, int]] = []

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
    index: MemoryIndex, *, cloud: list[Any], local: list[Any]
) -> tuple[ConversationTitler, Factory]:
    inv = inventory(
        spec("cloud", max_tier="private"),
        spec("local", max_tier="confidential", tags=("local",)),
    )
    factory = Factory({"cloud": cloud, "local": local})
    return ConversationTitler(index, orchestrator(inv, factory)), factory


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
    index.add("same", turns(), titled_turns=2)
    index.add("conf", turns("confidential"))
    inv = inventory(spec("cloud", max_tier="private"))
    t = ConversationTitler(index, orchestrator(inv, Factory({"cloud": [SUGGESTION]})))
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
