"""Owner overrides of the model registry, persisted in `model_overrides`.

Two shapes share the collection: a model override (`_id` = model id; `enabled` and/or
`max_tier`) and a role chain (`_id` = `role:<role>`; `chain` only). `OrchestratorOverrides`
adapts whichever store the app holds to the orchestrator's read protocol, so a PUT applies to the
next model call with no restart.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Literal, Protocol

import structlog
from pydantic import BaseModel, ConfigDict, Field

from app.db.documents import ModelOverride, utcnow
from app.exceptions import AppError, ErrorCode
from llm_orchestrator import Inventory, InventoryError, Role
from llm_orchestrator import ModelOverride as LLMOverride

logger = structlog.get_logger(__name__)

ROLE_PREFIX = "role:"
OverrideTier = Literal["public", "private", "confidential"]


class OverrideIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = None
    # Restricted data never reaches a model through an override; the cap stops at confidential.
    max_tier: OverrideTier | None = None
    chain: list[str] | None = Field(default=None, min_length=1)

    def check_for(self, override_id: str) -> None:
        """Raise VALIDATION_ERROR when the body does not fit the kind of id."""
        if override_id.startswith(ROLE_PREFIX):
            if self.chain is None or self.enabled is not None or self.max_tier is not None:
                raise AppError(ErrorCode.VALIDATION_ERROR, "A role override takes `chain` only")
            return
        if self.chain is not None:
            raise AppError(ErrorCode.VALIDATION_ERROR, "`chain` is only valid for role:<role>")
        if self.enabled is None and self.max_tier is None:
            raise AppError(ErrorCode.VALIDATION_ERROR, "Set `enabled` and/or `max_tier`")

    def check_against(self, override_id: str, inventory: Inventory) -> None:
        """Raise VALIDATION_ERROR for an unknown model or role, a bad chain, or a widened tier."""
        self.check_for(override_id)
        try:
            if override_id.startswith(ROLE_PREFIX):
                inventory.check_chain(_role(override_id), self.chain or [])
            else:
                inventory.check_override(
                    override_id, LLMOverride(enabled=self.enabled, max_tier=self.max_tier)
                )
        except (InventoryError, ValueError) as exc:
            raise AppError(ErrorCode.VALIDATION_ERROR, str(exc)) from exc


def _role(override_id: str) -> Role:
    return Role(override_id.removeprefix(ROLE_PREFIX))


class Override(OverrideIn):
    id: str
    updated_at: datetime


class OverrideStore(Protocol):
    async def list(self) -> list[Override]: ...

    async def get(self, override_id: str) -> Override | None: ...

    async def put(self, override_id: str, body: OverrideIn) -> Override: ...


def _override(doc: ModelOverride) -> Override:
    return Override(
        id=doc.id,
        enabled=doc.enabled,
        max_tier=doc.max_tier,
        chain=doc.chain,
        updated_at=doc.updated_at,
    )


class MongoOverrideStore:
    async def list(self) -> list[Override]:
        docs = await ModelOverride.find_all().sort("+_id").to_list()
        return [_override(d) for d in docs]

    async def get(self, override_id: str) -> Override | None:
        doc = await ModelOverride.get(override_id)
        return _override(doc) if doc else None

    async def put(self, override_id: str, body: OverrideIn) -> Override:
        doc = ModelOverride(id=override_id, updated_at=utcnow(), **body.model_dump())
        await doc.save()  # full replace: fields left out of the body are cleared
        return _override(doc)


class OrchestratorOverrides:
    """The orchestrator's `OverrideStore`, read from the app's store on every call.

    `source` returns the store, or None before storage is open (then there are no overrides).
    A row that no longer fits the inventory is skipped here; the orchestrator clamps the rest.
    """

    def __init__(self, source: Callable[[], OverrideStore | None]) -> None:
        self._source = source

    async def _rows(self) -> list[Override]:
        store = self._source()
        return await store.list() if store is not None else []

    async def model_overrides(self) -> Mapping[str, LLMOverride]:
        result: dict[str, LLMOverride] = {}
        for row in await self._rows():
            if row.id.startswith(ROLE_PREFIX):
                continue
            result[row.id] = LLMOverride(enabled=row.enabled, max_tier=row.max_tier)
        return result

    async def role_chains(self) -> Mapping[Role, Sequence[str]]:
        result: dict[Role, Sequence[str]] = {}
        for row in await self._rows():
            if not row.id.startswith(ROLE_PREFIX) or not row.chain:
                continue
            try:
                result[_role(row.id)] = list(row.chain)
            except ValueError:
                logger.warning("override for unknown role skipped", override_id=row.id)
        return result
