"""T7: the app builds one orchestrator from settings; /readyz reports per-role model counts."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from app.core.app_factory import create_app
from app.core.config import DEFAULT_MODEL_INVENTORY, Settings
from app.core.llm import build_llm, provider_secrets
from llm_orchestrator.classify import FlaggedClassifier
from llm_orchestrator.types import Role, Tier
from tests.conftest import FakeCheck

KEYLESS = {
    "groq_api_key": None,
    "cerebras_api_key": None,
    "gemini_api_key": None,
    "openrouter_api_key": None,
    "typesafe_api_key": None,
    "ollama_base_url": "",
}


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {"app_env": "test", "log_format": "json", **KEYLESS, **overrides}
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


async def _readyz(settings: Settings) -> dict[str, object]:
    app = create_app(settings, health_checks=[FakeCheck("mongodb")])
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/readyz")
    assert response.status_code == 200
    body: dict[str, object] = response.json()
    return body


def test_default_inventory_path_is_the_repo_file() -> None:
    assert DEFAULT_MODEL_INVENTORY.is_file()
    assert _settings().model_inventory_path == DEFAULT_MODEL_INVENTORY


async def test_keyless_app_starts_with_zero_models() -> None:
    body = await _readyz(_settings())
    assert body["models"] == {
        "router": 0,
        "classifier": 0,
        "generator": 0,
        "judge": 0,
        "guard": 0,
    }


async def test_keys_and_endpoints_enable_models() -> None:
    settings = _settings(
        groq_api_key="test-key-not-real",
        ollama_base_url="http://127.0.0.1:11434",
        llm_mock_enabled=True,
    )
    body = await _readyz(settings)
    # Groq (1 per role) + Ollama (3 for generator/judge) + mock; the guard role is Groq only.
    assert body["models"] == {
        "router": 3,
        "classifier": 3,
        "generator": 5,
        "judge": 5,
        "guard": 1,
    }


def test_settings_map_to_provider_secrets() -> None:
    secrets = provider_secrets(
        _settings(ollama_model="qwen2.5:3b", ollama_quality_model="", llm_mock_enabled=True)
    )
    assert secrets.ollama_base_url is None
    assert secrets.ollama_models == {"ollama-local": "qwen2.5:3b"}
    assert secrets.mock_enabled is True


def test_jev_wired_only_with_key_and_flag() -> None:
    without_key = build_llm(_settings(ff_jev_enabled=True)).classifier
    assert isinstance(without_key, FlaggedClassifier)
    assert not without_key.uses_jev(Tier.PUBLIC)

    keyed = _settings(ff_jev_enabled=True, typesafe_api_key=SecretStr("tsk-test-not-real"))
    classifier = build_llm(keyed).classifier
    assert isinstance(classifier, FlaggedClassifier)
    assert classifier.uses_jev(Tier.PUBLIC)
    assert not classifier.uses_jev(Tier.PRIVATE)

    flag_off = build_llm(_settings(typesafe_api_key=SecretStr("tsk-test-not-real"))).classifier
    assert isinstance(flag_off, FlaggedClassifier)
    assert not flag_off.uses_jev(Tier.PUBLIC)


async def test_confidential_answers_prefer_the_quality_local_model() -> None:
    settings = _settings(
        groq_api_key="test-key-not-real",
        ollama_base_url="http://127.0.0.1:11434",
        ollama_model="llama3.2:1b",
        llm_mock_enabled=True,
    )
    orch = build_llm(settings).orchestrator
    confidential = await orch.select(Role.GENERATOR, Tier.CONFIDENTIAL)
    assert [m.id for m in confidential.models] == [
        "ollama-qwen",
        "ollama-local",
        "ollama-qwen-14b",
        "mock",
    ]
    private = await orch.select(Role.GENERATOR, Tier.PRIVATE)
    assert private.models[0].id == "groq-gpt-oss-120b"
    router = await orch.select(Role.ROUTER, Tier.CONFIDENTIAL)
    assert "ollama-qwen" not in [m.id for m in router.models]
    assert provider_secrets(settings).ollama_models == {"ollama-local": "llama3.2:1b"}


def test_agent_models_resolve_auto_pins_and_the_tier_rule(tmp_path: Path) -> None:
    from app.agents.models import AgentModel, AgentModelError, AgentModels
    from llm_orchestrator import Inventory

    inventory = Inventory.from_yaml(DEFAULT_MODEL_INVENTORY)
    shipped = AgentModels.load(inventory)
    assert shipped.choice("super").pick(Tier.CONFIDENTIAL) == (None, "local-max")
    assert shipped.choice("prep").pick(Tier.PRIVATE) == (None, "quality")
    assert shipped.choice("owner").pick(Tier.PRIVATE, "mock") == ("mock", None)
    pinned = AgentModels.build(
        {"super": AgentModel(model="groq-gpt-oss-120b", confidential_model="ollama-qwen-14b")},
        inventory,
    ).choice("super")
    assert pinned.pick(Tier.PRIVATE) == ("groq-gpt-oss-120b", None)
    # A private-capped pin never reads confidential text: the local pin takes over.
    assert pinned.pick(Tier.CONFIDENTIAL) == ("ollama-qwen-14b", None)
    with pytest.raises(AgentModelError, match="cloud model"):
        AgentModels.build({"x": AgentModel(confidential_model="groq-gpt-oss-120b")}, inventory)
    with pytest.raises(AgentModelError, match="not in"):
        AgentModels.build({"x": AgentModel(model="nope")}, inventory)
    bad = tmp_path / "agents.yaml"
    bad.write_text("agents:\n  owner: {model: auto, colour: blue}\n")
    with pytest.raises(AgentModelError):
        AgentModels.load(inventory, bad)


def test_cloud_models_are_capped_at_private() -> None:
    from llm_orchestrator import ModelSpec

    base = {
        "id": "paid",
        "provider": "anthropic",
        "model": "m",
        "roles": ["generator"],
        "context_window": 1000,
        "trains_on_inputs": False,
    }
    assert ModelSpec.model_validate({**base, "max_tier": "private"}).max_tier is Tier.PRIVATE
    with pytest.raises(ValueError, match="capped at private"):
        ModelSpec.model_validate({**base, "max_tier": "confidential"})
