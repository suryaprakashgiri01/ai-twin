"""Owner-agent T4: routing, tools, tier-aware generation and withholding (AC3-AC6)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from typing import Any

from langchain_core.messages import HumanMessage
from structlog.testing import capture_logs

from app.agents.prompts.owner import NOT_IN_VAULT
from app.agents.routing import heuristic_route
from app.services.profile import ProfileIn
from llm_orchestrator import Tier
from tests.agent.harness import CONF_TEXT, PRIV_TEXT, TODAY, hit, make_agent, route_json

SEPT = {"kind": "interview", "date_from": "2026-09-01", "date_to": "2026-09-30"}


def prompt_text(messages: Sequence[object]) -> str:
    return "\n".join(str(getattr(m, "content", "")) for m in messages)


async def test_records_question_uses_the_tool_with_the_exact_count() -> None:
    q = "how many interviews in September?"
    agent = make_agent(router=[route_json("records", q, SEPT)], cloud=["unused"])
    state, tokens = await agent.run(q)
    assert state["route"] == "records"
    assert state["answer"].startswith("You have 3 interviews from 2026-09-01 to 2026-09-30.")
    assert "".join(tokens) == state["answer"]
    assert agent.searcher.calls == []  # the KB is not searched
    assert agent.factory.calls("cloud") == 0  # no model writes the count
    assert agent.records.calls[0].kind == "interview"
    assert state["tier"] is Tier.PRIVATE


async def test_confidential_context_is_withheld_when_only_cloud_models() -> None:
    q = "what was in my offer and what did I learn at the meetup?"
    hits = [hit("conf", Tier.CONFIDENTIAL, CONF_TEXT), hit("priv", Tier.PRIVATE, PRIV_TEXT)]
    agent = make_agent(router=[route_json("kb_search", q)], cloud=["From [1]: raft."], hits=hits)
    state, _ = await agent.run(q)
    assert state["tier"] is Tier.PRIVATE
    assert state["model_id"] == "cloud"
    assert state["withheld"] == 1
    assert [s.document_id for s in state["sources"]] == ["priv"]
    seen = prompt_text(agent.factory.models["cloud"].seen[0])
    assert PRIV_TEXT in seen and CONF_TEXT not in seen
    assert state["answer"].startswith("From [1]: raft.")
    assert "1 confidential source was withheld" in state["answer"]


async def test_confidential_context_uses_a_local_model() -> None:
    q = "what was in my offer?"
    hits = [hit("conf", Tier.CONFIDENTIAL, CONF_TEXT), hit("priv", Tier.PRIVATE, PRIV_TEXT)]
    agent = make_agent(
        router=[route_json("kb_search", q)], cloud=["unused"], local=["Base pay [1]."], hits=hits
    )
    state, _ = await agent.run(q)
    assert state["tier"] is Tier.CONFIDENTIAL
    assert state["model_id"] == "local"
    assert state["withheld"] == 0
    assert agent.factory.calls("cloud") == 0
    assert {s.document_id for s in state["sources"]} == {"conf", "priv"}
    assert CONF_TEXT in prompt_text(agent.factory.models["local"].seen[0])


async def test_pinned_cloud_model_withholds_instead_of_failing() -> None:
    q = "what was in my offer?"
    hits = [hit("conf", Tier.CONFIDENTIAL, CONF_TEXT), hit("priv", Tier.PRIVATE, PRIV_TEXT)]
    agent = make_agent(
        router=[route_json("kb_search", q)], cloud=["ok [1]"], local=["unused"], hits=hits
    )
    state, _ = await agent.run(q, model="cloud")
    assert state["model_id"] == "cloud" and state["withheld"] == 1
    assert agent.factory.calls("local") == 0


async def test_only_confidential_hits_and_no_local_model_is_not_in_the_vault() -> None:
    q = "what was in my offer?"
    agent = make_agent(
        router=[route_json("kb_search", q)],
        cloud=["unused"],
        hits=[hit("conf", Tier.CONFIDENTIAL, CONF_TEXT)],
    )
    state, _ = await agent.run(q)
    assert state["answer"].startswith(NOT_IN_VAULT)
    assert state["withheld"] == 1 and state["sources"] == []
    assert agent.factory.calls("cloud") == 0


async def test_empty_context_answers_not_in_the_vault() -> None:
    q = "what is my favourite colour?"
    agent = make_agent(router=[route_json("kb_search", q)], cloud=["Blue, obviously."])
    state, tokens = await agent.run(q)
    assert state["answer"] == NOT_IN_VAULT and tokens == [NOT_IN_VAULT]
    assert agent.factory.calls("cloud") == 0
    assert state["sources"] == [] and state["incomplete"] is False


async def test_follow_up_is_rewritten_from_history_and_logged_without_text() -> None:
    first = "which project used raft?"
    rewritten = "which project did I build before the raft project?"
    agent = make_agent(
        router=[route_json("kb_search", first), route_json("kb_search", rewritten)],
        cloud=["The rate limiter [1].", "The cache [1]."],
        hits=[hit("priv", Tier.PRIVATE, PRIV_TEXT)],
    )
    state, _ = await agent.run(first)
    with capture_logs() as logs:
        follow, _ = await agent.run(
            "and the one before that?", conversation_id=state["conversation_id"]
        )
    assert follow["query"] == rewritten
    assert agent.searcher.calls[-1]["query"] == rewritten
    router_prompt = prompt_text(agent.factory.models["router"].seen[-1])
    assert first in router_prompt and "The rate limiter [1]." in router_prompt
    route_line = next(e for e in logs if e.get("step") == "route")
    assert route_line["rewritten"] is True and route_line["query_chars"] == len(rewritten)
    assert rewritten not in str(logs) and "the one before that" not in str(logs)


async def test_router_failure_falls_back_to_keywords() -> None:
    agent = make_agent(router=[ValueError("router down")], cloud=["unused"])
    state, _ = await agent.run("how many applications in September?")
    assert state["route"] == "records" and state["route_source"] == "heuristic"
    assert state["answer"].startswith("You have 2 applications from 2026-09-01")


async def test_kb_miss_on_a_counting_question_tries_records_once() -> None:
    q = "how many interviews in September?"
    agent = make_agent(router=[route_json("kb_search", q)], cloud=["unused"])
    state, _ = await agent.run(q)
    assert len(agent.searcher.calls) == 1
    assert state["route"] == "records"
    assert state["answer"].startswith("You have 3 interviews")


async def test_profile_route_generates_from_the_profile() -> None:
    q = "what is my notice period?"
    agent = make_agent(router=[route_json("profile", q)], cloud=["30 days."])
    await agent.profile.put(ProfileIn(availability="open", notice_period="30 days"), None)
    state, _ = await agent.run(q)
    assert state["answer"] == "30 days." and state["tier"] is Tier.PRIVATE
    assert "Notice period: 30 days" in prompt_text(agent.factory.models["cloud"].seen[0])


async def test_records_and_profile_need_private() -> None:
    agent = make_agent(
        router=[route_json("records", "how many interviews?", {"kind": "interview"})]
    )
    state, _ = await agent.run("how many interviews?", max_tier=Tier.PUBLIC)
    assert "max_tier private" in state["answer"] and agent.records.calls == []


async def test_chat_route_needs_no_context() -> None:
    agent = make_agent(router=[route_json("chat", "hi")], cloud=["Hello!"])
    state, _ = await agent.run("hi")
    assert state["answer"] == "Hello!" and state["sources"] == []
    assert agent.searcher.calls == []


async def test_finalize_persists_both_turns_with_metadata() -> None:
    q = "what did I learn at the meetup?"
    agent = make_agent(
        router=[route_json("kb_search", q)],
        cloud=["Raft [1]."],
        hits=[hit("priv", Tier.PRIVATE, PRIV_TEXT)],
    )
    state, _ = await agent.run(q)
    user, assistant = await agent.history(state["conversation_id"])
    assert (user.role, user.content) == ("user", q)
    assert assistant.route == "kb_search" and assistant.max_tier == "private"
    assert assistant.model == "cloud" and assistant.citations == ["priv"]
    assert assistant.incomplete is False and assistant.withheld == 0
    assert assistant.tokens == 5 and assistant.latency_ms is not None
    assert state["seq"] == 2
    stored = agent.store.turns[state["conversation_id"]]
    assert stored[2].citations == ["priv"]


async def test_confidential_history_is_hidden_from_a_private_request() -> None:
    q = "what was in my offer?"
    agent = make_agent(
        router=[route_json("kb_search", q), route_json("chat", "thanks")],
        cloud=["unused", "You're welcome."],
        local=["Base pay [1]."],
        hits=[hit("conf", Tier.CONFIDENTIAL, CONF_TEXT)],
    )
    state, _ = await agent.run(q)
    assert state["tier"] is Tier.CONFIDENTIAL
    nxt, _ = await agent.run(
        "thanks", conversation_id=state["conversation_id"], max_tier=Tier.PRIVATE
    )
    assert nxt["model_id"] == "cloud"
    seen = prompt_text(agent.factory.models["cloud"].seen[-1])
    assert "Base pay" not in seen
    assert "Base pay" not in prompt_text(agent.factory.models["router"].seen[-1])


async def test_no_question_context_or_answer_in_logs() -> None:
    q = "QUESTION-MARKER what did I learn?"
    agent = make_agent(
        router=[route_json("kb_search", q)],
        cloud=["ANSWER-MARKER raft [1]."],
        hits=[hit("priv", Tier.PRIVATE, PRIV_TEXT)],
    )
    with capture_logs() as logs:
        await agent.run(q)
    dumped = str(logs)
    for marker in ("QUESTION-MARKER", "ANSWER-MARKER", PRIV_TEXT):
        assert marker not in dumped
    steps = [e["step"] for e in logs if e.get("event") == "owner step"]
    assert steps == ["route", "retrieve", "generate", "finalize"]


def test_heuristic_router_rules() -> None:
    def route(q: str, prev: str | None = None) -> Any:
        return heuristic_route(q, prev, TODAY)

    assert route("hello!").route == "chat"
    rec = route("how many interviews last month?")
    assert rec.records is not None
    assert (rec.records.date_from, rec.records.date_to) == (date(2026, 9, 1), date(2026, 9, 30))
    nov = route("how many applications in November?").records
    assert nov is not None and nov.date_from == date(2025, 11, 1)  # still ahead this year
    week = route("how many applications this week?").records
    assert week is not None and week.date_from == date(2026, 10, 12)
    assert route("which companies did I interview with?").records.group_by == "company"
    assert route("what is my notice period?").route == "profile"
    assert route("tell me about raft").route == "kb_search"
    follow = route("and in August?", "how many interviews in September?")
    assert follow.records is not None and follow.records.date_from == date(2026, 8, 1)
    assert route("and the one before that?", "which project used raft?").query == (
        "which project used raft? and the one before that?"
    )


def test_history_messages_alternate_roles() -> None:
    from app.agents.graphs.owner import history_messages
    from app.services.conversations import Turn

    messages = history_messages(
        [Turn(role="user", content="q" * 50), Turn(role="assistant", content="a")], max_chars=10
    )
    assert isinstance(messages[0], HumanMessage) and messages[0].content == "q" * 10
