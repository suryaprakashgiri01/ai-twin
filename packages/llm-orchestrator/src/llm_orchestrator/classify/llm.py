"""Classification as structured output through the orchestrator."""

from __future__ import annotations

from collections.abc import Mapping

from langchain_core.callbacks import Callbacks
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field, field_validator

from llm_orchestrator.classify.base import Classification, normalise
from llm_orchestrator.orchestrator import Orchestrator
from llm_orchestrator.types import Role, Tier


class _Answer(BaseModel):
    label: str
    confidence: float = Field(ge=0, le=1)
    probabilities: dict[str, float] | None = None


def _answer_schema(labels: frozenset[str]) -> type[_Answer]:
    class Answer(_Answer):
        @field_validator("label")
        @classmethod
        def _known(cls, value: str) -> str:
            if value not in labels:
                raise ValueError(f"label must be one of {sorted(labels)}")
            return value

    return Answer


class LLMClassifier:
    def __init__(
        self,
        orchestrator: Orchestrator,
        *,
        role: Role = Role.CLASSIFIER,
        callbacks: Callbacks = None,
    ) -> None:
        self._orchestrator = orchestrator
        self._role = role
        self._callbacks = callbacks

    async def classify(
        self, text: str, *, question: str, labels: Mapping[str, str], tier: Tier
    ) -> Classification:
        if not labels:
            raise ValueError("labels must not be empty")
        options = "\n".join(f"- {label}: {description}" for label, description in labels.items())
        messages = [
            SystemMessage(
                content=(
                    f"Answer the question about the text by choosing one label.\n"
                    f"Question: {question}\nLabels:\n{options}\n"
                    "Give `label`, your `confidence` (0-1) and `probabilities` per label."
                )
            ),
            HumanMessage(content=text),
        ]
        result = await self._orchestrator.structured_call(
            self._role,
            tier,
            _answer_schema(frozenset(labels)),
            messages,
            callbacks=self._callbacks,
        )
        answer = result.value
        probabilities = (
            normalise(answer.probabilities, labels)
            if answer.probabilities
            else {label: float(label == answer.label) for label in labels}
        )
        return Classification(
            label=answer.label,
            probabilities=probabilities,
            confidence=answer.confidence,
            model=result.model_id,
        )
