"""observability-alerts T2: Langfuse traces per run, masked by channel (AC3, AC4, AC5).

The Langfuse client runs for real with its OTLP exporter swapped for an in-memory one, so the
assertions are on exactly what would have been sent.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.core.logging import setup_logging
from app.core.telemetry import current_trace_id, tracer
from app.observability import tracing as tracing_module
from app.observability.masking import LEAD_MASK, Masker, MaskPolicy, channel_scope, set_channel
from app.observability.tracing import NOOP, NOOP_RUN, LangfuseTracing, build_tracing, install
from app.services.owner_chat import ChatRequest, OwnerChatService
from llm_orchestrator import Tier
from tests.agent.harness import PRIV_TEXT, hit, make_agent, route_json
from tests.leads.harness import extraction, make_capture
from tests.observability.fakes import (
    CapturedTraces,
    attrs_of,
    capture_with,
    langfuse_settings,
)
from tests.public.fakes import ANSWER, PublicMemoryIndex, make_public

OWNER_Q = "what did I learn at the meetup?"
OWNER_ANSWER = "Raft leader election [1]."
VISITOR_EMAIL = "visitor.one@example.org"
VISITOR_PHONE = "+44 20 7946 0958"


async def owner_chat(question: str = OWNER_Q) -> tuple[str, list[tuple[str, dict[str, Any]]]]:
    agent = make_agent(
        router=[route_json("kb_search", question)],
        cloud=[OWNER_ANSWER],
        hits=[hit("priv", Tier.PRIVATE, PRIV_TEXT)],
    )
    service = OwnerChatService(agent.graph, agent.conversations, PublicMemoryIndex(agent.store))
    with tracer().start_as_current_span("POST /admin/v1/chat"):
        request_trace_id = current_trace_id()
        prepared = await service.prepare(ChatRequest(message=question, max_tier="private"))
        events = [e async for e in service.events(prepared)]
    await agent.conversations.drain()
    assert request_trace_id is not None
    return request_trace_id, events


# --- off by default ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "keys",
    [
        {},
        {"langfuse_public_key": "pk", "langfuse_secret_key": "sk"},
        {"langfuse_base_url": "https://cloud.langfuse.com", "langfuse_secret_key": "sk"},
    ],
)
def test_without_all_three_keys_tracing_is_a_no_op(
    monkeypatch: pytest.MonkeyPatch, keys: dict[str, Any]
) -> None:
    def refuse(**_: Any) -> None:
        raise AssertionError("no Langfuse client without keys")

    monkeypatch.setattr(tracing_module, "Langfuse", refuse)
    tracing = build_tracing(Settings(_env_file=None, **keys))
    assert tracing is NOOP
    with tracing.run("owner-chat", "owner", input="text") as run:
        run.score("x", 1.0)
    assert run is NOOP_RUN


async def test_owner_chat_runs_untraced_without_keys() -> None:
    install(NOOP)
    _, events = await owner_chat()
    assert events[-1][0] == "done"


# --- owner channel: masked --------------------------------------------------------------------


async def test_owner_chat_is_one_masked_trace_with_the_request_trace_id(
    captured: CapturedTraces,
) -> None:
    request_trace_id, events = await owner_chat()
    assert events[-1][0] == "done"

    spans = captured.spans()
    assert {format(s.context.trace_id, "032x") for s in spans} == {request_trace_id}
    [root] = captured.roots()
    attrs = attrs_of(root)
    assert attrs["langfuse.trace.name"] == "owner-chat"
    assert {"channel:owner", "route:kb_search"} <= set(attrs["langfuse.trace.tags"])
    assert attrs["session.id"] == events[-1][1]["conversation_id"]
    assert attrs["langfuse.trace.input"].startswith("[owner content masked: ")
    assert attrs["langfuse.trace.output"].startswith("[owner content masked: ")

    generations = captured.generations()
    assert generations, "router and generator calls are generations"
    for generation in generations:
        g = attrs_of(generation)
        assert g["langfuse.observation.model.name"] in {"router-name", "cloud-name"}
        assert json.loads(str(g["langfuse.observation.usage_details"]))["input"] == 3
        assert g["langfuse.observation.metadata.llm_tier"] in {"private", "confidential"}
        assert str(g["langfuse.observation.input"]).startswith("[owner content masked: ")

    sent = captured.text()
    for secret in (OWNER_Q, OWNER_ANSWER, PRIV_TEXT, "meetup"):
        assert secret not in sent


async def test_owner_content_opt_in_sends_text_to_a_self_hosted_langfuse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for captured in capture_with(
        monkeypatch, langfuse_owner_content=True, langfuse_base_url="http://langfuse.lan:3000"
    ):
        await owner_chat()
        assert OWNER_Q in captured.text()


@pytest.mark.parametrize(
    "url",
    ["https://cloud.langfuse.com", "https://us.cloud.langfuse.com", "https://hipaa.langfuse.com/"],
)
def test_owner_content_with_a_cloud_url_is_refused_at_startup(url: str) -> None:
    with pytest.raises(ValidationError, match="self-hosted"):
        Settings(_env_file=None, langfuse_owner_content=True, langfuse_base_url=url)


def test_owner_content_with_a_self_hosted_url_is_allowed() -> None:
    settings = Settings(
        _env_file=None, langfuse_owner_content=True, langfuse_base_url="https://lf.example.dev"
    )
    assert settings.langfuse_owner_content


# --- public channel: identifiers masked -------------------------------------------------------


async def test_public_trace_keeps_the_text_with_identifiers_masked(
    captured: CapturedTraces,
) -> None:
    public = make_public()
    question = f"What did you build? Write to {VISITOR_EMAIL} or call {VISITOR_PHONE}"
    events = await public.ask(question)
    assert events[-1][0] == "done"

    [root] = captured.roots()
    attrs = attrs_of(root)
    assert attrs["langfuse.trace.name"] == "public-chat"
    assert {"channel:public", "intent:about_owner"} <= set(attrs["langfuse.trace.tags"])
    assert attrs["langfuse.trace.input"] == "What did you build? Write to [EMAIL] or call [PHONE]"
    assert attrs["langfuse.trace.output"] == ANSWER
    sent = captured.text()
    assert VISITOR_EMAIL not in sent and "7946" not in sent
    assert "What did you build?" in sent


async def test_public_content_off_masks_public_text(monkeypatch: pytest.MonkeyPatch) -> None:
    for captured in capture_with(monkeypatch, langfuse_public_content=False):
        await make_public().ask("What did you build?")
        sent = captured.text()
        assert "What did you build?" not in sent and ANSWER not in sent
        assert "[public content masked: " in sent


# --- leads: always masked ---------------------------------------------------------------------


async def test_lead_capture_turns_never_trace_lead_fields(captured: CapturedTraces) -> None:
    c = make_capture(
        [
            extraction(role="backend", company="Acme"),
            extraction(),
            extraction(name="Ravi Kumar"),
            extraction(email="ravi.kumar@acme.example"),
            "Ravi from Acme is hiring a senior backend engineer.",
        ]
    )
    await c.say("I'm hiring for a backend role at Acme")
    await c.say("We build payment APIs in Python and need a senior backend engineer.")
    await c.say("I'm Ravi Kumar")
    await c.say("ravi.kumar@acme.example")
    await c.say("yes")
    assert len(c.store.leads) == 1

    sent = captured.text()
    for field_value in ("Ravi", "ravi.kumar@acme.example", "payment APIs"):
        assert field_value not in sent
    names = {(s.attributes or {}).get("langfuse.trace.name") for s in captured.roots()}
    assert names == {"public-chat"}
    lead_spans = [s for s in captured.spans() if s.name == "lead-submit"]
    assert len(lead_spans) == 1, "the lead submission is a span of the public trace"


def test_masker_policies() -> None:
    masker = Masker(MaskPolicy(owner_content=True))
    set_channel("owner")
    assert masker(data={"question": "q", "lead": {"name": "R"}}) == {
        "question": "q",
        "lead": LEAD_MASK,
    }
    with channel_scope("leads"):
        assert masker(data="Ravi Kumar") == "[lead content masked: 10 chars]"
    set_channel(None)
    # Outside any run: masked, so an untraced call site fails closed.
    assert masker(data="anything") == "[owner content masked: 8 chars]"
    assert masker(data={"llm_model": "gen", "ls_provider": "groq", "text": "abc"}) == {
        "llm_model": "gen",
        "ls_provider": "groq",
        "text": "[owner content masked: 3 chars]",
    }


# --- outages ----------------------------------------------------------------------------------


async def test_langfuse_failures_never_fail_a_run_and_warn_once_a_minute(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    setup_logging(level="INFO", log_format="json")
    for captured in capture_with(monkeypatch):

        def broken(**_: Any) -> None:
            raise ConnectionError("langfuse unreachable")

        monkeypatch.setattr(captured.tracing.client, "start_span", broken)
        for _ in range(3):
            _, events = await owner_chat()
            assert events[-1][0] == "done"
    warnings = [
        json.loads(line)
        for line in capsys.readouterr().out.splitlines()
        if '"langfuse call failed"' in line
    ]
    assert len(warnings) == 1


async def test_an_unreachable_langfuse_logs_at_most_once_a_minute(
    capsys: pytest.CaptureFixture[str],
) -> None:
    setup_logging(level="INFO", log_format="json")
    # Nothing listens on port 9: every export fails.
    tracing = LangfuseTracing(langfuse_settings())
    install(tracing)
    try:
        for _ in range(3):
            _, events = await owner_chat()
            assert events[-1][0] == "done"
            tracing.flush()
    finally:
        install(NOOP)
        tracing.shutdown()
    noisy = [
        line
        for line in capsys.readouterr().out.splitlines()
        if line.strip()
        and json.loads(line).get("level") in {"warning", "error", "critical"}
        and json.loads(line).get("logger", "").startswith(("langfuse", "opentelemetry", "app.obs"))
    ]
    assert len(noisy) == 1, noisy
