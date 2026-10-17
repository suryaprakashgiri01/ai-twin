from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from pydantic import BaseModel, Field

from llm_orchestrator.types import Tier


class Classification(BaseModel):
    label: str
    probabilities: dict[str, float]
    confidence: float = Field(ge=0, le=1)
    model: str


class Classifier(Protocol):
    async def classify(
        self, text: str, *, question: str, labels: Mapping[str, str], tier: Tier
    ) -> Classification:
        """Pick one of `labels` (label -> description) for `text`."""
        ...


def normalise(probabilities: Mapping[str, float], labels: Mapping[str, str]) -> dict[str, float]:
    """Probabilities over exactly `labels`, summing to 1 (unknown labels dropped)."""
    kept = {label: max(0.0, float(probabilities.get(label, 0.0))) for label in labels}
    total = sum(kept.values())
    if total <= 0:
        return {label: 1 / len(labels) for label in labels}
    return {label: p / total for label, p in kept.items()}
