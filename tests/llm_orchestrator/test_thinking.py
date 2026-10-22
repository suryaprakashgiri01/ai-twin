"""llm-gateway T4: a reasoning model's thinking never reaches an answer."""

from __future__ import annotations

import pytest

from llm_orchestrator.thinking import ThinkingFilter, strip_thinking

SECRET = "the owner's offer is 99 units"


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        (f"<think>{SECRET}</think>\nThe answer.", "The answer."),
        ("No thinking here.", "No thinking here."),
        (f"<think>{SECRET}", ""),  # cut off mid-thought: nothing of it is shown
        (f"Start <think>{SECRET}</think> end", "Start  end"),
    ],
)
def test_strip_thinking(raw: str, clean: str) -> None:
    text, thinking = strip_thinking(raw)
    assert text == clean and SECRET not in text
    assert thinking == (SECRET if "<think>" in raw else "")


def test_filter_handles_tags_split_across_chunks() -> None:
    stream = ["<thi", "nk>the owner's ", "offer is 99 units</th", "ink>\nHel", "lo <", "b>!"]
    filt = ThinkingFilter()
    out = "".join(filt.feed(c) for c in stream) + filt.flush()
    assert out == "Hello <b>!" and SECRET not in out
    assert filt.take() == SECRET and filt.take() == ""


async def test_executor_and_stream_drop_thinking() -> None:
    from langchain_core.messages import HumanMessage

    from app.agents.generation import Generation
    from app.agents.streaming import StreamingGenerator
    from llm_orchestrator import Role, Tier
    from tests.agent.harness import StreamModel
    from tests.llm_orchestrator.fakes import Factory, inventory, orchestrator, spec

    factory = Factory({"m": [f"<think>{SECRET}</think>Plain answer."]})
    orch = orchestrator(inventory(spec("m", roles=["generator"])), factory)
    result = await orch.chat(Role.GENERATOR, Tier.PRIVATE, [HumanMessage(content="q")])
    assert result.text == "Plain answer." and result.thinking == SECRET

    streaming = Factory({})
    streaming.models["m"] = StreamModel(plays=[["<think>", SECRET, "</think>", "Streamed."]])  # type: ignore[assignment]
    orch = orchestrator(inventory(spec("m", roles=["generator"])), streaming)
    gen = StreamingGenerator(orch, idle_timeout_seconds=5)
    shown: list[str] = []
    out = Generation()
    stream = gen.stream(Tier.PRIVATE, [HumanMessage(content="q")], out, on_thinking=shown.append)
    parts = [t async for t in stream]
    assert "".join(parts) == "Streamed." and all(SECRET not in p for p in parts)
    assert "".join(shown) == SECRET and "".join(out.thinking) == SECRET
    # Without a listener (the public channel) the thinking is simply not forwarded.
    quiet = [t async for t in StreamingGenerator(orch).stream(Tier.PRIVATE, [], Generation())]
    assert all(SECRET not in p for p in quiet)
