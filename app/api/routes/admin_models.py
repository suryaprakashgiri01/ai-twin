from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from app.api.envelope import Envelope, ok
from app.auth.dependencies import Principal, require_scopes
from app.core.llm import LLMServices
from app.services.audit import AuditLog
from app.services.model_overrides import Override, OverrideIn, OverrideStore

router = APIRouter(prefix="/models", tags=["admin"])

AdminKey = Annotated[Principal, Depends(require_scopes("admin"))]


def _store(request: Request) -> OverrideStore:
    store: OverrideStore = request.app.state.resources.overrides
    return store


def _audit(request: Request) -> AuditLog:
    audit: AuditLog = request.app.state.resources.audit
    return audit


@router.get("/overrides", summary="List stored model and role overrides")
async def list_overrides(_principal: AdminKey, request: Request) -> Envelope[list[Override]]:
    return ok(await _store(request).list())


@router.put("/overrides/{model_id:path}", summary="Create or replace one override (audited)")
async def put_override(
    model_id: str, body: OverrideIn, principal: AdminKey, request: Request
) -> Envelope[Override]:
    """`model_id` is a model id (`enabled`, `max_tier`) or `role:<role>` (`chain`). The body
    replaces the stored override, is checked against `config/models.yaml` (a tier can only be
    lowered) and applies to the next model call. The change is written to the audit log."""
    llm: LLMServices = request.app.state.llm
    body.check_against(model_id, llm.orchestrator.inventory)
    store = _store(request)
    before = await store.get(model_id)
    after = await store.put(model_id, body)
    await _audit(request).record(
        actor=f"key:{principal.name}",
        action="model_override.put",
        target=model_id,
        details={
            "before": before.model_dump(mode="json", exclude={"id"}) if before else None,
            "after": after.model_dump(mode="json", exclude={"id"}),
        },
    )
    return ok(after, "Override saved")
