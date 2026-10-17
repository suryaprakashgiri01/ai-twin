"""AC3 (retries, fallback, result fields), AC5 (structured, one repair), AC8 (no content logs)."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import httpx
import pytest
import structlog
from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel
from structlog.testing import capture_logs

from app.core.logging import setup_logging
from llm_orchestrator import AllModelsFailedError, ProviderError, Role, Tier
from llm_orchestrator.executor import classify_error
from tests.llm_orchestrator.fakes import (
    SECRET_PROMPT,
    SECRET_REPLY,
    Factory,
    HTTPStatusFailure,
    inventory,
    orchestrator,
    spec,
)

MESSAGES = [HumanMessage(content=SECRET_PROMPT)]


def _pair() -> Any:
    return inventory(spec("first"), spec("second"))


class Route(BaseModel):
    route: str
    score: float


@pytest.mark.parametrize(
    "exc, transient, status",
    [
        (TimeoutError(), True, None),
        (httpx.ConnectError("down"), True, None),
        (HTTPStatusFailure(429), True, 429),
        (HTTPStatusFailure(503), True, 503),
        (HTTPStatusFailure(529), True, 529),
        (HTTPStatusFailure(401), False, 401),
        (HTTPStatusFailure(403), False, 403),
        (HTTPStatusFailure(400), False, 400),
        (ProviderError("x", transient=True), True, None),
        (type("RateLimitError", (Exception,), {})(), True, None),
        (ValueError("bad"), False, None),
    ],
)
def test_error_classification(exc: BaseException, transient: bool, status: int | None) -> None:
    assert classify_error(exc) == (transient, status)


def test_status_read_from_response() -> None:
    request = httpx.Request("POST", "http://provider.invalid")
    exc = httpx.HTTPStatusError("x", request=request, response=httpx.Response(502))
    assert classify_error(exc) == (True, 502)


async def test_success_reports_model_attempts_and_usage() -> None:
    factory = Factory({"first": ["hi"], "second": ["unused"]})
    result = await orchestrator(_pair(), factory).chat(Role.GENERATOR, Tier.PRIVATE, MESSAGES)
    assert (result.text, result.model_id, result.fallback_used) == ("hi", "first", False)
    assert [(a.model_id, a.number, a.outcome) for a in result.attempts] == [("first", 1, "ok")]
    assert (result.usage.input_tokens, result.usage.output_tokens) == (3, 2)


async def test_transient_retried_up_to_max_then_falls_back() -> None:
    factory = Factory({"first": [HTTPStatusFailure(429)], "second": ["ok"]})
    result = await orchestrator(_pair(), factory, max_attempts=3).chat(
        Role.GENERATOR, Tier.PRIVATE, MESSAGES
    )
    assert factory.calls("first") == 3
    assert result.model_id == "second"
    assert result.fallback_used is True
    assert [a.outcome for a in result.attempts] == ["transient_error"] * 3 + ["ok"]
    assert [a.number for a in result.attempts] == [1, 2, 3, 1]
    assert result.attempts[0].status_code == 429


async def test_transient_then_success_stays_on_model() -> None:
    factory = Factory({"first": [TimeoutError(), "ok"], "second": ["unused"]})
    result = await orchestrator(_pair(), factory).chat(Role.GENERATOR, Tier.PRIVATE, MESSAGES)
    assert (result.model_id, result.fallback_used, len(result.attempts)) == ("first", False, 2)


async def test_permanent_failure_falls_back_without_retry() -> None:
    factory = Factory({"first": [HTTPStatusFailure(401)], "second": ["ok"]})
    result = await orchestrator(_pair(), factory, max_attempts=3).chat(
        Role.GENERATOR, Tier.PRIVATE, MESSAGES
    )
    assert factory.calls("first") == 1
    assert result.model_id == "second"
    assert result.fallback_used is True
    assert result.attempts[0].outcome == "permanent_error"


async def test_slow_model_times_out_as_transient() -> None:
    factory = Factory({"first": ["late"], "second": ["ok"]})

    async def slow(*_args: object, **_kwargs: object) -> None:
        await asyncio.sleep(1)

    object.__setattr__(factory.models["first"], "ainvoke", slow)
    orch = orchestrator(_pair(), factory, max_attempts=1)
    orch._executor._timeout = 0.01
    result = await orch.chat(Role.GENERATOR, Tier.PRIVATE, MESSAGES)
    assert result.model_id == "second"
    assert result.attempts[0].error_type == "TimeoutError"


async def test_all_models_failed_lists_every_attempt() -> None:
    factory = Factory({"first": [HTTPStatusFailure(500)], "second": [HTTPStatusFailure(403)]})
    with pytest.raises(AllModelsFailedError) as info:
        await orchestrator(_pair(), factory, max_attempts=2).chat(
            Role.GENERATOR, Tier.PRIVATE, MESSAGES
        )
    outcomes = [(a.model_id, a.outcome) for a in info.value.attempts]
    assert outcomes == [
        ("first", "transient_error"),
        ("first", "transient_error"),
        ("second", "permanent_error"),
    ]


async def test_callbacks_and_max_tokens_reach_the_model() -> None:
    seen: list[str] = []

    class Handler(AsyncCallbackHandler):
        async def on_chat_model_start(self, *_args: Any, **_kwargs: Any) -> None:
            seen.append("start")

    factory = Factory({"first": ["ok"], "second": ["unused"]})
    await orchestrator(_pair(), factory).chat(
        Role.GENERATOR, Tier.PRIVATE, MESSAGES, callbacks=[Handler()], max_tokens=16
    )
    assert seen == ["start"]


async def test_structured_returns_schema_instance() -> None:
    factory = Factory({"first": ['```json\n{"route": "about_owner", "score": 0.9}\n```']})
    orch = orchestrator(inventory(spec("first")), factory)
    value = await orch.structured(Role.ROUTER, Tier.PUBLIC, Route, MESSAGES)
    assert value == Route(route="about_owner", score=0.9)
    prompt = factory.models["first"].seen[0]
    assert isinstance(prompt[0], SystemMessage)
    assert "JSON Schema" in str(prompt[0].content)


async def test_structured_repairs_once_on_same_model() -> None:
    factory = Factory({"first": ['{"route": "x"}', '{"route": "x", "score": 1}']})
    orch = orchestrator(_pair(), factory)
    result = await orch.structured_call(Role.ROUTER, Tier.PUBLIC, Route, MESSAGES)
    assert result.value.score == 1
    assert (result.model_id, result.fallback_used) == ("first", False)
    assert [a.outcome for a in result.attempts] == ["invalid_output", "ok"]
    repair = factory.models["first"].seen[1][-1]
    assert "score" in str(repair.content)


async def test_structured_moves_on_after_failed_repair() -> None:
    factory = Factory(
        {"first": ["not json", "still not json"], "second": ['{"route":"y","score":0}']}
    )
    result = await orchestrator(_pair(), factory).structured_call(
        Role.ROUTER, Tier.PUBLIC, Route, MESSAGES
    )
    assert factory.calls("first") == 2
    assert (result.model_id, result.fallback_used) == ("second", True)
    assert [a.outcome for a in result.attempts] == ["invalid_output", "invalid_output", "ok"]


async def test_structured_provider_errors_fall_back_and_finally_fail() -> None:
    factory = Factory({"first": [HTTPStatusFailure(401)], "second": ["{}", "{}"]})
    with pytest.raises(AllModelsFailedError):
        await orchestrator(_pair(), factory).structured(Role.ROUTER, Tier.PUBLIC, Route, MESSAGES)


async def test_repair_call_failure_moves_on() -> None:
    factory = Factory(
        {"first": ["{}", HTTPStatusFailure(400)], "second": ['{"route":"z","score":0.1}']}
    )
    result = await orchestrator(_pair(), factory).structured_call(
        Role.ROUTER, Tier.PUBLIC, Route, MESSAGES
    )
    assert result.model_id == "second"


async def test_chat_model_for_returns_first_eligible() -> None:
    factory = Factory({"first": ["a"], "second": ["b"]})
    spec_, model = await orchestrator(_pair(), factory).chat_model_for(Role.JUDGE, Tier.PRIVATE)
    assert spec_.id == "first"
    assert model is factory.models["first"]


async def test_no_log_line_contains_prompt_or_completion(
    capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    """AC8, checked at both structlog's event dicts and the rendered JSON lines."""
    factory = Factory(
        {
            "first": [HTTPStatusFailure(429), SECRET_REPLY],
            "second": [SECRET_REPLY, f'{{"route": "{SECRET_REPLY}", "score": 1}}'],
        }
    )
    orch = orchestrator(_pair(), factory, max_attempts=2)
    with capture_logs() as events:
        await orch.chat(Role.GENERATOR, Tier.PRIVATE, MESSAGES)
        await orch.structured(Role.ROUTER, Tier.PUBLIC, Route, MESSAGES)
    assert len([e for e in events if e["event"] == "llm attempt"]) >= 4
    for event in events:
        assert SECRET_PROMPT not in repr(event)
        assert SECRET_REPLY not in repr(event)

    setup_logging(level="DEBUG", log_format="json")
    caplog.set_level(logging.DEBUG)
    try:
        await orch.chat(Role.GENERATOR, Tier.PRIVATE, MESSAGES)
        await orch.structured(Role.ROUTER, Tier.PUBLIC, Route, MESSAGES)
    finally:
        structlog.reset_defaults()
    output = capsys.readouterr()
    lines = [json.loads(line) for line in output.out.splitlines() if line.startswith("{")]
    assert any(line["event"] == "llm attempt" for line in lines)
    rendered = output.out + output.err + caplog.text
    assert "CANARY" not in rendered
