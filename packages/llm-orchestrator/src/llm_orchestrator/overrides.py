"""The override layer on top of the inventory file.

Overrides can only tighten safety: they may disable a model, lower its `max_tier` or reorder a role
chain. The file is the ceiling, so raising `max_tier` is refused (see `Inventory.check_override`).
kb-storage provides a MongoDB-backed store; this module ships the protocol and an in-memory store.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Protocol

from pydantic import BaseModel, ConfigDict, field_validator

from llm_orchestrator.types import Role, Tier

if TYPE_CHECKING:
    from llm_orchestrator.inventory import Inventory


class ModelOverride(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool | None = None
    max_tier: Tier | None = None

    @field_validator("max_tier", mode="before")
    @classmethod
    def _parse_tier(cls, value: object) -> Tier | None:
        return None if value is None else Tier.parse(value)


class OverrideStore(Protocol):
    """Read side of the override layer. Read on every call, so changes apply to the next call."""

    async def model_overrides(self) -> Mapping[str, ModelOverride]: ...

    async def role_chains(self) -> Mapping[Role, Sequence[str]]: ...


class InMemoryOverrideStore:
    """Process-local overrides. Validates writes against the inventory when given one."""

    def __init__(self, inventory: Inventory | None = None) -> None:
        self._inventory = inventory
        self._models: dict[str, ModelOverride] = {}
        self._chains: dict[Role, list[str]] = {}

    async def model_overrides(self) -> Mapping[str, ModelOverride]:
        return dict(self._models)

    async def role_chains(self) -> Mapping[Role, Sequence[str]]:
        return {role: list(chain) for role, chain in self._chains.items()}

    def set_model(self, model_id: str, override: ModelOverride) -> None:
        if self._inventory is not None:
            self._inventory.check_override(model_id, override)
        self._models[model_id] = override

    def set_chain(self, role: Role, model_ids: Sequence[str]) -> None:
        if self._inventory is not None:
            self._inventory.check_chain(role, model_ids)
        self._chains[role] = list(model_ids)

    def clear(self) -> None:
        self._models.clear()
        self._chains.clear()
