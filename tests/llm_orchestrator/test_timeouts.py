"""Local models get their own (longer) timeout; cloud models keep the default."""

from __future__ import annotations

from llm_orchestrator import ModelSpec, OrchestratorSettings, ProviderSecrets
from llm_orchestrator.providers import default_factory
from tests.llm_orchestrator.fakes import spec


def _spec(tags: tuple[str, ...], provider: str = "mock") -> ModelSpec:
    return ModelSpec.model_validate(spec("m", provider=provider, tags=tags))


def test_timeout_for_uses_the_local_value_only_for_local_models() -> None:
    settings = OrchestratorSettings(
        secrets=ProviderSecrets(), timeout_seconds=30, local_timeout_seconds=120
    )
    assert settings.timeout_for(_spec(("local",))) == 120
    assert settings.timeout_for(_spec(("fast",))) == 30
    assert OrchestratorSettings(secrets=ProviderSecrets()).timeout_for(_spec(("local",))) == 30


def test_factory_builds_ollama_with_the_local_timeout() -> None:
    settings = OrchestratorSettings(
        secrets=ProviderSecrets(ollama_base_url="http://127.0.0.1:11434"),
        timeout_seconds=30,
        local_timeout_seconds=120,
    )
    factory = default_factory(settings.secrets, settings.timeout_for)
    model = factory(_spec(("local",), provider="ollama"))
    assert getattr(model, "client_kwargs", {}).get("timeout") == 120
