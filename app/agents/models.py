"""Per-agent model choice from `config/agents.yaml`: auto mode, pins, and the tier rule.

Each agent names a `model` and a `confidential_model`, each `auto` or an inventory id, plus
an optional `prefer` for auto mode. For one step at `tier`:

1. A request's own `model` wins (the orchestrator still refuses it above its tier).
2. A pinned id is used when its `max_tier` allows `tier`.
3. Otherwise auto: the role chain, ordered by the agent's `prefer`.

Loading refuses a `confidential_model` that is not a local model, so a mistake in the file can
never route confidential text to a cloud API.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict

from llm_orchestrator import Inventory, ModelSpec, Need, Prefer, Profiler, Tier
from llm_orchestrator.gateway import Complexity
from llm_orchestrator.types import LOCAL_PROVIDERS

AgentName = Literal["owner", "prep", "super", "intake"]
AUTO = "auto"
DEFAULT_AGENTS = Path(__file__).resolve().parents[2] / "config" / "agents.yaml"


class AgentModelError(ValueError):
    """config/agents.yaml names an unknown model, or a cloud model for confidential steps."""


class AgentModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    model: str = AUTO
    prefer: Prefer | None = None
    confidential_model: str = AUTO
    confidential_prefer: Prefer | None = None
    # The gateway's complexity for this agent's steps; unset means profile each request.
    need: Complexity | None = None


class ModelChoice(BaseModel):
    """One agent's resolved choice; `pick` is called per step with that step's tier."""

    model_config = ConfigDict(frozen=True)

    agent: str
    model: str | None = None
    model_tier: Tier | None = None
    prefer: Prefer | None = None
    confidential_model: str | None = None
    confidential_prefer: Prefer | None = None
    need: Complexity | None = None

    async def need_for(self, text: str, tier: Tier, profiler: Profiler | None) -> Need | None:
        """The configured need, else the profiler's reading of `text`, else none (chain order)."""
        if self.need is not None:
            return Need(complexity=self.need, reasoning=self.need == "complex", source="caller")
        if profiler is None:
            return None
        return await profiler.profile(text, tier)

    def pick(
        self, tier: Tier, requested: str | None = None, requested_prefer: Prefer | None = None
    ) -> tuple[str | None, Prefer | None]:
        """(model id or None for auto, prefer) for a step at `tier`."""
        if requested is not None:
            return requested, requested_prefer
        if tier >= Tier.CONFIDENTIAL:
            return self.confidential_model, requested_prefer or self.confidential_prefer
        if self.model is not None and self.model_tier is not None and tier <= self.model_tier:
            return self.model, requested_prefer or self.prefer
        return None, requested_prefer or self.prefer


AUTO_CHOICE = ModelChoice(agent="default")


class AgentModels:
    def __init__(self, choices: dict[str, ModelChoice] | None = None) -> None:
        self._choices = choices or {}

    @classmethod
    def empty(cls) -> AgentModels:
        """Auto mode for every agent (tests and callers without a config file)."""
        return cls()

    @classmethod
    def build(cls, agents: dict[str, AgentModel], inventory: Inventory) -> AgentModels:
        return cls({name: _resolve(name, a, inventory) for name, a in agents.items()})

    @classmethod
    def load(cls, inventory: Inventory, path: Path = DEFAULT_AGENTS) -> AgentModels:
        if not path.exists():
            return cls()
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        raw = data.get("agents") or {}
        try:
            agents = {str(k): AgentModel.model_validate(v or {}) for k, v in raw.items()}
        except ValueError as exc:
            raise AgentModelError(f"{path}: {exc}") from exc
        return cls.build(agents, inventory)

    def choice(self, agent: AgentName) -> ModelChoice:
        return self._choices.get(agent, AUTO_CHOICE.model_copy(update={"agent": agent}))


def _resolve(name: str, agent: AgentModel, inventory: Inventory) -> ModelChoice:
    model = None if agent.model == AUTO else _spec(name, "model", agent.model, inventory)
    confidential = None
    if agent.confidential_model != AUTO:
        spec = _spec(name, "confidential_model", agent.confidential_model, inventory)
        if spec.provider not in LOCAL_PROVIDERS:
            raise AgentModelError(
                f"agents.{name}.confidential_model {spec.id} is a cloud model; confidential "
                "text may only go to a local model"
            )
        confidential = spec.id
    return ModelChoice(
        agent=name,
        model=model.id if model else None,
        model_tier=model.max_tier if model else None,
        prefer=agent.prefer,
        confidential_model=confidential,
        confidential_prefer=agent.confidential_prefer,
        need=agent.need,
    )


def _spec(agent: str, key: str, model_id: str, inventory: Inventory) -> ModelSpec:
    spec = inventory.models.get(model_id)
    if spec is None:
        raise AgentModelError(f"agents.{agent}.{key}: {model_id} is not in config/models.yaml")
    return spec
