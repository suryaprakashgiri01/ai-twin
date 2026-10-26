"""Classifier order: Laya (local, when enabled) → Jev (public text, when enabled) → the LLM
classifier. Each failure falls through to the next, so classification never stops on one."""

from __future__ import annotations

from collections.abc import Callable, Mapping

import structlog

from llm_orchestrator.classify.base import Classification, Classifier
from llm_orchestrator.classify.jev import JEV_MAX_TIER
from llm_orchestrator.classify.laya import LAYA_MAX_TIER
from llm_orchestrator.types import Tier

logger = structlog.get_logger(__name__)


class FlaggedClassifier:
    def __init__(
        self,
        llm: Classifier,
        jev: Classifier | None,
        *,
        enabled: bool | Callable[[], bool],
        laya: Classifier | None = None,
        laya_enabled: bool | Callable[[], bool] = False,
    ) -> None:
        self._llm = llm
        self._jev = jev
        self._laya = laya
        self._laya_enabled = laya_enabled if callable(laya_enabled) else (lambda: laya_enabled)
        # A callable lets a runtime flag source be read on every call.
        self._enabled = enabled if callable(enabled) else (lambda: enabled)

    def uses_jev(self, tier: Tier) -> bool:
        return self._jev is not None and tier <= JEV_MAX_TIER and self._enabled()

    def uses_laya(self, tier: Tier) -> bool:
        return self._laya is not None and tier <= LAYA_MAX_TIER and self._laya_enabled()

    async def classify(
        self, text: str, *, question: str, labels: Mapping[str, str], tier: Tier
    ) -> Classification:
        if self._laya is not None and self.uses_laya(tier):
            try:
                return await self._laya.classify(text, question=question, labels=labels, tier=tier)
            except Exception as exc:
                logger.warning(
                    "laya failed, trying the next classifier", error_type=type(exc).__name__
                )
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
