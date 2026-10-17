"""Build the FastAPI app: logging, middleware, error handlers, routers."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import AsyncExitStack, asynccontextmanager

import structlog
from fastapi import FastAPI

from app.api.routes import health, public
from app.api.routes.admin import build_admin_router
from app.core.config import Settings, get_settings
from app.core.llm import LLMServices, build_llm
from app.core.logging import setup_logging
from app.core.resources import Resources, ResourcesFactory, open_resources
from app.exceptions.handlers import register_exception_handlers
from app.kb.embedding import Embedder, FastEmbedder
from app.kb.ingestion.runtime import build_ingestion, run_ingestion
from app.middleware.request_context import RequestContextMiddleware
from app.services.health import DependencyCheck, default_checks
from app.services.model_overrides import OrchestratorOverrides, OverrideStore
from app.services.owner_chat import build_owner_chat
from app.services.public_chat import build_public_chat
from app.services.resume import resume_provider

logger = structlog.get_logger(__name__)


def create_app(
    settings: Settings | None = None,
    health_checks: Sequence[DependencyCheck] | None = None,
    llm: LLMServices | None = None,
    resources_factory: ResourcesFactory | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    setup_logging(level=settings.log_level, log_format=settings.log_format)
    open_storage = resources_factory or open_resources

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with open_storage(settings) as resources, AsyncExitStack() as stack:
            app.state.resources = resources
            # Tests may hand in stand-in resources and wire services themselves.
            if isinstance(resources, Resources):
                await _start_services(app, settings, resources, stack)
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
    app.state.llm = llm or build_llm(settings, OrchestratorOverrides(lambda: _overrides(app)))

    app.add_middleware(RequestContextMiddleware)
    register_exception_handlers(app)
    app.include_router(health.router)
    app.include_router(public.router)
    # The public deployment has no admin surface at all, not merely a locked one.
    if settings.deployment_profile == "vault":
        app.include_router(build_admin_router())
    return app


async def _start_services(
    app: FastAPI, settings: Settings, resources: Resources, stack: AsyncExitStack
) -> None:
    embedder: Embedder
    # Ingestion workers and owner chat run in the vault only.
    if settings.deployment_profile == "vault":
        ingestion = build_ingestion(settings, resources, app.state.llm)
        app.state.ingestion = await stack.enter_async_context(run_ingestion(ingestion))
        # One embedder for indexing and search, so its models load once.
        embedder = ingestion.pipeline.embedder
        app.state.owner_chat = build_owner_chat(settings, resources, app.state.llm, embedder)
    else:
        embedder = FastEmbedder(settings.models_dir / "fastembed")
    app.state.resume = resume_provider(settings, resources.profile, resources.blobs)
    app.state.public_chat = build_public_chat(
        settings, resources, app.state.llm, embedder, app.state.resume
    )


def _overrides(app: FastAPI) -> OverrideStore | None:
    resources = getattr(app.state, "resources", None)
    return getattr(resources, "overrides", None)
