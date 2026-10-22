"""Laya adapter: the open-source, local alternative to Jev (`pip install laya`).

Laya answers the same `choice` question shape as Jev (`instructions` plus `criteria`), but runs
on this machine: no text leaves it, so it may classify up to confidential text. Its weights
(~0.8 GB for the English model) download once on first use. The package is optional; without
it every call fails fast and the caller's fallback (the LLM classifier) answers.

Neither the text nor the answer is logged.
"""

from __future__ import annotations

import asyncio
import importlib
import time
from collections.abc import Mapping
from typing import Any

import structlog

from llm_orchestrator.classify.base import Classification, normalise
from llm_orchestrator.types import NoEligibleModelError, ProviderError, Role, Tier

logger = structlog.get_logger(__name__)

LAYA_MAX_TIER = Tier.CONFIDENTIAL
_QUESTION_KEY = "q"


class LayaError(ProviderError):
    """Laya is not installed, failed to load, or gave an unusable answer."""


class LayaClassifier:
    def __init__(self, *, device: str = "cpu", router: Any = None) -> None:
        self._device = device
        self._router = router
        self._lock = asyncio.Lock()

    @property
    def model_id(self) -> str:
        return "laya"

    def _load(self) -> Any:
        try:
            laya = importlib.import_module("laya")
        except ImportError as exc:
            raise LayaError("laya is not installed: uv add laya", transient=False) from exc
        return laya.Router(device=self._device)

    async def _router_ready(self) -> Any:
        async with self._lock:
            if self._router is None:
                self._router = await asyncio.to_thread(self._load)
        return self._router

    async def classify(
        self, text: str, *, question: str, labels: Mapping[str, str], tier: Tier
    ) -> Classification:
        if tier > LAYA_MAX_TIER:
            raise NoEligibleModelError(Role.CLASSIFIER, tier)
        if not labels:
            raise ValueError("labels must not be empty")
        router = await self._router_ready()
        questions = {
            _QUESTION_KEY: {"type": "choice", "instructions": question, "criteria": dict(labels)}
        }
        started = time.perf_counter()
        result = await asyncio.to_thread(router.predict, text, questions)
        logger.info(
            "laya classify",
            latency_ms=round((time.perf_counter() - started) * 1000, 1),
            labels=len(labels),
            tier=tier.label,
        )
        return self._map(result, labels)

    @staticmethod
    def _map(result: Any, labels: Mapping[str, str]) -> Classification:
        try:
            answer = result["answers"][_QUESTION_KEY]
            choice = str(answer["choice"])
            confidence = float(answer.get("answer_confidence", answer.get("confidence", 0.5)))
            model = str((result.get("routing") or {}).get("model") or "default")
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise LayaError("laya answer missing fields", transient=False) from exc
        if choice not in labels:
            raise LayaError("laya chose an unknown label", transient=False)
        confidence = min(max(confidence, 0.0), 1.0)
        return Classification(
            label=choice,
            probabilities=normalise({choice: confidence}, labels),
            confidence=confidence,
            model=f"laya:{model}",
        )
