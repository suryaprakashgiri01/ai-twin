"""answer-studio T9: the super agent plans, delegates to intake, ask and prep, and composes."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from fakeredis import FakeAsyncRedis

from app.agents.generation import ChatGenerator
from app.agents.graphs.owner import OwnerDeps, build_owner_graph
from app.agents.graphs.prep import PrepDeps, build_prep_graph
from app.agents.models import AUTO_CHOICE
from app.agents.prep.filter import OutwardFilter
from app.agents.tools.profile import ProfileTool
from app.kb.retrieval.rerank import FusedOrderReranker
from app.services.conversations import ConversationService
from app.services.super_agent import (
    StepResult,
    SuperAgent,
    SuperDeps,
    SuperRequest,
    compose,
    rule_plan,
)
from llm_orchestrator import Tier
from tests.agent.harness import (
    CONF_TEXT,
    PRIV_TEXT,
    TODAY,
    FakeSearcher,
    MemoryRecordsTool,
    hit,
    route_json,
)
from tests.fakes import MemoryConversationStore
from tests.ingestion.fakes import MemoryProfileStore
from tests.llm_orchestrator.fakes import Factory, inventory, orchestrator, spec


@dataclass
class FakeIntake:
    """Intake as the super agent sees it: a draft for each request, done on "yes"."""

    action: str = "add_record"
    told: list[str] = field(default_factory=list)

    async def prepare(self, request: Any, key_name: str) -> Any:
        return type(
            "Ready",
            (),
            {"conversation_id": request.conversation_id or "f" * 32, "message": request.message},
        )()

    async def events(self, ready: Any) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        self.told.append(ready.message)
        if ready.message == "yes":
            yield "token", {"text": "Added the Acme interview (today 15:00)."}
            yield "done", {"action": self.action, "pending": False, "status": "done"}
            return
        if self.action == "ask" and len(self.told) == 1:
            yield "token", {"text": "To add a record, I still need:\n- stage"}
            yield "done", {"action": "add_record", "pending": False, "status": "asked"}
            return
        if self.action == "ask":
            yield "token", {"text": "Added the Acme interview (screen)."}
            yield "done", {"action": "add_record", "pending": False, "status": "done"}
            return
        if self.action == "reworded" and ready.message.startswith("Add a new record"):
            yield "token", {"text": "I can add notes and records. What would you like to do?"}
            yield "done", {"action": None, "pending": False, "status": "chat"}
            return
        if self.action == "reworded":
            yield "token", {"text": "Added the Acme interview."}
            yield "done", {"action": "add_record", "pending": False, "status": "done"}
            return
        if self.action == "none":
            yield "token", {"text": "I can add notes and records. What would you like to do?"}
            yield "done", {"action": None, "pending": False, "status": "chat"}
            return
        draft = {"kind": "draft", "action": self.action, "prompt": "Save this? (yes/no)"}
        yield "token", {"text": "Here is the draft."}
        yield "draft", draft
        yield "done", {"action": self.action, "pending": True, "status": "draft"}


def plan(*steps: tuple[str, str]) -> str:
    return json.dumps({"steps": [{"agent": a, "instruction": i} for a, i in steps]})


def make(
    planner: list[Any], cloud: list[Any], local: list[Any], hits: list[Any], intake: FakeIntake
) -> tuple[SuperAgent, Factory, ConversationService]:
    specs = [
        spec("router", roles=["router"], max_tier="confidential"),
        spec("planner", roles=["generator"], max_tier="private", tags=["quality"]),
        spec("cloud", roles=["generator"], max_tier="private"),
        spec("local", roles=["generator"], max_tier="confidential", tags=["local"]),
    ]
    factory = Factory(
        {
            "router": [route_json("kb_search", "q")] * 5,
            "planner": planner,
            "cloud": cloud,
            "local": local,
        }
    )
    orch = orchestrator(inventory(*specs), factory, max_attempts=1)
    conversations = ConversationService(
        FakeAsyncRedis(decode_responses=True),
        MemoryConversationStore(),
        visitor_salt="salt",
        retry_base_delay=0,
    )
    searcher = FakeSearcher(hits=hits)
    owner = OwnerDeps(
        orchestrator=orch,
        retriever=searcher,
        reranker=FusedOrderReranker(),
        records=MemoryRecordsTool(),
        profile=ProfileTool(MemoryProfileStore()),
        conversations=conversations,
        generator=ChatGenerator(orch),
        today=lambda: TODAY,
    )
    prep = PrepDeps(
        orchestrator=orch,
        retriever=searcher,
        reranker=FusedOrderReranker(),
        conversations=conversations,
        generator=ChatGenerator(orch),
        filter=OutwardFilter(),
    )
    index = FakeIndex()
    agent = SuperAgent(
        SuperDeps(
            orchestrator=orch,
            conversations=conversations,
            index=index,
            owner_graph=build_owner_graph(owner, persist=False),
            prep_graph=build_prep_graph(prep, persist=False),
            intake=intake,  # type: ignore[arg-type]
            models=AUTO_CHOICE.model_copy(update={"prefer": "quality"}),
            today=lambda: TODAY,
        )
    )
    return agent, factory, conversations


class FakeIndex:
    def __init__(self) -> None:
        self.modes: dict[str, str] = {}

    async def set_mode(self, cid: str, mode: str) -> None:
        self.modes[cid] = mode

    async def get(self, cid: str) -> Any:
        return None

    async def names_like(self, base: str) -> set[str]:
        return set()

    async def set_title(self, cid: str, name: str | None, description: str | None) -> None:
        return None


async def turn(
    agent: SuperAgent, conversations: ConversationService, message: str, cid: str | None = None
) -> tuple[list[tuple[str, dict[str, Any]]], str]:
    prepared = await agent.prepare(SuperRequest(message=message, conversation_id=cid), "owner")
    events = [e async for e in agent.events(prepared)]
    await conversations.drain()
    return events, prepared.conversation_id


def of(events: list[tuple[str, dict[str, Any]]], name: str) -> list[dict[str, Any]]:
    return [d for e, d in events if e == name]


def test_rule_plan_and_compose() -> None:
    assert rule_plan("I applied to Acme for an AI Engineer role").steps[0].agent == "intake"
    assert rule_plan("Tell me about yourself for an interview").steps[0].agent == "prep"
    assert rule_plan("what did I learn about raft?").steps[0].agent == "ask"
    one = [StepResult(agent="ask", text="Raft [1].")]
    assert compose(one) == "Raft [1]."
    two = [
        StepResult(agent="intake", text="Added.", saved=True),
        StepResult(agent="prep", text="I am…"),
    ]
    assert compose(two) == "**Saved**\nAdded.\n\n**Your answer**\nI am…"


async def test_mixed_request_saves_then_prepares() -> None:
    intake = FakeIntake()
    pitch = "I build reliable AI systems; I cut p99 latency [add metric]."
    agent, factory, conversations = make(
        planner=[
            plan(
                ("intake", "Interview with Acme today 3 pm, AI Engineer"),
                ("prep", "60-second pitch for the Acme AI Engineer interview"),
            ),
            pitch,  # the super agent prefers quality models for its steps too
        ],
        cloud=["unused"],
        local=["unused"],
        hits=[hit("p1", Tier.PRIVATE, PRIV_TEXT)],
        intake=intake,
    )
    events, cid = await turn(
        agent, conversations, "Interview with Acme today at 3 pm; add it and give me a pitch"
    )
    meta = of(events, "meta")[0]
    assert [s["agent"] for s in meta["plan"]] == ["intake", "prep"]
    assert meta["plan_source"] == "model:planner"
    assert intake.told == ["Interview with Acme today 3 pm, AI Engineer", "yes"]
    answer = of(events, "token")[0]["text"]
    assert answer.startswith("**Saved**\nAdded the Acme interview")
    assert "**Your answer**\nI build reliable AI systems" in answer
    assert "[add metric]" in answer  # and its check is listed
    done = of(events, "done")[0]
    assert done["pending"] is False and done["steps"] == 2
    history = await conversations.history(cid)
    assert history[-1].guards["intake_conversation"] == "f" * 32
    assert factory.calls("local") == 0  # private context: no local model needed


async def test_delete_waits_for_yes_and_the_reply_goes_to_intake() -> None:
    intake = FakeIntake(action="delete_record")
    agent, _, conversations = make(
        planner=[plan(("intake", "Delete my Acme interview"))],
        cloud=["unused"],
        local=["unused"],
        hits=[],
        intake=intake,
    )
    events, cid = await turn(agent, conversations, "Delete my Acme interview")
    assert of(events, "done")[0]["pending"] is True
    assert intake.told == ["Delete my Acme interview"]
    assert "Save this? (yes/no)" in of(events, "token")[0]["text"]
    events, _ = await turn(agent, conversations, "yes", cid)
    assert of(events, "meta")[0]["plan_source"] == "reply"
    assert intake.told == ["Delete my Acme interview", "yes"]
    assert of(events, "done")[0]["pending"] is False


async def test_confidential_question_runs_local_and_planner_falls_back() -> None:
    intake = FakeIntake()
    agent, factory, conversations = make(
        planner=[ValueError("planner down")],
        cloud=["unused"],
        local=["Base pay [1]."],
        hits=[hit("conf", Tier.CONFIDENTIAL, CONF_TEXT)],
        intake=intake,
    )
    events, _ = await turn(agent, conversations, "what was in my offer?")
    meta = of(events, "meta")[0]
    assert [s["agent"] for s in meta["plan"]] == ["ask"]
    step = next(s for s in of(events, "step") if s["status"] != "running")
    assert step["model"] == "local" and step["tier"] == "confidential"
    assert of(events, "token")[0]["text"] == "Base pay [1]."
    # The cloud model may try to plan (the planner's fallback), but never sees the offer.
    seen = [str(m.content) for call in factory.models["cloud"].seen for m in call]
    assert not any(CONF_TEXT in text for text in seen)


async def test_an_intake_step_that_wrote_nothing_says_so() -> None:
    intake = FakeIntake(action="none")
    agent, _, conversations = make(
        planner=[plan(("intake", "Interview with Acme today"), ("prep", "pitch")), "I build."],
        cloud=["unused"],
        local=["unused"],
        hits=[hit("p1", Tier.PRIVATE, PRIV_TEXT)],
        intake=intake,
    )
    events, _ = await turn(agent, conversations, "Interview with Acme today; give me a pitch")
    answer = of(events, "token")[0]["text"]
    assert answer.startswith("**Not saved**\nNothing was saved")
    assert "**Saved**" not in answer


async def test_intake_question_waits_and_the_answer_goes_back_to_intake() -> None:
    intake = FakeIntake(action="ask")
    agent, _, conversations = make(
        planner=[plan(("intake", "I have an interview with Acme today at 3 pm"))],
        cloud=["unused"],
        local=["unused"],
        hits=[],
        intake=intake,
    )
    events, cid = await turn(agent, conversations, "I have an interview with Acme today at 3 pm")
    assert of(events, "done")[0]["pending"] is True
    assert "stage" in of(events, "token")[0]["text"]
    events, _ = await turn(agent, conversations, "screen", cid)
    assert of(events, "meta")[0]["plan_source"] == "reply"
    assert intake.told == ["I have an interview with Acme today at 3 pm", "screen"]
    assert of(events, "token")[0]["text"] == "Added the Acme interview (screen)."
    assert of(events, "done")[0]["pending"] is False


async def test_a_missed_rewording_is_retried_with_the_owners_message() -> None:
    intake = FakeIntake(action="reworded")
    agent, _, conversations = make(
        planner=[plan(("intake", "Add a new record for an interview with Acme today"))],
        cloud=["unused"],
        local=["unused"],
        hits=[],
        intake=intake,
    )
    message = "I have an interview with Acme today at 3 pm"
    events, _ = await turn(agent, conversations, message)
    assert intake.told == ["Add a new record for an interview with Acme today", message]
    assert of(events, "token")[0]["text"] == "Added the Acme interview."
