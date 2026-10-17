"""Owner overrides of the model registry, persisted in `model_overrides`.

Two shapes share the collection: a model override (`_id` = model id; `enabled` and/or
`max_tier`) and a role chain (`_id` = `role:<role>`; `chain` only). The orchestrator applies
them on top of `config/models.yaml`; wiring that up is the first task of ingestion.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from app.db.documents import ModelOverride, utcnow
from app.exceptions import AppError, ErrorCode

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
