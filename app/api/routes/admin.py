"""Admin API, mounted only in the `vault` deployment profile."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.routes import (
    admin_assistant,
    admin_chat,
    admin_ingest,
    admin_leads,
    admin_memory,
    admin_meta,
    admin_models,
    admin_prep,
    admin_profile,
    admin_records,
    admin_review,
    admin_security,
    admin_super,
)

ADMIN_PREFIX = "/admin/v1"


def build_admin_router() -> APIRouter:
    router = APIRouter(prefix=ADMIN_PREFIX)
    router.include_router(admin_meta.router)
    router.include_router(admin_models.router)
    router.include_router(admin_ingest.router)
    router.include_router(admin_review.router)
    router.include_router(admin_profile.router)
    router.include_router(admin_records.router)
    router.include_router(admin_leads.router)
    router.include_router(admin_chat.router)
    router.include_router(admin_assistant.router)
    router.include_router(admin_prep.router)
    router.include_router(admin_super.router)
    router.include_router(admin_memory.router)
    router.include_router(admin_security.router)
    return router
