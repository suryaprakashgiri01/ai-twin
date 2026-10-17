"""Fakes for orchestrator tests: scripted chat models and small inventories. No network."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from langchain_core.callbacks import AsyncCallbackManagerForLLMRun, CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field
from tenacity import wait_none

from llm_orchestrator import (
    Inventory,
    ModelSpec,
    Orchestrator,
    OrchestratorSettings,
    OverrideStore,
    ProviderSecrets,
)

SECRET_PROMPT = "PROMPT-CANARY my private diary entry"
SECRET_REPLY = "REPLY-CANARY the private answer"


class ScriptedModel(BaseChatModel):
    """Plays back a script: a string is a reply, an exception is raised. Counts calls."""

    script: list[Any]
    calls: int = 0
    seen: list[list[BaseMessage]] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def _next(self, messages: list[BaseMessage]) -> ChatResult:
        self.seen.append(list(messages))
        step = self.script[min(self.calls, len(self.script) - 1)]
        self.calls += 1
        if isinstance(step, BaseException):
            raise step
        message = AIMessage(
            content=step,
            usage_metadata={"input_tokens": 3, "output_tokens": 2, "total_tokens": 5},
        )
        return ChatResult(generations=[ChatGeneration(message=message)])

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        return self._next(messages)

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        return self._next(messages)


class HTTPStatusFailure(Exception):
    """Looks like an SDK status error: carries `status_code`."""

    def __init__(self, status_code: int) -> None:
        super().__init__(f"status {status_code}")
        self.status_code = status_code


def spec(
    model_id: str,
    *,
    provider: str = "mock",
    roles: Sequence[str] = ("router", "classifier", "generator", "judge"),
    max_tier: str = "private",
    trains: bool = False,
    tags: Sequence[str] = (),
    enabled: bool = True,
) -> dict[str, Any]:
    return {
        "id": model_id,
        "provider": provider,
        "model": f"{model_id}-name",
        "roles": list(roles),
        "max_tier": max_tier,
        "context_window": 8192,
        "trains_on_inputs": trains,
        "tags": list(tags),
        "enabled": enabled,
    }


def inventory(*specs: dict[str, Any], chains: dict[str, list[str]] | None = None) -> Inventory:
    ids = [s["id"] for s in specs]
    roles = chains or {
        role: [s["id"] for s in specs if role in s["roles"]]
        for role in ("router", "classifier", "generator", "judge")
    }
    assert ids
    return Inventory.from_mapping({"version": 1, "models": list(specs), "roles": roles})


class Factory:
    """Model factory that hands out scripted models by spec id and records what was built."""

    def __init__(self, scripts: dict[str, list[Any]]) -> None:
        self.models = {mid: ScriptedModel(script=s) for mid, s in scripts.items()}
        self.built: list[str] = []

    def __call__(self, model_spec: ModelSpec) -> BaseChatModel:
        self.built.append(model_spec.id)
        return self.models[model_spec.id]

    def calls(self, model_id: str) -> int:
        return self.models[model_id].calls if model_id in self.models else 0


def orchestrator(
    inv: Inventory,
    factory: Factory,
    *,
    store: OverrideStore | None = None,
    max_attempts: int = 3,
) -> Orchestrator:
    # Every provider in these tests is "mock", so availability is the mock switch.
    settings = OrchestratorSettings(
        secrets=ProviderSecrets(mock_enabled=True), timeout_seconds=5, max_attempts=max_attempts
    )
    return Orchestrator.from_config(inv, settings, store, factory=factory, wait=wait_none())
