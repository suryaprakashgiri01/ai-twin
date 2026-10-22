"""Public facade: `chat`, `structured` and `chat_model_for`."""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass

import structlog
from langchain_core.callbacks import Callbacks
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from pydantic import BaseModel, ValidationError
from tenacity.wait import wait_base

from llm_orchestrator.cooloff import MissingModels
from llm_orchestrator.executor import CallContext, Executor, usage_of
from llm_orchestrator.gateway import Need
from llm_orchestrator.hooks import exhausted
from llm_orchestrator.inventory import Inventory
from llm_orchestrator.overrides import OverrideStore
from llm_orchestrator.policy import rejection
from llm_orchestrator.providers import ModelFactory, ProviderSecrets, default_factory, is_available
from llm_orchestrator.selection import Selection, select
from llm_orchestrator.types import (
    CallResult,
    ModelSpec,
    Prefer,
    Role,
    StructuredResult,
    Tier,
)

logger = structlog.get_logger(__name__)

_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)


@dataclass(frozen=True)
class OrchestratorSettings:
    secrets: ProviderSecrets
    timeout_seconds: float = 30.0
    max_attempts: int = 3
    # Models tagged `local` (Ollama on a laptop CPU) can need minutes for a long prompt; None
    # means they share timeout_seconds.
    local_timeout_seconds: float | None = None

    def timeout_for(self, spec: ModelSpec) -> float:
        if self.local_timeout_seconds is not None and "local" in spec.tags:
            return self.local_timeout_seconds
        return self.timeout_seconds


