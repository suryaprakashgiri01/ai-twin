"""The gateway: what a request needs, and which eligible models fit it best.

`Need` describes a request: how complex it is, and whether it needs tools or step-by-step
reasoning. A caller may state it; otherwise `Profiler` derives it from the text: rules first,
then (when the owner enables it) Laya, a local classifier that may read up to confidential text.
`fit_order` reorders an already-eligible chain so the model's strength matches the need; it
never adds a model, so tier policy and availability are untouched.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import TYPE_CHECKING, Literal

import structlog
from pydantic import BaseModel, ConfigDict

from llm_orchestrator.types import ModelSpec, Tier

if TYPE_CHECKING:
    # Type-only: the classify package imports the orchestrator, which imports this module.
    from llm_orchestrator.classify.base import Classifier

logger = structlog.get_logger(__name__)

Complexity = Literal["simple", "moderate", "complex"]
NeedSource = Literal["caller", "rules", "laya"]

# The quality a model should have for each complexity (see `fit_order`).
TARGET_QUALITY: dict[Complexity, int] = {"simple": 2, "moderate": 3, "complex": 5}
LAYA_MIN_CONFIDENCE = 0.5
LAYA_QUESTION = "How hard is it to answer this request well?"
LAYA_LABELS: dict[str, str] = {
    "simple": "a greeting, thanks, a lookup or one short fact",
    "moderate": "a summary, a short explanation or a short piece of writing",
    "complex": "multi-step reasoning, a design, a diagnosis, a comparison or a long structured "
    "answer",
}

_REASONING = re.compile(
    r"\b(step[- ]by[- ]step|diagnos\w*|debug\w*|troubleshoot\w*|root cause|why|compare|"
    r"comparison|trade-?offs?|design|architect\w*|plan(?:ning)?|analy[sz]\w*|prove|calculate|"
    r"estimate|explain how|walk me through|how would you|what would you do)\b",
    re.IGNORECASE,
)
_SMALL_TALK = re.compile(
    r"^\s*(hi|hello|hey|thanks?|thank you|ok(?:ay)?|cool|great|bye|good (?:morning|night))\b",
    re.IGNORECASE,
)
_BULLET = re.compile(r"(?m)^\s*(?:[-*]|\d+[.)])\s")
_LONG_WORDS = 120
_SHORT_WORDS = 12


class Need(BaseModel):
    model_config = ConfigDict(frozen=True)

    complexity: Complexity = "moderate"
    tools: bool = False
    reasoning: bool = False
    source: NeedSource = "caller"

    def summary(self) -> str:
        extras = [name for name, on in (("tools", self.tools), ("reasoning", self.reasoning)) if on]
        return "+".join([self.complexity, *extras])


def rule_need(text: str) -> Need:
    """A need from wording and shape alone: free, instant and predictable."""
    words = len(text.split())
    reasoning = bool(_REASONING.search(text))
    bullets = len(_BULLET.findall(text))
    structured = text.count("?") >= 2 or "```" in text or bullets >= 3
    if reasoning or structured or words > _LONG_WORDS:
        return Need(complexity="complex", reasoning=reasoning, source="rules")
    if _SMALL_TALK.match(text) or words <= _SHORT_WORDS // 2:
        return Need(complexity="simple", source="rules")
    if words <= _SHORT_WORDS and "?" not in text:
        return Need(complexity="simple", source="rules")
    return Need(complexity="moderate", source="rules")


class Profiler:
    """Rules, refined by Laya when it is enabled and confident."""

    def __init__(self, laya: Classifier | None = None, *, laya_enabled: bool = False) -> None:
        self._laya = laya
        self._laya_enabled = laya_enabled

    async def profile(self, text: str, tier: Tier, *, tools: bool = False) -> Need:
        need = rule_need(text)
        if self._laya is not None and self._laya_enabled and tier <= Tier.CONFIDENTIAL:
            try:
                result = await self._laya.classify(
                    text, question=LAYA_QUESTION, labels=LAYA_LABELS, tier=tier
                )
            except Exception as exc:
                logger.warning("laya profile skipped", error_type=type(exc).__name__)
            else:
                if result.confidence >= LAYA_MIN_CONFIDENCE and result.label in TARGET_QUALITY:
                    complexity = result.label
                    need = Need(
                        complexity=complexity,
                        reasoning=need.reasoning or complexity == "complex",
                        source="laya",
                    )
        return need.model_copy(update={"tools": tools}) if tools else need


def capable(spec: ModelSpec, need: Need) -> bool:
    """Has every capability the need requires (text is always there)."""
    return (not need.tools or "tools" in spec.capabilities) and (
        not need.reasoning or "reasoning" in spec.capabilities
    )


def fit_order(models: Sequence[ModelSpec], need: Need) -> list[ModelSpec]:
    """Eligible models best-fit first; every model stays as a fallback.

    Models with every capability the need asks for (tools, reasoning) come first; the rest
    stay at the end, so an unreachable provider never leaves a request without a model. Within
    each group: for a complex need the strongest first; otherwise the closest quality at or
    above the target first, cheaper before dearer. The chain's own order breaks ties (sorting
    is stable).
    """
    target = TARGET_QUALITY[need.complexity]

    def key(spec: ModelSpec) -> tuple[int, int, float]:
        group = 0 if capable(spec, need) else 1
        cost = spec.cost.input_per_mtok + spec.cost.output_per_mtok
        if need.complexity == "complex":
            return (group, -spec.quality, cost)
        shortfall = max(0, target - spec.quality)
        excess = max(0, spec.quality - target)
        return (group, shortfall * 10 + excess, cost)

    return sorted(models, key=key)
