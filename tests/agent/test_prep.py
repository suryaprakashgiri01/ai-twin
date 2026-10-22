"""answer-studio T5: the prep agent answers as the owner, within limits, with internal details
removed before anything leaves."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, cast

import pytest
from fakeredis import FakeAsyncRedis

from app.agents.generation import ChatGenerator
from app.agents.graphs.prep import (
    PrepDeps,
    PrepState,
    build_prep_graph,
    detect_type,
    fit_limit,
    initial_state,
    unsourced_numbers,
)
from app.agents.prep.filter import OutwardFilter
from app.kb.ingestion.guard import parse_denylist
from app.kb.retrieval.rerank import FusedOrderReranker
from app.kb.retrieval.retriever import Hit
from app.services.conversations import ConversationService
from llm_orchestrator import Tier
from tests.agent.harness import CONF_TEXT, FakeSearcher, hit
from tests.fakes import MemoryConversationStore
from tests.llm_orchestrator.fakes import Factory, inventory, orchestrator, spec

DENYLIST = parse_denylist("rules:\n  - name: internal-services\n    terms: [bluebird-gateway]\n")
PROJECT = "Led the rate limiter rewrite: p99 latency fell from 480 ms to 120 ms."


@pytest.mark.parametrize(
    ("question", "kind"),
    [
        ("Tell us about the most challenging technical project you led", "behavioural"),
        (
            "If your AI inference latency increases significantly after new features, what's "
            "your step-by-step process to diagnose it?",
            "scenario",
        ),
        ("Tell me about yourself", "pitch"),
        ("Why do you want to join us?", "application"),
        ("Walk me through the architecture of your search project", "project"),
    ],
)
def test_question_types(question: str, kind: str) -> None:
    assert detect_type(question) == kind
    assert detect_type("Describe your strengths", limited=True) == "application"


def test_limits_numbers_and_filter() -> None:
    long = "One sentence here. " * 50
    assert len(fit_limit(long, 20, None).split()) <= 20
    assert len(fit_limit(long, None, 120)) <= 120
    assert unsourced_numbers("1. Cut p99 to 120 ms and 35% cost, 3 steps", PROJECT) == ["35%"]
    text = "I fixed FAKEPROJ-12 in bluebird-gateway via user:session:id on orders.events.v2."
    out = OutwardFilter(denylist=lambda: DENYLIST).apply(text)
    assert "bluebird" not in out.text and "FAKEPROJ" not in out.text
    assert "user:session" not in out.text and "orders.events" not in out.text
    assert len(out.removed) == 4


def agent(
    hits: Sequence[Hit], cloud: list[Any], local: list[Any] | None = None
) -> tuple[Any, Factory, ConversationService]:
    specs = [spec("cloud", roles=["generator"], max_tier="private")]
    scripts: dict[str, list[Any]] = {"cloud": cloud}
    if local is not None:
        specs.append(spec("local", roles=["generator"], max_tier="confidential", tags=["local"]))
        scripts["local"] = local
    factory = Factory(scripts)
    orch = orchestrator(inventory(*specs), factory, max_attempts=1)
    conversations = ConversationService(
        FakeAsyncRedis(decode_responses=True),
        MemoryConversationStore(),
        visitor_salt="salt",
        retry_base_delay=0,
    )
    deps = PrepDeps(
        orchestrator=orch,
        retriever=FakeSearcher(hits=list(hits)),
        reranker=FusedOrderReranker(),
        conversations=conversations,
        generator=ChatGenerator(orch),
        filter=OutwardFilter(denylist=lambda: DENYLIST),
    )
    return build_prep_graph(deps), factory, conversations


async def run(graph: Any, conversations: ConversationService, **kw: Any) -> PrepState:
    cid = kw.pop("conversation_id", None) or (await conversations.start("owner")).id
    state = initial_state(conversation_id=cid, history=await conversations.history(cid), **kw)
    final = await graph.ainvoke(state)
    await conversations.drain()
    return cast(PrepState, final)


async def test_behavioural_answer_is_first_person_filtered_and_checked() -> None:
    reply = (
        "I led the rate limiter rewrite in bluebird-gateway: p99 fell from 480 ms to 120 ms "
        "and cost dropped [add metric]. We saved 35% on hosts."
    )
    graph, factory, conversations = agent([hit("p1", Tier.PRIVATE, PROJECT)], cloud=[reply])
    state = await run(
        graph, conversations, question="Tell us about the most challenging project you led"
    )
    assert state["kind"] == "behavioural" and state["model_id"] == "cloud"
    assert "bluebird" not in state["answer"] and "an internal system" in state["answer"]
    assert state["removed"] == ["1 internal names → “an internal system”"]
    assert any("[add metric]" in c for c in state["checks"])
    assert any("35%" in c for c in state["checks"])
    system = str(factory.models["cloud"].seen[0][0].content)
    assert 'first person ("I"' in system and "STAR" in system


async def test_confidential_sources_use_the_local_model_and_limits_hold() -> None:
    graph, factory, conversations = agent(
        [hit("conf", Tier.CONFIDENTIAL, CONF_TEXT)],
        cloud=["unused"],
        local=["word " * 80, "I want to grow here."],
    )
    state = await run(graph, conversations, question="Why this company?", words=30)
    assert state["kind"] == "application" and state["model_id"] == "local"
    assert factory.calls("cloud") == 0
    assert state["answer"] == "I want to grow here."  # the over-long draft was shortened
    assert any("limit 30 words" in c for c in state["checks"])


async def test_revise_rewrites_the_previous_answer() -> None:
    graph, factory, conversations = agent(
        [hit("p1", Tier.PRIVATE, PROJECT)], cloud=["Long answer.", "Short answer."]
    )
    first = await run(graph, conversations, question="Tell me about yourself")
    again = await run(
        graph,
        conversations,
        conversation_id=first["conversation_id"],
        question="/shorter",
        revise="shorter",
    )
    assert again["kind"] == "pitch" and again["answer"] == "Short answer."
    last = factory.models["cloud"].seen[-1]
    assert "Long answer." in "\n".join(str(m.content) for m in last)
    assert "shorter" in str(last[-1].content)
