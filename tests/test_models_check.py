"""llm-gateway T5: retired model ids are reported, and a 404 sits a model out for a while."""

from __future__ import annotations

from typing import Any

import httpx
from pydantic import SecretStr

from app.core.config import Settings
from llm_orchestrator import ModelSpec
from llm_orchestrator.cooloff import MissingModels
from scripts import models_check
from tests.llm_orchestrator.fakes import spec


def cloud(model_id: str, provider: str, model: str) -> ModelSpec:
    raw: dict[str, Any] = {**spec(model_id, provider=provider), "model": model}
    return ModelSpec.model_validate(raw)


def test_retired_ids_are_reported_and_keys_stay_in_headers() -> None:
    seen: list[httpx.Request] = []

    def provider(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.host == "api.groq.com":
            return httpx.Response(200, json={"data": [{"id": "openai/gpt-oss-120b"}]})
        if request.url.host == "generativelanguage.googleapis.com":
            return httpx.Response(200, json={"models": [{"name": "models/gemini-x"}]})
        raise httpx.ConnectError("down", request=request)

    settings = Settings(
        _env_file=None,
        groq_api_key=SecretStr("gk-test"),
        gemini_api_key=SecretStr("ge-test"),
        cerebras_api_key=SecretStr("ck-test"),
    )
    specs = [
        cloud("groq-big", "groq", "openai/gpt-oss-120b"),
        cloud("groq-old", "groq", "llama-3.1-8b-instant"),
        cloud("gem", "gemini", "gemini-x"),
        cloud("cer", "cerebras", "qwen-x"),
        cloud("or", "openrouter", "gemma"),
        cloud("local", "ollama", "qwen2.5:7b"),
    ]
    with httpx.Client(transport=httpx.MockTransport(provider)) as client:
        results = models_check.check(specs, settings, client)
    assert {r.model_id: r.status for r in results} == {
        "groq-big": "ok",
        "groq-old": "missing",
        "gem": "ok",
        "cer": "unreachable",
        "or": "no key",
    }
    assert len([r for r in seen if r.url.host == "api.groq.com"]) == 1  # one listing per provider
    assert all("gk-test" not in str(r.url) and "ge-test" not in str(r.url) for r in seen)
    assert models_check.report(results) == 1


def test_a_not_found_model_cools_off_then_returns() -> None:
    now = [0.0]
    missing = MissingModels(seconds=600, clock=lambda: now[0])
    model = cloud("groq-old", "groq", "llama-3.1-8b-instant")
    missing.note(model, 500)  # other failures never start a cool-off
    assert not missing.cooling("groq-old")
    missing.note(model, 404)
    assert missing.cooling("groq-old")
    now[0] = 601.0
    assert not missing.cooling("groq-old")


async def test_orchestrator_skips_a_404_model_on_the_next_selection() -> None:
    from langchain_core.messages import HumanMessage

    from llm_orchestrator import Role, Tier
    from tests.llm_orchestrator.fakes import Factory, inventory, orchestrator

    class NotFound(Exception):
        status_code = 404

    factory = Factory({"gone": [NotFound("model not found")], "next": ["ok", "ok"]})
    orch = orchestrator(inventory(spec("gone"), spec("next")), factory, max_attempts=1)
    first = await orch.chat(Role.GENERATOR, Tier.PRIVATE, [HumanMessage(content="q")])
    assert first.model_id == "next" and orch.missing.cooling("gone")
    selection = await orch.select(Role.GENERATOR, Tier.PRIVATE)
    assert [m.id for m in selection.models] == ["next"]
