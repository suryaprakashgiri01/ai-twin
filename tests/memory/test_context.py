"""Owner context in prompts: active memories in order and budget, tier rules, the summary only
for long chats, intake prompts too, and never anything in the public agent's prompt."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app.agents.context import EARLIER_HEAD, PREFERENCES_HEAD, ContextBuilder, OwnerContext
from app.services.memory import MemoryIn, MemoryService
from llm_orchestrator import Tier
from tests.agent.harness import make_agent, route_json
from tests.intake.harness import Twin, extract
from tests.memory.fakes import FakeAudit, FakeMemoryStore
from tests.public.fakes import answer_of, make_public

PENDING = "Prefers haiku answers"
CONF = "Call the Globex offer project-orchid"
SUMMARY = "Discussed raft leader election and a failed record add."


async def memories() -> MemoryService:
    svc = MemoryService(FakeMemoryStore(), FakeAudit())
    await svc.add(MemoryIn(text="Concise bullets with sources first"), "key:t")
    await svc.add(MemoryIn(text="magnus-eval = Magnus Eval project", kind="alias"), "key:t")
    await svc.add(MemoryIn(text="Show dates as 12 Oct 2026", kind="instruction"), "key:t")
    await svc.add(MemoryIn(text=CONF, kind="alias", tier="confidential"), "key:t")
    item, _ = await svc.add(MemoryIn(text=PENDING), "key:t")
    await svc.update(item.id, svc_patch("pending"), "key:t")
    return svc


def svc_patch(status: Any) -> Any:
    from app.services.memory import MemoryPatch

    return MemoryPatch(status=status)


class Summaries:
    def __init__(self, count: int, tier: str = "private") -> None:
        self.count, self.tier = count, tier

    async def get(self, conversation_id: str) -> Any:
        from types import SimpleNamespace

        return SimpleNamespace(summary=SUMMARY, summary_tier=self.tier, message_count=self.count)


async def test_active_memories_in_order_pending_never_confidential_only_locally() -> None:
    context = await ContextBuilder(await memories(), None).load(None)
    private = context.preferences(Tier.PRIVATE)
    lines = private.splitlines()
    assert lines[0] == PREFERENCES_HEAD
    assert lines[1] == "- Rule: Show dates as 12 Oct 2026"  # instructions first
    assert lines[2].startswith("- Preference: Concise") and lines[3].startswith("- Alias:")
    assert PENDING not in private and CONF not in private
    assert CONF in context.preferences(Tier.CONFIDENTIAL)


async def test_memory_budget_and_item_cap() -> None:
    svc = MemoryService(FakeMemoryStore(), FakeAudit())
    for i in range(30):
        await svc.add(MemoryIn(text=f"Rule number {i} about formatting tables"), "key:t")
    context = await ContextBuilder(svc, None, memory_items=20, memory_tokens=400).load(None)
    block = context.preferences(Tier.PRIVATE)
    assert len(block.splitlines()) - 1 == 20 and len(block) <= 400 * 4
    tight = await ContextBuilder(svc, None, memory_tokens=40).load(None)
    assert len(tight.preferences(Tier.PRIVATE)) <= 160


async def test_summary_only_for_long_chats_within_budget_and_tier() -> None:
    long = await ContextBuilder(None, Summaries(12)).load("c1", 12)
    assert long.earlier(Tier.PRIVATE, 8) == f"{EARLIER_HEAD}\n{SUMMARY}"
    short = await ContextBuilder(None, Summaries(6)).load("c1", 6)
    assert short.earlier(Tier.PRIVATE, 8) == ""
    confidential = await ContextBuilder(None, Summaries(12, "confidential")).load("c1", 12)
    assert confidential.earlier(Tier.PRIVATE, 8) == ""
    assert SUMMARY in confidential.earlier(Tier.CONFIDENTIAL, 8)
    tiny = OwnerContext(summary="x" * 2000, turns=20, summary_tokens=10)
    assert len(tiny.earlier(Tier.PRIVATE, 8)) <= len(EARLIER_HEAD) + 1 + 40


async def test_owner_chat_prompt_carries_preferences_and_summary_for_long_chats() -> None:
    context = await ContextBuilder(await memories(), Summaries(12)).load("c1", 12)
    agent = make_agent(router=[route_json("chat", "hi")], cloud=["Hello."], local=["Local."])
    await agent.run("hi", max_tier=Tier.PRIVATE, context=context)
    system = str(agent.factory.models["cloud"].seen[0][0].content)
    assert "Show dates as 12 Oct 2026" in system and SUMMARY in system
    assert CONF not in system and PENDING not in system  # cloud call: private only

    local = make_agent(router=[route_json("chat", "hi")], cloud=["unused"], local=["Local."])
    await local.run("hi", min_tier=Tier.CONFIDENTIAL, context=context)
    assert CONF in str(local.factory.models["local"].seen[0][0].content)

    short = await ContextBuilder(await memories(), Summaries(4)).load("c1", 4)
    brief = make_agent(router=[route_json("chat", "hi")], cloud=["Hello."])
    await brief.run("hi", max_tier=Tier.PRIVATE, context=short)
    assert SUMMARY not in str(brief.factory.models["cloud"].seen[0][0].content)


async def test_intake_prompt_carries_preferences(make_twin: Callable[..., Twin]) -> None:
    twin = make_twin([extract(None, reply="Hi!")])
    twin.service._context = ContextBuilder(await memories(), twin.index)
    await twin.say("hello there")
    system = "\n".join(str(m.content) for m in twin.factory.models["local"].seen[0])
    assert "Show dates as 12 Oct 2026" in system and CONF in system  # intake is local
    assert PENDING not in system


async def test_public_agent_prompt_never_has_memories() -> None:
    svc = await memories()
    assert len(await svc.active()) == 4  # the vault has active memories
    public = make_public()
    events = await public.ask("What does Kavya work on?")
    assert answer_of(events)
    prompts = [str(m.content) for call in public.factory.models["gen"].seen for m in call]
    everything = "\n".join(prompts)
    for item in await svc.listing():
        assert item.text not in everything
    assert PREFERENCES_HEAD not in everything and EARLIER_HEAD not in everything
