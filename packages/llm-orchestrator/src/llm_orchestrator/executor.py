"""Run one model with retries on transient failures, logging one line per attempt.

Prompt and completion content is never logged: a line carries role, tier, model, outcome,
latency, token counts and the error's type and status code only.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx
import structlog
from langchain_core.callbacks import Callbacks
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.runnables import RunnableConfig
from tenacity import AsyncRetrying, retry_if_exception, stop_after_attempt, wait_random_exponential
from tenacity.wait import wait_base

from llm_orchestrator.hooks import AttemptEvent, notify_attempt
from llm_orchestrator.providers import ModelFactory
from llm_orchestrator.types import (
    Attempt,
    ModelSpec,
    Provider,
    ProviderError,
    Role,
    Tier,
    TokenUsage,
)

logger = structlog.get_logger(__name__)

_TRANSIENT_STATUSES = frozenset({408, 409, 425, 429})
# Exception class names from provider SDKs that mean "try again later".
_TRANSIENT_NAME_HINTS = ("Timeout", "Connection", "RateLimit", "ResourceExhausted", "Unavailable")


def _status_of(exc: BaseException) -> int | None:
    for source in (exc, getattr(exc, "response", None)):
        for attr in ("status_code", "code", "status"):
            value = getattr(source, attr, None)
            if isinstance(value, int) and not isinstance(value, bool) and 100 <= value < 600:
                return value
    return None


def classify_error(exc: BaseException) -> tuple[bool, int | None]:
    """(transient, status_code). Timeouts, 429 and 5xx are transient; other 4xx are permanent."""
    if isinstance(exc, ProviderError):
        return exc.transient, exc.status_code
    if isinstance(exc, TimeoutError | ConnectionError | httpx.TransportError):
        return True, None
    status = _status_of(exc)
    if status is not None:
        return status in _TRANSIENT_STATUSES or status >= 500, status
    name = type(exc).__name__
    return any(hint in name for hint in _TRANSIENT_NAME_HINTS), None


def is_transient(exc: BaseException) -> bool:
    return classify_error(exc)[0]


def usage_of(message: AIMessage) -> TokenUsage:
    meta = message.usage_metadata
    if not meta:
        return TokenUsage()
    return TokenUsage(input_tokens=meta["input_tokens"], output_tokens=meta["output_tokens"])


def limit_kwargs(spec: ModelSpec, max_tokens: int | None) -> dict[str, Any]:
    """The output-token limit as each provider's chat model takes it at call time.

    Ollama's client rejects `max_tokens` and wants `options.num_predict`; Gemini names it
    `max_output_tokens`; the offline mock takes none.
    """
    if max_tokens is None:
        return {}
    match spec.provider:
        case Provider.OLLAMA:
            return {"options": {"num_predict": max_tokens}}
        case Provider.GEMINI:
            return {"max_output_tokens": max_tokens}
        case Provider.MOCK:
            return {}
        case _:
            return {"max_tokens": max_tokens}


@dataclass
class CallContext:
    """Per-call state shared across the models of one chain."""

    role: Role
    tier: Tier
    callbacks: Callbacks = None
    max_tokens: int | None = None
    attempts: list[Attempt] = field(default_factory=list)


class Executor:
    def __init__(
        self,
        factory: ModelFactory,
        *,
        max_attempts: int,
        timeout_seconds: float,
        timeout_for: Callable[[ModelSpec], float] | None = None,
        wait: wait_base | None = None,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        self._factory = factory
        self._models: dict[str, BaseChatModel] = {}
        self._max_attempts = max_attempts
        self._timeout = timeout_seconds
        self._timeout_for = timeout_for
        self._wait = wait or wait_random_exponential(multiplier=0.5, max=8)

    def model_for(self, spec: ModelSpec) -> BaseChatModel:
        """Build each model once; specs only change in enabled/max_tier, which policy reads."""
        if spec.id not in self._models:
            self._models[spec.id] = self._factory(spec)
        return self._models[spec.id]

    async def run(
        self, spec: ModelSpec, messages: Sequence[BaseMessage], ctx: CallContext
    ) -> AIMessage:
        """Call `spec`, retrying transient failures. Raises the last error when it gives up."""
        model = self.model_for(spec)
        retrying = AsyncRetrying(
            stop=stop_after_attempt(self._max_attempts),
            wait=self._wait,
            retry=retry_if_exception(is_transient),
            reraise=True,
        )
        async for attempt in retrying:
            with attempt:
                number = attempt.retry_state.attempt_number
                return await self._once(model, spec, messages, ctx, number)
        raise AssertionError("unreachable: tenacity re-raises the last error")  # pragma: no cover

    async def _once(
        self,
        model: BaseChatModel,
        spec: ModelSpec,
        messages: Sequence[BaseMessage],
        ctx: CallContext,
        number: int,
    ) -> AIMessage:
        config: RunnableConfig = {
            "callbacks": ctx.callbacks,
            "metadata": {
                "llm_role": ctx.role.value,
                "llm_tier": ctx.tier.label,
                "llm_model": spec.id,
                # Read by tracing callbacks (Langfuse) as the generation's model name.
                "ls_model_name": spec.model,
            },
        }
        kwargs = limit_kwargs(spec, ctx.max_tokens)
        started = time.perf_counter()
        try:
            seconds = self._timeout_for(spec) if self._timeout_for else self._timeout
            async with asyncio.timeout(seconds):
                message = await model.ainvoke(list(messages), config=config, **kwargs)
        except Exception as exc:
            transient, status = classify_error(exc)
            self._record(
                spec,
                ctx,
                number,
                started,
                outcome="transient_error" if transient else "permanent_error",
                error_type=type(exc).__name__,
                status_code=status,
            )
            raise
        self._record(spec, ctx, number, started, outcome="ok", usage=usage_of(message))
        return message

    def _record(
        self,
        spec: ModelSpec,
        ctx: CallContext,
        number: int,
        started: float,
        *,
        outcome: Any,
        usage: TokenUsage | None = None,
        error_type: str | None = None,
        status_code: int | None = None,
    ) -> None:
        latency_ms = round((time.perf_counter() - started) * 1000, 1)
        ctx.attempts.append(
            Attempt(
                model_id=spec.id,
                number=number,
                outcome=outcome,
                latency_ms=latency_ms,
                error_type=error_type,
                status_code=status_code,
            )
        )
        log_attempt(
            ctx,
            spec,
            number=number,
            outcome=outcome,
            latency_ms=latency_ms,
            usage=usage,
            error_type=error_type,
            status_code=status_code,
        )


def log_attempt(
    ctx: CallContext,
    spec: ModelSpec,
    *,
    number: int,
    outcome: str,
    latency_ms: float,
    usage: TokenUsage | None = None,
    error_type: str | None = None,
    status_code: int | None = None,
) -> None:
    usage = usage or TokenUsage()
    method = logger.info if outcome == "ok" else logger.warning
    method(
        "llm attempt",
        role=ctx.role.value,
        tier=ctx.tier.label,
        model=spec.id,
        provider=spec.provider.value,
        attempt=number,
        outcome=outcome,
        latency_ms=latency_ms,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        error_type=error_type,
        status_code=status_code,
    )
    notify_attempt(
        AttemptEvent(
            role=ctx.role.value,
            tier=ctx.tier.label,
            model_id=spec.id,
            provider=spec.provider.value,
            number=number,
            outcome=outcome,
            latency_ms=latency_ms,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            error_type=error_type,
        )
    )
