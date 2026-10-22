"""llm-gateway T2/T3: request needs (rules, Laya) and fit ordering of eligible models."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from llm_orchestrator import ModelSpec, Tier
from llm_orchestrator.classify import Classification
from llm_orchestrator.gateway import Need, Profiler, fit_order, rule_need
from tests.llm_orchestrator.fakes import spec

DIAGNOSE = (
    "If your AI inference latency increases significantly after new features, what's your "
    "step-by-step process to diagnose it?"
)


def model(model_id: str, quality: int, caps: tuple[str, ...] = (), cost: float = 0.0) -> ModelSpec:
    raw: dict[str, Any] = {
        **spec(model_id, max_tier="confidential"),
        "quality": quality,
        "capabilities": list(caps),
        "cost": {"input_per_mtok": cost, "output_per_mtok": cost},
    }
    return ModelSpec.model_validate(raw)


@pytest.mark.parametrize(
    ("text", "complexity", "reasoning"),
    [
        (DIAGNOSE, "complex", True),
        ("thanks", "simple", False),
        ("how many interviews this month", "simple", False),
        (
            "Give me the summary of the projects I worked on at my current company?",
            "moderate",
            False,
        ),
        ("Compare Kafka and RabbitMQ for our event bus", "complex", True),
    ],
)
def test_rule_need(text: str, complexity: str, reasoning: bool) -> None:
    need = rule_need(text)
    assert (need.complexity, need.reasoning, need.source) == (complexity, reasoning, "rules")


class ScriptedLaya:
    def __init__(self, label: str, confidence: float = 0.9, fail: bool = False) -> None:
        self.label, self.confidence, self.fail = label, confidence, fail
        self.calls = 0

    async def classify(
        self, text: str, *, question: str, labels: Mapping[str, str], tier: Tier
    ) -> Classification:
        self.calls += 1
        if self.fail:
            raise RuntimeError("laya down")
        probabilities = {self.label: self.confidence}
        return Classification(
            label=self.label, probabilities=probabilities, confidence=self.confidence, model="laya"
        )


async def test_laya_refines_the_rules_and_failure_keeps_them() -> None:
    laya = ScriptedLaya("complex")
    need = await Profiler(laya, laya_enabled=True).profile("summarise my notes", Tier.CONFIDENTIAL)
    assert (need.complexity, need.reasoning, need.source) == ("complex", True, "laya")
    unsure = Profiler(ScriptedLaya("simple", confidence=0.3), laya_enabled=True)
    assert (await unsure.profile(DIAGNOSE, Tier.PRIVATE)).source == "rules"
    broken = Profiler(ScriptedLaya("simple", fail=True), laya_enabled=True)
    assert (await broken.profile(DIAGNOSE, Tier.PRIVATE)).complexity == "complex"
    off = Profiler(laya, laya_enabled=False)
    assert (await off.profile("thanks", Tier.PRIVATE)).source == "rules" and laya.calls == 1
    restricted = await Profiler(laya, laya_enabled=True).profile("x", Tier.RESTRICTED)
    assert restricted.source == "rules"
    tools = await off.profile("thanks", Tier.PRIVATE, tools=True)
    assert tools.tools is True


def ids(models: list[ModelSpec]) -> list[str]:
    return [m.id for m in models]


def test_fit_order_matches_strength_to_complexity() -> None:
    small = model("small", 1, ("tools",))
    mid = model("mid", 3, ("tools",))
    big = model("big", 5, ("tools", "reasoning"), cost=1.0)
    chain = [mid, big, small]
    assert ids(fit_order(chain, Need(complexity="complex")))[0] == "big"
    assert ids(fit_order(chain, Need(complexity="moderate"))) == ["mid", "big", "small"]
    assert ids(fit_order(chain, Need(complexity="simple")))[0] == "mid"  # 3 is nearest above 2
    assert ids(fit_order([small, big], Need(complexity="simple"))) == ["big", "small"]
    # A reasoning need puts models that can reason first; the rest stay as fallbacks.
    assert ids(fit_order(chain, Need(complexity="moderate", reasoning=True))) == [
        "big",
        "mid",
        "small",
    ]
    assert ids(fit_order([mid, small], Need(reasoning=True))) == ["mid", "small"]
    # Equal fit: cheaper first, then chain order.
    twin = model("twin", 3, ("tools",), cost=0.5)
    assert ids(fit_order([twin, mid], Need(complexity="moderate"))) == ["mid", "twin"]
    assert Need(complexity="complex", reasoning=True).summary() == "complex+reasoning"


async def test_selection_fits_the_need_but_prefer_pins_and_tiers_win() -> None:
    from llm_orchestrator import Role
    from tests.llm_orchestrator.fakes import Factory, inventory, orchestrator

    def entry(model_id: str, quality: int, max_tier: str, tags: tuple[str, ...] = ()) -> Any:
        return {
            **spec(model_id, roles=["generator"], max_tier=max_tier, tags=tags),
            "quality": quality,
            "capabilities": ["tools", "reasoning"] if quality == 5 else ["tools"],
        }

    inv = inventory(
        entry("small", 2, "private", ("fast",)),
        entry("big", 5, "private"),
        entry("local", 3, "confidential", ("local",)),
    )
    orch = orchestrator(inv, Factory({}))

    async def order(tier: Tier, **kw: Any) -> list[str]:
        return [
            m.id for m in (await orch.select(Role.GENERATOR, tier, privileged=True, **kw)).models
        ]

    assert await order(Tier.PRIVATE) == ["small", "big", "local"]  # chain order without a need
    assert (await order(Tier.PRIVATE, need=Need(complexity="complex")))[0] == "big"
    assert (await order(Tier.PRIVATE, need=Need(complexity="simple")))[0] == "small"
    # prefer still wins over the fit.
    complex_fast = await order(Tier.PRIVATE, need=Need(complexity="complex"), prefer="fast")
    assert complex_fast[0] == "small"
    # A pin is a pin.
    assert await order(Tier.PRIVATE, model="local", need=Need(complexity="complex")) == ["local"]
    # Confidential: the cloud models are never eligible, whatever the need.
    assert await order(Tier.CONFIDENTIAL, need=Need(complexity="complex", reasoning=True)) == [
        "local"
    ]


async def test_agent_need_from_config_or_profiler() -> None:
    from app.agents.models import AUTO_CHOICE

    prep = AUTO_CHOICE.model_copy(update={"need": "complex"})
    need = await prep.need_for("thanks", Tier.PRIVATE, Profiler())
    assert need is not None and (need.complexity, need.source) == ("complex", "caller")
    profiled = await AUTO_CHOICE.need_for(DIAGNOSE, Tier.PRIVATE, Profiler())
    assert profiled is not None and profiled.source == "rules" and profiled.reasoning
    assert await AUTO_CHOICE.need_for("thanks", Tier.PRIVATE, None) is None
