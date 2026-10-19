"""Load and validate the model inventory, and merge the override layer on top of it."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog
import yaml
from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from llm_orchestrator.overrides import ModelOverride, OverrideStore
from llm_orchestrator.types import InventoryError, ModelSpec, Role

logger = structlog.get_logger(__name__)


class InventoryFile(BaseModel):
    """Schema of `config/models.yaml`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: int = 1
    models: list[ModelSpec]
    roles: dict[Role, list[str]]

    @model_validator(mode="after")
    def _consistent(self) -> InventoryFile:
        ids = [m.id for m in self.models]
        duplicates = {i for i in ids if ids.count(i) > 1}
        if duplicates:
            raise ValueError(f"duplicate model ids: {sorted(duplicates)}")
        by_id = {m.id: m for m in self.models}
        for role, chain in self.roles.items():
            _validate_chain(role, chain, by_id)
        return self


def _validate_chain(role: Role, chain: Sequence[str], by_id: Mapping[str, ModelSpec]) -> None:
    if len(set(chain)) != len(chain):
        raise ValueError(f"role {role.value}: chain repeats a model")
    for model_id in chain:
        spec = by_id.get(model_id)
        if spec is None:
            raise ValueError(f"role {role.value}: unknown model {model_id!r}")
        if not spec.serves(role):
            raise ValueError(f"role {role.value}: model {model_id!r} does not serve this role")


@dataclass(frozen=True)
class EffectiveInventory:
    """The file merged with the current overrides: what selection works from."""

    models: Mapping[str, ModelSpec]
    roles: Mapping[Role, Sequence[str]]

    def chain(self, role: Role) -> list[ModelSpec]:
        return [self.models[i] for i in self.roles.get(role, ())]


class Inventory:
    """The committed defaults. Immutable; overrides are applied per call by `effective`."""

    def __init__(self, file: InventoryFile) -> None:
        self._file = file
        self._models = {m.id: m for m in file.models}

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> Inventory:
        try:
            return cls(InventoryFile.model_validate(data))
        except ValidationError as exc:
            raise InventoryError(f"invalid model inventory: {exc}") from exc

    @classmethod
    def from_yaml(cls, path: Path) -> Inventory:
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise InventoryError(f"cannot read model inventory {path}: {exc}") from exc
        if not isinstance(data, dict):
            raise InventoryError(f"model inventory {path} must be a mapping")
        return cls.from_mapping(data)

    @property
    def models(self) -> Mapping[str, ModelSpec]:
        return dict(self._models)

    @property
    def roles(self) -> Mapping[Role, Sequence[str]]:
        return {role: list(chain) for role, chain in self._file.roles.items()}

    def check_override(self, model_id: str, override: ModelOverride) -> None:
        """Refuse overrides that would widen exposure. Raises `InventoryError`."""
        spec = self._models.get(model_id)
        if spec is None:
            raise InventoryError(f"override for unknown model {model_id!r}")
        if override.max_tier is not None and override.max_tier > spec.max_tier:
            raise InventoryError(
                f"{model_id}: max_tier may only be lowered "
                f"(file {spec.max_tier.label}, override {override.max_tier.label})"
            )

    def check_chain(self, role: Role, model_ids: Sequence[str]) -> None:
        try:
            _validate_chain(role, model_ids, self._models)
        except ValueError as exc:
            raise InventoryError(str(exc)) from exc

    async def effective(self, store: OverrideStore | None) -> EffectiveInventory:
        if store is None:
            return EffectiveInventory(models=self.models, roles=self.roles)
        model_overrides = await store.model_overrides()
        chain_overrides = await store.role_chains()
        models = {
            mid: self._apply(spec, model_overrides.get(mid)) for mid, spec in self._models.items()
        }
        roles = dict(self.roles)
        for role, chain in chain_overrides.items():
            try:
                self.check_chain(role, chain)
            except InventoryError as exc:
                logger.warning("role chain override ignored", role=role.value, reason=str(exc))
                continue
            roles[role] = list(chain)
        return EffectiveInventory(models=models, roles=roles)

    def _apply(self, spec: ModelSpec, override: ModelOverride | None) -> ModelSpec:
        if override is None:
            return spec
        update: dict[str, Any] = {}
        if override.enabled is not None:
            update["enabled"] = override.enabled
        if override.max_tier is not None:
            if override.max_tier > spec.max_tier:
                # A store that skipped validation must still not widen exposure.
                logger.warning(
                    "max_tier override above file value ignored",
                    model=spec.id,
                    file_tier=spec.max_tier.label,
                    override_tier=override.max_tier.label,
                )
            else:
                update["max_tier"] = override.max_tier
        return spec.model_copy(update=update) if update else spec
