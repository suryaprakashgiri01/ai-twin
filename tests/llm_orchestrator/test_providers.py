"""T4: builders construct the right LangChain class offline; a missing key means disabled."""

from __future__ import annotations

import pytest
from langchain_core.messages import HumanMessage
from pydantic import SecretStr

from llm_orchestrator import (
    ModelSpec,
    Orchestrator,
    OrchestratorSettings,
    ProviderSecrets,
    Role,
    Tier,
)
from llm_orchestrator.providers import MockChatModel, build_chat_model, is_available
from tests.llm_orchestrator.fakes import inventory, spec

KEY = SecretStr("test-key-not-real")
ALL = ProviderSecrets(
    groq_api_key=KEY,
    cerebras_api_key=KEY,
    gemini_api_key=KEY,
    openrouter_api_key=KEY,
    ollama_base_url="http://127.0.0.1:11434",
    mock_enabled=True,
)


def _spec(provider: str, max_tier: str = "public") -> ModelSpec:
    return ModelSpec.model_validate(spec(f"{provider}-m", provider=provider, max_tier=max_tier))


@pytest.mark.parametrize(
    "provider, class_name",
    [
        ("groq", "ChatGroq"),
        ("cerebras", "ChatCerebras"),
        ("gemini", "ChatGoogleGenerativeAI"),
        ("openrouter", "ChatOpenAI"),
        ("ollama", "ChatOllama"),
        ("mock", "MockChatModel"),
    ],
)
def test_builders_construct_without_network(provider: str, class_name: str) -> None:
    model = build_chat_model(_spec(provider), ALL, timeout=5)
    assert type(model).__name__ == class_name
    if hasattr(model, "max_retries") and provider != "ollama":
        assert model.max_retries == 0  # the executor owns retries


def test_openrouter_uses_its_base_url() -> None:
    model = build_chat_model(_spec("openrouter"), ALL, timeout=5)
    assert "openrouter.ai" in str(model.openai_api_base)  # type: ignore[attr-defined]


def test_ollama_model_setting_replaces_spec_name_per_entry() -> None:
    secrets = ProviderSecrets(
        ollama_base_url="http://127.0.0.1:11434", ollama_models={"ollama-m": "qwen2.5:3b"}
    )
    model = build_chat_model(_spec("ollama"), secrets, timeout=5)
    assert model.model == "qwen2.5:3b"  # type: ignore[attr-defined]
    other = ModelSpec.model_validate(spec("ollama-q", provider="ollama"))
    assert build_chat_model(other, secrets, timeout=5).model == "ollama-q-name"  # type: ignore[attr-defined]


@pytest.mark.parametrize("provider", ["groq", "cerebras", "gemini", "openrouter", "ollama", "mock"])
def test_missing_key_is_unavailable(provider: str) -> None:
    assert is_available(_spec(provider), ALL)
    assert not is_available(_spec(provider), ProviderSecrets())
    assert not is_available(_spec(provider), ProviderSecrets(groq_api_key=SecretStr(" ")))


def test_building_without_key_is_a_bug() -> None:
    with pytest.raises(ValueError, match="key missing"):
        build_chat_model(_spec("groq"), ProviderSecrets(), timeout=5)


async def test_missing_key_models_are_skipped_not_errors() -> None:
    inv = inventory(spec("groq-m", provider="groq"), spec("mock-m", provider="mock"))
    orch = Orchestrator(inv, OrchestratorSettings(secrets=ProviderSecrets(mock_enabled=True)))
    counts = await orch.model_counts()
    assert counts == {"router": 1, "classifier": 1, "generator": 1, "judge": 1, "guard": 0}
    result = await orch.chat(Role.ROUTER, Tier.PRIVATE, [HumanMessage(content="hi")])
    assert (result.model_id, result.text) == ("mock-m", "mock response")


def test_mock_model_does_not_echo_input() -> None:
    reply = MockChatModel().invoke([HumanMessage(content="secret words")])
    assert "secret" not in str(reply.content)
