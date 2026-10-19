"""Intake `remember`, `forget` and `show_memory`: confirmed like every write, active at once,
archived on forget, audited as the intake agent."""

from __future__ import annotations

from collections.abc import Callable

from app.agents.graphs.intake import rule_action
from app.services.memory import MemoryIn
from tests.intake.harness import Twin, extract

MakeTwin = Callable[..., Twin]


def test_memory_keyword_rules() -> None:
    assert rule_action("remember that I prefer concise bullets", None) == "remember"
    assert rule_action("Please remember: magnus-eval means the Magnus Eval project", None) == (
        "remember"
    )
    assert rule_action("remember when I interviewed at Acme?", "answer") == "answer"
    assert rule_action("forget the British spelling rule", None) == "forget"
    assert rule_action("what do you remember about me?", None) == "show_memory"
    assert rule_action("show my preferences", None) == "show_memory"


async def test_remember_is_confirmed_then_active(make_twin: MakeTwin) -> None:
    # The model leaves the text and kind out: both come from the owner's own words.
    twin = make_twin([extract("remember")])
    first = await twin.say("remember that I always want dates shown as 12 Oct 2026")
    assert first.draft is not None and "Remember this" in first.draft["summary"]
    assert first.draft["payload"] == {
        "text": "I always want dates shown as 12 Oct 2026",
        "kind": "instruction",
    }
    assert await twin.memory.listing() == []  # nothing before the yes
    done = await twin.say("yes", first.conversation_id)
    assert done.done["status"] == "done" and "Remembered" in done.text
    (item,) = await twin.memory.active()
    assert item.kind == "instruction" and item.status == "active"
    assert any(
        e["action"] == "memory.add" and e["actor"].startswith("agent:intake")
        for e in twin.audit.entries
    )


async def test_forget_archives_the_matching_memory(make_twin: MakeTwin) -> None:
    twin = make_twin([extract("forget", memory="british spelling")])
    item, _ = await twin.memory.add(MemoryIn(text="Use British spelling"), "key:t")
    first = await twin.say("forget the british spelling memory")
    assert first.draft is not None and f"Use British spelling ({item.id})" in first.draft["summary"]
    await twin.say("yes", first.conversation_id)
    assert await twin.memory.active() == []
    assert [m.id for m in await twin.memory.listing("archived")] == [item.id]


async def test_show_memory_lists_active_and_pending(make_twin: MakeTwin) -> None:
    twin = make_twin([extract(None, reply="hmm")])
    await twin.memory.add(MemoryIn(text="Use British spelling", kind="instruction"), "key:t")
    reply = await twin.say("what do you remember?")
    assert reply.done["status"] == "done" and reply.draft is None
    assert "Active (1):" in reply.text and "Use British spelling" in reply.text
