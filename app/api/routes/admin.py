"""Admin API, mounted only in the `vault` deployment profile."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.routes import admin_meta, admin_models

ADMIN_PREFIX = "/admin/v1"


def build_admin_router() -> APIRouter:
    router = APIRouter(prefix=ADMIN_PREFIX)
    router.include_router(admin_meta.router)
    router.include_router(admin_models.router)
    return router
