"""The owner's profile: availability, notice period, preferred roles, locations, and which
ingested resume is the downloadable one."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from app.api.envelope import Envelope, ok
from app.auth.dependencies import Principal, require_scopes
from app.services.admin_ops import AdminOps, key_actor
from app.services.profile import ProfileIn, ProfileOut

router = APIRouter(prefix="/profile", tags=["admin: profile"])

AdminKey = Annotated[Principal, Depends(require_scopes("admin"))]


def _ops(request: Request) -> AdminOps:
    return AdminOps.from_state(request.app.state)


@router.get("", summary="The owner's profile")
async def get_profile(_principal: AdminKey, request: Request) -> Envelope[ProfileOut]:
    return ok(await _ops(request).get_profile())


@router.put("", summary="Replace the owner's profile (audited)")
async def put_profile(
    body: ProfileIn, principal: AdminKey, request: Request
) -> Envelope[ProfileOut]:
    """`resume_document_id` names an ingested document; its original file becomes the
    downloadable resume."""
    return ok(await _ops(request).put_profile(body, key_actor(principal.name)), "Profile saved")
