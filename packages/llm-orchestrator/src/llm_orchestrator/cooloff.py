"""Skip a model that answered "not found" (404) for a while instead of failing on every call.

A provider retiring a model id, or an Ollama model that is not pulled, answers 404. Without a
memory, every request pays for that failed call before falling back. `MissingModels` remembers
it per process for `seconds` (10 minutes by default) and says which id to update; after the
cool-off the model is tried again, so a pull or a provider fix is picked up on its own.
"""

from __future__ import annotations

import time
from collections.abc import Callable

import structlog

from llm_orchestrator.types import ModelSpec

logger = structlog.get_logger(__name__)

NOT_FOUND = 404
DEFAULT_SECONDS = 600.0


class MissingModels:
    def __init__(
        self, seconds: float = DEFAULT_SECONDS, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._seconds = seconds
        self._clock = clock
        self._until: dict[str, float] = {}

    def note(self, spec: ModelSpec, status_code: int | None) -> None:
        """Record a failed call; only a 404 starts a cool-off."""
        if status_code != NOT_FOUND:
            return
        first = spec.id not in self._until or not self.cooling(spec.id)
        self._until[spec.id] = self._clock() + self._seconds
        if first:
            logger.warning(
                "model not found; skipped for a while",
                model=spec.id,
                provider=spec.provider.value,
                provider_model=spec.model,
                seconds=self._seconds,
                fix="update the id in config/models.yaml (make models-check), or pull it",
            )

    def cooling(self, model_id: str) -> bool:
        until = self._until.get(model_id)
        if until is None:
            return False
        if self._clock() >= until:
            del self._until[model_id]
            return False
        return True