class Orchestrator:
    def __init__(
        self,
        inventory: Inventory,
        settings: OrchestratorSettings,
        override_store: OverrideStore | None = None,
        *,
        factory: ModelFactory | None = None,
        wait: wait_base | None = None,
    ) -> None:
        self._inventory = inventory
        self._settings = settings
        self._store = override_store
        self._factory = factory or default_factory(settings.secrets, settings.timeout_for)
        # Models that answered 404 (a retired id, an Ollama model not pulled) sit out a while.
        self.missing = MissingModels()
        self._executor = Executor(
            self._factory,
            max_attempts=settings.max_attempts,
            timeout_seconds=settings.timeout_seconds,
            timeout_for=settings.timeout_for,
            wait=wait,
            missing=self.missing,
        )

    def timeout_for(self, spec: ModelSpec) -> float:
        """Seconds a call (or a gap between streamed chunks) to this model may take."""
        return self._settings.timeout_for(spec)

    @classmethod
    def from_config(
        cls,
        inventory: Inventory,
        settings: OrchestratorSettings,
        override_store: OverrideStore | None = None,
        *,
        factory: ModelFactory | None = None,
        wait: wait_base | None = None,
    ) -> Orchestrator:
        return cls(inventory, settings, override_store, factory=factory, wait=wait)

    @property
    def inventory(self) -> Inventory:
        return self._inventory

    def _available(self, spec: ModelSpec) -> bool:
        return is_available(spec, self._settings.secrets) and not self.missing.cooling(spec.id)

    async def select(
        self,
        role: Role,
        tier: Tier,
        *,
        model: str | None = None,
        prefer: Prefer | None = None,
        privileged: bool = False,
        need: Need | None = None,
    ) -> Selection:
        effective = await self._inventory.effective(self._store)
        return select(
            effective,
            role,
            tier,
            self._available,
            model=model,
            prefer=prefer,
            privileged=privileged,
            need=need,
        )

    async def chat(
        self,
        role: Role,
        tier: Tier,
        messages: Sequence[BaseMessage],
        *,
        model: str | None = None,
        prefer: Prefer | None = None,
        privileged: bool = False,
        need: Need | None = None,
        callbacks: Callbacks = None,
        max_tokens: int | None = None,
    ) -> CallResult:
        selection = await self.select(
            role, tier, model=model, prefer=prefer, privileged=privileged, need=need
        )
        ctx = CallContext(role=role, tier=tier, callbacks=callbacks, max_tokens=max_tokens)
        for index, spec in enumerate(selection.models):
            try:
                message = await self._executor.run(spec, messages, ctx)
            except Exception:
                continue  # the executor logged and recorded every attempt
            return CallResult(
                text=message.text,
                model_id=spec.id,
                attempts=ctx.attempts,
                fallback_used=index > 0,
                usage=usage_of(message),
            )
        raise exhausted(role, tier, ctx.attempts)

    async def structured_call[T: BaseModel](
        self,
        role: Role,
        tier: Tier,
        schema: type[T],
        messages: Sequence[BaseMessage],
        *,
        model: str | None = None,
        prefer: Prefer | None = None,
        privileged: bool = False,
        need: Need | None = None,
        callbacks: Callbacks = None,
        max_tokens: int | None = None,
    ) -> StructuredResult[T]:
        """Validated `schema` instance. One repair per model on invalid output, then next model."""
        selection = await self.select(
            role, tier, model=model, prefer=prefer, privileged=privileged, need=need
        )
        ctx = CallContext(role=role, tier=tier, callbacks=callbacks, max_tokens=max_tokens)
        prompt = [SystemMessage(content=_schema_instruction(schema)), *messages]
        for index, spec in enumerate(selection.models):
            value = await self._structured_on(spec, schema, prompt, ctx)
            if value is not None:
                return StructuredResult[T](
                    value=value, model_id=spec.id, attempts=ctx.attempts, fallback_used=index > 0
                )
        raise exhausted(role, tier, ctx.attempts)

    async def structured[T: BaseModel](
        self,
        role: Role,
        tier: Tier,
        schema: type[T],
        messages: Sequence[BaseMessage],
        *,
        model: str | None = None,
        prefer: Prefer | None = None,
        privileged: bool = False,
        need: Need | None = None,
        callbacks: Callbacks = None,
        max_tokens: int | None = None,
    ) -> T:
        result = await self.structured_call(
            role,
            tier,
            schema,
            messages,
            model=model,
            prefer=prefer,
            privileged=privileged,
            callbacks=callbacks,
            max_tokens=max_tokens,
            need=need,
        )
        return result.value

    async def _structured_on[T: BaseModel](
        self, spec: ModelSpec, schema: type[T], prompt: list[BaseMessage], ctx: CallContext
    ) -> T | None:
        conversation = list(prompt)
        for repairing in (False, True):
            try:
                message = await self._executor.run(spec, conversation, ctx)
            except Exception:
                return None
            value, error = _parse(schema, message.text)
            if value is not None:
                return value
            self._mark_invalid(spec, ctx)
            if repairing:
                return None
            conversation += [
                AIMessage(content=message.text),
                HumanMessage(content=_repair_instruction(error)),
            ]
        return None  # pragma: no cover

    def _mark_invalid(self, spec: ModelSpec, ctx: CallContext) -> None:
        # The attempt line already went out as "ok"; this one names the parse failure only.
        last = ctx.attempts[-1]
        ctx.attempts[-1] = last.model_copy(update={"outcome": "invalid_output"})
        logger.warning(
            "llm output invalid",
            role=ctx.role.value,
            tier=ctx.tier.label,
            model=spec.id,
            attempt=last.number,
            error_type="ValidationError",
        )

    async def chat_model_for(
        self,
        role: Role,
        tier: Tier,
        *,
        model: str | None = None,
        prefer: Prefer | None = None,
        privileged: bool = False,
        need: Need | None = None,
    ) -> tuple[ModelSpec, BaseChatModel]:
        """The first eligible model as a LangChain chat model, for LangGraph nodes.

        The caller owns retries and fallbacks for a model used this way.
        """
        selection = await self.select(
            role, tier, model=model, prefer=prefer, privileged=privileged, need=need
        )
        spec = selection.models[0]
        return spec, self._executor.model_for(spec)

    async def chat_models(
        self,
        role: Role,
        tier: Tier,
        *,
        model: str | None = None,
        prefer: Prefer | None = None,
        privileged: bool = False,
        need: Need | None = None,
    ) -> list[tuple[ModelSpec, BaseChatModel]]:
        """Every eligible model, best first, for callers that stream and own their fallbacks."""
        selection = await self.select(
            role, tier, model=model, prefer=prefer, privileged=privileged, need=need
        )
        return [(spec, self._executor.model_for(spec)) for spec in selection.models]

    async def model_counts(self) -> dict[str, int]:
        """Usable models (enabled and configured) per role, at the public tier."""
        effective = await self._inventory.effective(self._store)
        return {
            role.value: sum(
                1
                for spec in effective.chain(role)
                if rejection(spec, role, Tier.PUBLIC, self._available) is None
            )
            for role in Role
        }


def _schema_instruction(schema: type[BaseModel]) -> str:
    return (
        "Reply with a single JSON object and nothing else. It must validate against this "
        f"JSON Schema:\n{json.dumps(schema.model_json_schema())}"
    )


def _repair_instruction(error: ValidationError | ValueError | None) -> str:
    if isinstance(error, ValidationError):
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or '<root>'}: {e['msg']}" for e in error.errors()
        )
    else:
        problems = "the reply was not a JSON object"
    return f"That reply was invalid ({problems}). Reply again with only the corrected JSON object."


def _parse[T: BaseModel](
    schema: type[T], text: str
) -> tuple[T | None, ValidationError | ValueError | None]:
    fenced = _FENCE.match(text)
    candidate = fenced.group(1) if fenced else text
    start, end = candidate.find("{"), candidate.rfind("}")
    if start == -1 or end < start:
        return None, ValueError("no JSON object")
    try:
        return schema.model_validate_json(candidate[start : end + 1]), None
    except ValidationError as exc:
        return None, exc
