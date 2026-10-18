"""Listeners for what the orchestrator does, for metrics and alerting in the host app.

`on_attempt` hears about every provider call (one per `log_attempt` line); `on_exhausted` hears
when every model in a chain failed, just before `AllModelsFailedError` is raised. Listeners get
names and numbers only, never prompt or completion text. They run inline and must be quick; one
that raises is logged and skipped, so it can never break a model call. Each `on_*` returns a
function that removes the listener.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

import structlog

from llm_orchestrator.types import AllModelsFailedError, Attempt, Role, Tier

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class AttemptEvent:
    role: str
    tier: str
    model_id: str
    provider: str
    number: int
    outcome: str
    latency_ms: float
    input_tokens: int = 0
    output_tokens: int = 0
    error_type: str | None = None


AttemptListener = Callable[[AttemptEvent], None]
ExhaustedListener = Callable[[Role, Tier, Sequence[Attempt]], None]

_attempt_listeners: list[AttemptListener] = []
_exhausted_listeners: list[ExhaustedListener] = []


def on_attempt(listener: AttemptListener) -> Callable[[], None]:
    _attempt_listeners.append(listener)
    return lambda: _remove(_attempt_listeners, listener)


def on_exhausted(listener: ExhaustedListener) -> Callable[[], None]:
    _exhausted_listeners.append(listener)
    return lambda: _remove(_exhausted_listeners, listener)


def _remove[L](listeners: list[L], listener: L) -> None:
    if listener in listeners:
        listeners.remove(listener)


def notify_attempt(event: AttemptEvent) -> None:
    for listener in list(_attempt_listeners):
        try:
            listener(event)
        except Exception as exc:
            logger.warning("attempt listener failed", error_type=type(exc).__name__)


def exhausted(role: Role, tier: Tier, attempts: list[Attempt]) -> AllModelsFailedError:
    """Tell the listeners, then hand back the error for the caller to raise."""
    for listener in list(_exhausted_listeners):
        try:
            listener(role, tier, attempts)
        except Exception as exc:
            logger.warning("exhausted listener failed", error_type=type(exc).__name__)
    return AllModelsFailedError(role, tier, attempts)
