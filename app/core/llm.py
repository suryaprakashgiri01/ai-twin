"""Build the model orchestrator and classifier once, from settings and the inventory file."""

from __future__ import annotations

from dataclasses import dataclass, field

from app.agents.models import AgentModels
from app.core.config import Settings
from llm_orchestrator import (
    InMemoryOverrideStore,
    Inventory,
    Orchestrator,
    OrchestratorSettings,
    OverrideStore,
    ProviderSecrets,
)
from llm_orchestrator.classify import Classifier, FlaggedClassifier, JevClassifier, LLMClassifier


@dataclass(frozen=True)
class LLMServices:
    orchestrator: Orchestrator
    classifier: Classifier
    overrides: OverrideStore
    # Per-agent model choice from config/agents.yaml.
    agents: AgentModels = field(default_factory=AgentModels.empty)


def provider_secrets(settings: Settings) -> ProviderSecrets:
    return ProviderSecrets(
        groq_api_key=settings.groq_api_key,
        cerebras_api_key=settings.cerebras_api_key,
        gemini_api_key=settings.gemini_api_key,
        openrouter_api_key=settings.openrouter_api_key,
        anthropic_api_key=settings.anthropic_api_key,
        openai_api_key=settings.openai_api_key,
        ollama_base_url=settings.ollama_base_url or None,
        ollama_models={
            spec_id: model
            for spec_id, model in (
                ("ollama-local", settings.ollama_model),
                ("ollama-qwen", settings.ollama_quality_model),
                ("ollama-qwen-14b", settings.ollama_max_model),
            )
            if model
        },
        mock_enabled=settings.llm_mock_enabled,
    )


def build_llm(settings: Settings, overrides: OverrideStore | None = None) -> LLMServices:
    """`overrides` is read on every call; the app passes its MongoDB-backed adapter."""
    inventory = Inventory.from_yaml(settings.model_inventory_path)
    overrides = overrides or InMemoryOverrideStore(inventory)
    orchestrator = Orchestrator.from_config(
        inventory,
        OrchestratorSettings(
            secrets=provider_secrets(settings),
            timeout_seconds=settings.llm_timeout_seconds,
            max_attempts=settings.llm_max_attempts,
            local_timeout_seconds=settings.llm_local_timeout_seconds,
        ),
        overrides,
    )
    key = settings.typesafe_api_key
    jev = (
        JevClassifier(
            key,
            model=settings.jev_model,
            timeout_seconds=settings.llm_timeout_seconds,
            max_attempts=settings.llm_max_attempts,
        )
        if key is not None and key.get_secret_value().strip()
        else None
    )
    classifier = FlaggedClassifier(
        LLMClassifier(orchestrator), jev, enabled=lambda: settings.ff_jev_enabled
    )
    agents = AgentModels.load(inventory, settings.agent_models_path)
    return LLMServices(
        orchestrator=orchestrator, classifier=classifier, overrides=overrides, agents=agents
    )
