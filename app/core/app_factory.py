"""Build the FastAPI app: logging, middleware, error handlers, routers."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI

from app.api.routes import health
from app.core.config import Settings, get_settings
from app.core.logging import setup_logging
from app.exceptions.handlers import register_exception_handlers
from app.middleware.request_context import RequestContextMiddleware
from app.services.health import DependencyCheck, default_checks

logger = structlog.get_logger(__name__)


def create_app(
    settings: Settings | None = None,
    health_checks: Sequence[DependencyCheck] | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    setup_logging(level=settings.log_level, log_format=settings.log_format)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        logger.info(
            "service started",
            env=settings.app_env,
            profile=settings.deployment_profile,
            version=settings.service_version,
        )
        yield
        logger.info("service stopped")

    app = FastAPI(
        title="ai-twin",
        version=settings.service_version,
        description=(
            "Personal knowledge base and portfolio AI twin.\n\n"
            "**Tracing:** every response carries `X-Request-ID`; send `X-Session-ID` to group "
            "requests.\n\n"
            '**Errors:** always `{"error": {code, message, request_id, details?}}`.'
        ),
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.health_checks = list(
        health_checks if health_checks is not None else default_checks(settings)
    )

    app.add_middleware(RequestContextMiddleware)
    register_exception_handlers(app)
    app.include_router(health.router)
    return app
