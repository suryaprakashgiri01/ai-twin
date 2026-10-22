"""owner-console: each model's status for the picker (pulled, keyed, still served)."""

from __future__ import annotations

import httpx
from pydantic import SecretStr

from app.core.config import Settings
from app.core.llm import provider_secrets
from app.services.model_status import ModelStatusChecker
from llm_orchestrator import ModelSpec
from tests.llm_orchestrator.fakes import spec


def models(*raw: dict[str, object]) -> list[ModelSpec]:
    return [ModelSpec.model_validate(r) for r in raw]


SPECS = models(
    spec("pulled", provider="ollama", max_tier="confidential"),
    spec("absent", provider="ollama", max_tier="confidential"),
    spec("groq-ok", provider="groq"),
    spec("groq-gone", provider="groq"),
    spec("no-key", provider="cerebras"),
    spec("off", provider="groq", enabled=False),
)


def checker(ollama_up: bool = True) -> tuple[ModelStatusChecker, list[str]]:
    calls: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        if request.url.host == "ollama":
            if not ollama_up:
                raise httpx.ConnectError("down")
            return httpx.Response(200, json={"models": [{"name": "pulled-name"}]})
        assert request.headers["authorization"] == "Bearer gk"
        return httpx.Response(200, json={"data": [{"id": "groq-ok-name"}]})

    settings = Settings(
        _env_file=None,
        app_env="test",
        groq_api_key=SecretStr("gk"),
        cerebras_api_key=SecretStr(""),
        ollama_base_url="http://ollama:11434",
    )
    secrets = provider_secrets(settings)
    return ModelStatusChecker(settings, secrets, httpx.MockTransport(handle)), calls


async def test_statuses_say_what_to_fix_and_verify_is_opt_in() -> None:
    check, calls = checker()
    states = await check.check(SPECS, lambda s: s.enabled)
    assert {k: v[0] for k, v in states.items()} == {
        "pulled": "active",
        "absent": "not pulled",
        "groq-ok": "active",
        "groq-gone": "active",  # only a live check can tell
        "no-key": "no key",
        "off": "disabled",
    }
    assert states["absent"][1] == 'make ollama ARGS="pull absent-name"'
    assert states["no-key"][1] == "set CEREBRAS_API_KEY in .env"
    assert calls == ["ollama"]  # no provider is called without verify
    verified = await check.check(SPECS, lambda s: s.enabled, verify=True)
    assert verified["groq-gone"][0] == "missing" and verified["groq-ok"][0] == "active"
    assert calls.count("api.groq.com") == 1  # one listing per provider


async def test_ollama_down_is_unreachable() -> None:
    check, _ = checker(ollama_up=False)
    states = await check.check(SPECS, lambda s: s.enabled)
    assert states["pulled"][0] == "unreachable" and states["groq-ok"][0] == "active"
