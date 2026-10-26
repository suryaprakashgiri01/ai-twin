"""Per-agent model settings: what each owner agent uses, overridden from the console."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from app.agents.models import AgentName
from app.api.envelope import Envelope, ok
from app.auth.dependencies import Principal, require_scopes
from app.exceptions import AppError, ErrorCode
from app.services.agent_settings import AgentSettings, AgentSettingsIn, AgentSettingsService
from app.services.audit import AuditLog

router = APIRouter(prefix="/agents", tags=["admin: agents"])

AdminKey = Annotated[Principal, Depends(require_scopes("admin"))]


def _service(request: Request) -> AgentSettingsService:
    service: AgentSettingsService | None = getattr(request.app.state, "agent_settings", None)
    if service is None:
        raise AppError(ErrorCode.SERVICE_UNAVAILABLE, "Agent settings are not loaded")
    return service


async def _audit(
    request: Request,
    principal: Principal,
    action: str,
    name: str,
    before: AgentSettings,
    after: AgentSettings,
) -> None:
    audit: AuditLog = request.app.state.resources.audit
    keep = {"model", "prefer", "confidential_model", "confidential_prefer", "need", "source"}
    await audit.record(
        actor=f"key:{principal.name}",
        action=action,
        target=name,
        details={
            "before": before.model_dump(include=keep),
            "after": after.model_dump(include=keep),
        },
    )


@router.get("", summary="Every owner agent's model settings and where they come from")
async def list_agents(_principal: AdminKey, request: Request) -> Envelope[list[AgentSettings]]:
    """`source` is `yaml` (config/agents.yaml), `override` (set here) or `default` (auto)."""
    return ok(_service(request).list())


@router.put("/{name}", summary="Override one agent's model settings (audited, applies at once)")
async def put_agent(
    name: AgentName, body: AgentSettingsIn, principal: AdminKey, request: Request
) -> Envelope[AgentSettings]:
    """`model` / `confidentialModel` are `auto` or an id from config/models.yaml. A cloud model
    as `confidentialModel` is refused: confidential text only ever reaches a local model. The
    body replaces the agent's settings and the next call uses it."""
    service = _service(request)
    before = service.get(name)
    after = await service.put(name, body)
    await _audit(request, principal, "agent_settings.put", name, before, after)
    return ok(after, "Agent settings saved")


@router.delete("/{name}", summary="Reset one agent to config/agents.yaml (audited)")
async def reset_agent(
    name: AgentName, principal: AdminKey, request: Request
) -> Envelope[AgentSettings]:
    service = _service(request)
    before = service.get(name)
    after = await service.reset(name)
    await _audit(request, principal, "agent_settings.reset", name, before, after)
    return ok(after, "Agent settings reset")
