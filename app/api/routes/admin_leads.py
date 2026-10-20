"""Opportunities left through the assistant or the form: list them and move them along."""

from __future__ import annotations

from typing import Annotated, get_args

from fastapi import APIRouter, Depends, Request

from app.api.envelope import Envelope, ok
from app.api.paging import LIST_SCAN_MAX, ListSpec, Page, PageQuery, page_query, paginate
from app.auth.dependencies import Principal, require_scopes
from app.leads.models import LeadStatus, TriageLabel
from app.services.admin_ops import AdminOps, LeadOut, LeadPatch, key_actor

router = APIRouter(prefix="/leads", tags=["admin: leads"])

AdminKey = Annotated[Principal, Depends(require_scopes("admin"))]

LEADS = ListSpec(
    sortable=("createdAt", "name", "company", "status", "triage", "triageConfidence"),
    default_sort="createdAt",
    filters={
        "status": get_args(LeadStatus),
        "triage": get_args(TriageLabel),
        "source": None,
        "employmentType": None,
    },
    search=("name", "email", "company", "role", "message", "summary", "location"),
)


def _ops(request: Request) -> AdminOps:
    return AdminOps.from_state(request.app.state)


@router.get("", summary="Leads, newest first")
async def list_leads(
    _principal: AdminKey,
    request: Request,
    query: Annotated[PageQuery, Depends(page_query(LEADS))],
) -> Envelope[Page[LeadOut]]:
    status, triage = query.filters.get("status"), query.filters.get("triage")
    leads = await _ops(request).list_leads(status, triage, LIST_SCAN_MAX)
    return ok(paginate(leads, query, LEADS))


@router.patch(
    "/{lead_id}", summary="Set a lead's status: new, reviewed, replied, archived (audited)"
)
async def patch_lead(
    lead_id: str, body: LeadPatch, principal: AdminKey, request: Request
) -> Envelope[LeadOut]:
    lead = await _ops(request).update_lead(lead_id, body, key_actor(principal.name))
    return ok(lead, "Lead updated")
