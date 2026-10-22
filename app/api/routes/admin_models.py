from __future__ import annotations

from typing import Annotated, get_args

from fastapi import APIRouter, Depends, Query, Request

from app.api.envelope import ApiModel, Envelope, ok
from app.api.paging import ListSpec, Page, PageQuery, page_query, paginate
from app.auth.dependencies import Principal, require_scopes
from app.core.llm import LLMServices, provider_secrets
from app.db.documents import Tier
from app.services.audit import AuditLog
from app.services.model_overrides import Override, OverrideIn, OverrideStore
from app.services.model_status import ModelStatus, ModelStatusChecker
from llm_orchestrator import ModelSpec
from llm_orchestrator.types import LOCAL_PROVIDERS

router = APIRouter(prefix="/models", tags=["admin"])

AdminKey = Annotated[Principal, Depends(require_scopes("admin"))]

OVERRIDES = ListSpec(
    sortable=("id", "updatedAt"),
    default_sort="id",
    default_order="asc",
    filters={"enabled": ("true", "false"), "maxTier": get_args(Tier)},
    search=("id", "chain"),
)


def _store(request: Request) -> OverrideStore:
    store: OverrideStore = request.app.state.resources.overrides
    return store


def _audit(request: Request) -> AuditLog:
    audit: AuditLog = request.app.state.resources.audit
    return audit


class InventoryModel(ApiModel):
    id: str
    # active, disabled, no key, not pulled, unreachable, or missing (verify only).
    status: ModelStatus
    hint: str | None = None
    provider: str
    roles: list[str]
    max_tier: str
    enabled: bool
    local: bool
    quality: int
    tags: list[str]
    capabilities: list[str]


def _checker(request: Request) -> ModelStatusChecker:
    checker: ModelStatusChecker | None = getattr(request.app.state, "model_status", None)
    if checker is None:
        settings = request.app.state.settings
        checker = ModelStatusChecker(settings, provider_secrets(settings))
    return checker


@router.get("", summary="Every model in config/models.yaml, with whether it can answer here")
async def list_models(
    _principal: AdminKey,
    request: Request,
    verify: Annotated[
        bool, Query(description="also ask each provider if the id is served")
    ] = False,
) -> Envelope[list[InventoryModel]]:
    """`enabled` and `maxTier` include stored overrides. `status`: `active`, `disabled`, `no key`
    (provider key not set), `not pulled` (Ollama), `unreachable`, or with `verify=true`
    `missing` (the provider no longer serves the id). `hint` says how to fix it. The provider's
    own model name is left out."""
    llm: LLMServices = request.app.state.llm
    stored = {o.id: o for o in await _store(request).list()}
    specs = list(llm.orchestrator.inventory.models.values())

    def enabled_of(spec: ModelSpec) -> bool:
        override = stored.get(spec.id)
        return spec.enabled if override is None or override.enabled is None else override.enabled

    states = await _checker(request).check(specs, enabled_of, verify=verify)
    out = []
    for spec in specs:
        override = stored.get(spec.id)
        enabled = enabled_of(spec)
        status, hint = states[spec.id]
        tier = override.max_tier if override and override.max_tier else spec.max_tier.label
        out.append(
            InventoryModel(
                id=spec.id,
                status=status,
                hint=hint,
                provider=spec.provider.value,
                roles=sorted(r.value for r in spec.roles),
                max_tier=tier,
                enabled=enabled,
                local=spec.provider in LOCAL_PROVIDERS,
                quality=spec.quality,
                tags=sorted(spec.tags),
                capabilities=sorted(spec.capabilities),
            )
        )
    return ok(out)


@router.get("/overrides", summary="List stored model and role overrides")
async def list_overrides(
    _principal: AdminKey,
    request: Request,
    query: Annotated[PageQuery, Depends(page_query(OVERRIDES))],
) -> Envelope[Page[Override]]:
    return ok(paginate(await _store(request).list(), query, OVERRIDES))


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
