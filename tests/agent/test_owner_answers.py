"""answer-studio T3: the owner chat synthesises, uses document cards for overviews and keeps
follow-ups on the documents the previous answer cited."""

from __future__ import annotations

from collections.abc import Sequence

from app.agents.graphs.owner import (
    OwnerSettings,
    is_broad,
    is_employer_question,
    is_vague_follow_up,
)
from app.agents.prompts.owner import GENERATE_KB_V3, PROMPT_VERSION
from app.kb.retrieval.retriever import Hit
from llm_orchestrator import Tier
from tests.agent.harness import CONF_TEXT, PRIV_TEXT, hit, make_agent, route_json


def prompt_text(messages: Sequence[object]) -> str:
    return "\n".join(str(getattr(m, "content", "")) for m in messages)


def card(doc: str, tier: Tier, text: str) -> Hit:
    return hit(doc, tier, text, ordinal=1_000_000).model_copy(update={"card": True})


def test_prompt_asks_for_synthesis_not_quotes() -> None:
    assert PROMPT_VERSION == "owner-v4"
    assert "Never copy the context back" in GENERATE_KB_V3
    assert "document summary" in GENERATE_KB_V3
    assert "never list a project twice" in GENERATE_KB_V3
    assert "never list personal" in GENERATE_KB_V3


def test_overview_questions_are_broad() -> None:
    assert is_broad("Give me the summary of projects I worked on")
    assert is_broad("list all my achievements")
    assert is_broad("What projects have I done?")
    assert not is_broad("what did I learn about raft?")
    assert is_vague_follow_up("Give me the summary") and is_vague_follow_up("tell me more")
    assert not is_vague_follow_up("summary of the rate limiter")


async def test_overview_uses_cards_first_and_a_larger_budget() -> None:
    q = "Give me the summary of all my projects"
    hits = [
        hit("p1", Tier.PRIVATE, PRIV_TEXT),
        card("p1", Tier.PRIVATE, "Rate limiter: token bucket in Go, p99 12 ms."),
        card("p2", Tier.PRIVATE, "Search service: hybrid retrieval, 40% better recall."),
    ]
    agent = make_agent(
        router=[route_json("kb_search", q)],
        cloud=["Two projects: a rate limiter [1] and a search service [2]."],
        hits=hits,
        settings=OwnerSettings(top_k=1, context_tokens=10),
    )
    state, _ = await agent.run(q)
    assert state["broad"] is True
    assert [h.card for h in state["hits"]][:2] == [True, True]
    assert any(c["cards_only"] for c in agent.searcher.calls)
    seen = prompt_text(agent.factory.models["cloud"].seen[0])
    # Both cards and the chunk fit: the broad budget replaces the 10-token one.
    assert "Rate limiter: token bucket" in seen and "Search service" in seen and PRIV_TEXT in seen
    assert "(notes, document summary)" in seen
    assert [s.document_id for s in state["sources"]] == ["p1", "p2"]


async def test_vague_follow_up_stays_on_the_cited_documents() -> None:
    first = "What did I learn at the meetup?"
    hits = [
        hit("priv", Tier.PRIVATE, PRIV_TEXT),
        card("priv", Tier.PRIVATE, "Meetup notes."),
        card("other", Tier.PRIVATE, "An unrelated document."),
    ]
    agent = make_agent(
        router=[route_json("kb_search", first), route_json("kb_search", "Give me the summary")],
        cloud=["Raft leader election [1].", "In short: raft [1]."],
        hits=hits,
    )
    state, _ = await agent.run(first)
    assert [s.document_id for s in state["sources"]] == ["priv"]
    nxt, _ = await agent.run("Give me the summary", conversation_id=state["conversation_id"])
    assert nxt["reused"] is True
    assert {h.document_id for h in nxt["hits"]} == {"priv"}
    assert all(c["document_ids"] == ["priv"] for c in agent.searcher.calls[-2:])
    assert [s.document_id for s in nxt["sources"]] == ["priv"]


async def test_follow_up_to_a_confidential_answer_stays_local_and_sees_it() -> None:
    q = "what was in my offer?"
    agent = make_agent(
        router=[route_json("kb_search", q), route_json("kb_search", "and the meetup?")],
        cloud=["unused"],
        local=["Base pay [1].", "Raft [1]."],
        hits=[hit("conf", Tier.CONFIDENTIAL, CONF_TEXT)],
    )
    state, _ = await agent.run(q)
    agent.searcher.hits = [hit("priv", Tier.PRIVATE, PRIV_TEXT)]
    nxt, _ = await agent.run("and the meetup?", conversation_id=state["conversation_id"])
    # Private hits alone would go to the cloud model and drop the earlier answer from history.
    assert nxt["model_id"] == "local" and nxt["tier"] is Tier.CONFIDENTIAL
    assert "Base pay" in prompt_text(agent.factory.models["local"].seen[-1])
    assert agent.factory.calls("cloud") == 0


def test_employer_questions_are_recognised() -> None:
    assert is_employer_question("summary of the projects I worked on at my current company")
    assert is_employer_question("what did I do at work last quarter?")
    assert is_employer_question("projects at my previous employer")
    assert not is_employer_question("summary of all my projects")


async def test_employer_question_leaves_personal_repositories_out() -> None:
    q = "Give me the summary of the projects I worked on at my current company"
    work = hit("w1", Tier.CONFIDENTIAL, "Built the evaluation service.").model_copy(
        update={"category": "company-projects"}
    )
    repo = hit("r1", Tier.PUBLIC, "A calculator in Python.").model_copy(
        update={"category": "project"}
    )
    agent = make_agent(
        router=[route_json("kb_search", q)],
        cloud=["unused"],
        local=["The evaluation service [1]."],
        hits=[repo, work],
    )
    state, _ = await agent.run(q)
    assert [h.document_id for h in state["hits"]] == ["w1"]
    seen = prompt_text(agent.factory.models["local"].seen[0])
    assert "(company-projects)" in seen and "calculator" not in seen
    # A general question still sees every project.
    general = make_agent(
        router=[route_json("kb_search", "summary of all my projects")],
        cloud=["unused"],
        local=["Both [1][2]."],
        hits=[repo, work],
    )
    state, _ = await general.run("summary of all my projects")
    assert {h.document_id for h in state["hits"]} == {"w1", "r1"}
