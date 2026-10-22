"""Build LangChain chat models from specs. The only module that knows provider classes.

SDK-level retries are switched off: the executor owns retries so it can count attempts, tell
transient errors from permanent ones and fall back across providers.
"""

from __future__ import annotations

import importlib
import importlib.util
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import SecretStr

from llm_orchestrator.types import ModelSpec, Provider

CEREBRAS_BASE_URL = "https://api.cerebras.ai/v1"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

ModelFactory = Callable[[ModelSpec], BaseChatModel]


@dataclass(frozen=True)
class ProviderSecrets:
    """Keys and endpoints. A provider without its key or endpoint is treated as disabled."""

    groq_api_key: SecretStr | None = None
    cerebras_api_key: SecretStr | None = None
    gemini_api_key: SecretStr | None = None
    openrouter_api_key: SecretStr | None = None
    anthropic_api_key: SecretStr | None = None
    openai_api_key: SecretStr | None = None
    ollama_base_url: str | None = None
    # Ollama spec id -> model name that replaces the spec's, so any locally pulled tag can be used.
    ollama_models: Mapping[str, str] = field(default_factory=dict)
    mock_enabled: bool = False


def _installed(module: str) -> bool:
    return importlib.util.find_spec(module) is not None


def _present(secret: SecretStr | None) -> bool:
    return secret is not None and bool(secret.get_secret_value().strip())


def is_available(spec: ModelSpec, secrets: ProviderSecrets) -> bool:
    match spec.provider:
        case Provider.GROQ:
            return _present(secrets.groq_api_key)
        case Provider.CEREBRAS:
            return _present(secrets.cerebras_api_key)
        case Provider.GEMINI:
            return _present(secrets.gemini_api_key)
        case Provider.OPENROUTER:
            return _present(secrets.openrouter_api_key)
        case Provider.OLLAMA:
            return bool(secrets.ollama_base_url)
        case Provider.MOCK:
            return secrets.mock_enabled
        case Provider.ANTHROPIC:
            # An optional extra: `uv add --package llm-orchestrator langchain-anthropic`.
            return _present(secrets.anthropic_api_key) and _installed("langchain_anthropic")
        case Provider.OPENAI:
            return _present(secrets.openai_api_key)


class MockChatModel(BaseChatModel):
    """Deterministic, offline stand-in for keyless local runs. Never echoes its input."""

    reply: str = "mock response"

    @property
    def _llm_type(self) -> str:
        return "llm-orchestrator-mock"

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=self.reply))])


def _key(secret: SecretStr | None) -> SecretStr:
    if not _present(secret):
        raise ValueError("provider key missing; the model should have been filtered out")
    assert secret is not None
    return secret


def build_chat_model(spec: ModelSpec, secrets: ProviderSecrets, timeout: float) -> BaseChatModel:
    """A LangChain chat model for `spec`. Imports each integration only when it is used."""
    match spec.provider:
        case Provider.GROQ:
            from langchain_groq import ChatGroq

            return ChatGroq(
                model_name=spec.model,
                api_key=_key(secrets.groq_api_key),
                timeout=timeout,
                max_retries=0,
            )
        case Provider.CEREBRAS:
            from langchain_cerebras import ChatCerebras

            return ChatCerebras(
                model_name=spec.model,
                api_key=_key(secrets.cerebras_api_key),
                base_url=spec.base_url or CEREBRAS_BASE_URL,
                timeout=timeout,
                max_retries=0,
            )
        case Provider.GEMINI:
            from langchain_google_genai import ChatGoogleGenerativeAI

            return ChatGoogleGenerativeAI(
                model=spec.model,
                api_key=_key(secrets.gemini_api_key),
                timeout=timeout,
                retries=0,
            )
        case Provider.OPENROUTER:
            from langchain_openai import ChatOpenAI

            return ChatOpenAI(
                model=spec.model,
                api_key=_key(secrets.openrouter_api_key),
                base_url=spec.base_url or OPENROUTER_BASE_URL,
                timeout=timeout,
                max_retries=0,
            )
        case Provider.OLLAMA:
            from langchain_ollama import ChatOllama

            # A reasoning model's thinking comes back separately, never in the content.
            return ChatOllama(
                model=secrets.ollama_models.get(spec.id) or spec.model,
                base_url=secrets.ollama_base_url,
                client_kwargs={"timeout": timeout},
                reasoning=True if "reasoning" in spec.capabilities else None,
            )
        case Provider.MOCK:
            return MockChatModel()
        case Provider.ANTHROPIC:
            anthropic: Any = importlib.import_module("langchain_anthropic")
            model: BaseChatModel = anthropic.ChatAnthropic(
                model=spec.model,
                api_key=_key(secrets.anthropic_api_key),
                timeout=timeout,
                max_retries=0,
            )
            return model
        case Provider.OPENAI:
            from langchain_openai import ChatOpenAI

            return ChatOpenAI(
                model=spec.model,
                api_key=_key(secrets.openai_api_key),
                base_url=spec.base_url,
                timeout=timeout,
                max_retries=0,
            )


def default_factory(
    secrets: ProviderSecrets, timeout: float | Callable[[ModelSpec], float]
) -> ModelFactory:
    """`timeout` is one value for every model, or a function picking one per model."""

    def factory(spec: ModelSpec) -> BaseChatModel:
        seconds = timeout(spec) if callable(timeout) else timeout
        return build_chat_model(spec, secrets, seconds)

    return factory
