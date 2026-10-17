"""Owner-agent T5: pre-first-token fallback, mid-stream failure, SSE frames (AC7, AC8)."""

from __future__ import annotations

import json

from app.agents.streaming import sse
from app.exceptions import ErrorCode
from llm_orchestrator import Role, Tier
from tests.agent.harness import PRIV_TEXT, hit, make_agent, route_json

Q = "what did I learn at the meetup?"


def agent_with(**streams: list[list[object]]):  # type: ignore[no-untyped-def]
    return make_agent(
        router=[route_json("kb_search", Q)],
        cloud=["unused"],
        local=["unused"],
        hits=[hit("priv", Tier.PRIVATE, PRIV_TEXT)],
        streams=streams,
        idle_timeout=0.05,
    )


def test_sse_frame_format() -> None:
    frame = sse("token", {"text": "line one\nline two"})
    assert frame.startswith("event: token\ndata: ") and frame.endswith("\n\n")
    assert frame.count("\n") == 3  # the payload's newline is escaped inside JSON
    assert json.loads(frame.split("data: ", 1)[1]) == {"text": "line one\nline two"}


async def test_tokens_stream_in_chunks() -> None:
    agent = agent_with(cloud=[["Raft ", "leader ", "election [1]."]])
    state, tokens = await agent.run(Q)
    assert tokens == ["Raft ", "leader ", "election [1]."]
    assert state["answer"] == "Raft leader election [1]."
    assert state["model_id"] == "cloud" and state["incomplete"] is False
    assert state["input_tokens"] == 3 and state["output_tokens"] == 3


async def test_failure_before_first_token_falls_back() -> None:
    agent = agent_with(cloud=[[ConnectionError("down")]], local=[["Local [1]."]])
    state, tokens = await agent.run(Q)
    assert tokens == ["Local [1]."]
    assert state["model_id"] == "local" and state["error"] is None


async def test_stall_before_first_token_falls_back() -> None:
    agent = agent_with(cloud=[[1.0, "late"]], local=[["Local [1]."]])
    state, _ = await agent.run(Q)
    assert state["model_id"] == "local"


async def test_mid_stream_failure_keeps_partial_and_marks_incomplete() -> None:
    agent = agent_with(cloud=[["Raft ", ConnectionError("dropped")]], local=[["never"]])
    state, tokens = await agent.run(Q)
    assert tokens == ["Raft "]
    assert state["incomplete"] is True
    assert state["error"] is not None and state["error"].code is ErrorCode.STREAM_INTERRUPTED
    assert agent.factory.models["local"].calls == 0  # no splice of a second answer
    turns = await agent.history(state["conversation_id"])
    assert turns[-1].incomplete is True and turns[-1].content == "Raft "
    assert turns[-1].model == "cloud"
    assert agent.store.turns[state["conversation_id"]][2].incomplete is True


async def test_every_model_failing_first_is_model_unavailable() -> None:
    agent = agent_with(cloud=[[ConnectionError("a")]], local=[[ValueError("b")]])
    state, tokens = await agent.run(Q)
    assert tokens == []
    assert state["error"] is not None and state["error"].code is ErrorCode.MODEL_UNAVAILABLE
    assert state["incomplete"] is True and state["answer"] == ""


async def test_orchestrator_lists_every_eligible_model_in_order() -> None:
    agent = agent_with(cloud=[["x"]], local=[["y"]])
    private = await agent.orchestrator.chat_models(Role.GENERATOR, Tier.PRIVATE)
    assert [spec.id for spec, _ in private] == ["cloud", "local"]
    confidential = await agent.orchestrator.chat_models(Role.GENERATOR, Tier.CONFIDENTIAL)
    assert [spec.id for spec, _ in confidential] == ["local"]
