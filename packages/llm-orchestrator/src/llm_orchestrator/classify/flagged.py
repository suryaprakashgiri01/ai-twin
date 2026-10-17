"""Jev for public text when `FF_JEV_ENABLED` is on; the LLM classifier otherwise."""

from __future__ import annotations

from collections.abc import Callable, Mapping

import structlog

from llm_orchestrator.classify.base import Classification, Classifier
from llm_orchestrator.classify.jev import JEV_MAX_TIER
from llm_orchestrator.types import Tier

logger = structlog.get_logger(__name__)


class FlaggedClassifier:
    def __init__(
        self,
        llm: Classifier,
        jev: Classifier | None,
        *,
        enabled: bool | Callable[[], bool],
    ) -> None:
        self._llm = llm
        self._jev = jev
        # A callable lets a runtime flag source be read on every call.
        self._enabled = enabled if callable(enabled) else (lambda: enabled)

    def uses_jev(self, tier: Tier) -> bool:
        return self._jev is not None and tier <= JEV_MAX_TIER and self._enabled()

    async def classify(
        self, text: str, *, question: str, labels: Mapping[str, str], tier: Tier
    ) -> Classification:
        if self._jev is not None and self.uses_jev(tier):
            try:
                return await self._jev.classify(text, question=question, labels=labels, tier=tier)
            except Exception as exc:
                logger.warning(
                    "jev failed, using llm classifier",
                    error_type=type(exc).__name__,
                    status_code=getattr(exc, "status_code", None),
                )
        return await self._llm.classify(text, question=question, labels=labels, tier=tier)
