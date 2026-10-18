"""Opportunities left through the assistant or the form: list them and move them along."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from app.auth.dependencies import Principal, require_scopes
from app.leads.models import LeadStatus, TriageLabel
from app.services.admin_ops import AdminOps, LeadOut, LeadPatch, key_actor

router = APIRouter(prefix="/leads", tags=["admin: leads"])

AdminKey = Annotated[Principal, Depends(require_scopes("admin"))]


def _ops(request: Request) -> AdminOps:
    return AdminOps.from_state(request.app.state)


@router.get("", summary="Leads, newest first")
async def list_leads(
    _principal: AdminKey,
    request: Request,
    status: Annotated[LeadStatus | None, Query()] = None,
    triage: Annotated[TriageLabel | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[LeadOut]:
    return await _ops(request).list_leads(status, triage, limit)


@router.patch(
    "/{lead_id}", summary="Set a lead's status: new, reviewed, replied, archived (audited)"
)
async def patch_lead(
    lead_id: str, body: LeadPatch, principal: AdminKey, request: Request
) -> LeadOut:
    return await _ops(request).update_lead(lead_id, body, key_actor(principal.name))
