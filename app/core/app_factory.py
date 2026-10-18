"""Build the FastAPI app: logging, middleware, error handlers, routers."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from functools import partial

import structlog
from fastapi import FastAPI

from app.api.routes import health, public
from app.api.routes.admin import build_admin_router
from app.core.config import Settings, get_settings
from app.core.llm import LLMServices, build_llm
from app.core.logging import setup_logging
from app.core.resources import Resources, ResourcesFactory, open_resources
from app.core.telemetry import setup_telemetry, shutdown_telemetry
from app.exceptions.handlers import register_exception_handlers
from app.kb.embedding import Embedder, FastEmbedder
from app.kb.ingestion.runtime import build_ingestion, run_ingestion
from app.leads.outbox import build_outbox
from app.leads.service import build_lead_service
from app.middleware.request_context import RequestContextMiddleware
from app.observability import metrics
from app.observability.tracing import NOOP, Tracing, build_tracing, install
from app.services.health import DependencyCheck, default_checks
from app.services.intake import build_intake, prune_checkpoints
from app.services.model_overrides import OrchestratorOverrides, OverrideStore
from app.services.owner_chat import MongoConversationIndex, build_owner_chat_from, owner_deps
from app.services.public_chat import build_public_chat, prune_public, public_checkpointer
from app.services.resume import resume_provider
from app.services.titling import ConversationTitler

logger = structlog.get_logger(__name__)

SHUTDOWN_FLUSH_SECONDS = 5.0


def create_app(
    settings: Settings | None = None,
    health_checks: Sequence[DependencyCheck] | None = None,
    llm: LLMServices | None = None,
    resources_factory: ResourcesFactory | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    setup_logging(level=settings.log_level, log_format=settings.log_format)
    setup_telemetry(settings)
    metrics.install_llm_metrics()
    open_storage = resources_factory or open_resources

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        tracing = build_tracing(settings)
        install(tracing)
        app.state.tracing = tracing
        async with open_storage(settings) as resources, AsyncExitStack() as stack:
            stack.push_async_callback(_stop_tracing, tracing)
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
        shutdown_telemetry()
        logger.info("service stopped")

    app = FastAPI(
        title="ai-twin",
        version=settings.service_version,
        description=(
            "Personal knowledge base and portfolio AI twin.\n\n"
            "**Tracing:** every response carries `X-Request-ID` and `X-Trace-ID` (the trace id "
            "on the request's log lines and its Langfuse trace); send `X-Session-ID` to group "
            "requests.\n\n"
            '**Errors:** always `{"error": {code, message, request_id, details?}}`.'
        ),
        lifespan=lifespan,
        # Our middleware owns the server span: FastAPI's native one would adopt a caller's
        # `traceparent`, letting a visitor choose the trace id.
        telemetry={"tracing": False, "metrics": False, "logs": False, "operation_spans": False},
    )
    app.state.settings = settings
    app.state.health_checks = list(
        health_checks if health_checks is not None else default_checks(settings)
    )
    app.state.llm = llm or build_llm(settings, OrchestratorOverrides(lambda: _overrides(app)))

    app.add_middleware(RequestContextMiddleware)
    register_exception_handlers(app)
    app.include_router(health.router)
    app.include_router(metrics.router)
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
        owner = owner_deps(settings, resources, app.state.llm, embedder)
        app.state.owner_chat = build_owner_chat_from(owner, resources, app.state.llm)
        app.state.titler = ConversationTitler(MongoConversationIndex(), app.state.llm.orchestrator)
        intake, saver = build_intake(settings, resources, app.state.llm, ingestion, owner)
        app.state.intake = intake
        await prune_checkpoints(saver, settings.intake_checkpoint_days)
    else:
        embedder = FastEmbedder(settings.models_dir / "fastembed")
    saver = public_checkpointer()
    outbox = build_outbox(
        settings,
        resources.leads,
        resources.security_events,
        resources.redis,
        hooks=[partial(prune_public, saver)],
    )
    app.state.lead_outbox = await stack.enter_async_context(outbox.running())
    app.state.leads = build_lead_service(settings, resources, app.state.llm, outbox)
    app.state.resume = resume_provider(settings, resources.profile, resources.blobs)
    app.state.public_chat = build_public_chat(
        settings,
        resources,
        app.state.llm,
        embedder,
        app.state.resume,
        leads=app.state.leads,
        checkpointer=saver,
    )


async def _stop_tracing(tracing: Tracing) -> None:
    """Flush pending traces, but never hold shutdown hostage to an unreachable Langfuse."""
    install(NOOP)
    try:
        await asyncio.wait_for(asyncio.to_thread(tracing.shutdown), timeout=SHUTDOWN_FLUSH_SECONDS)
    except TimeoutError:
        logger.warning("langfuse flush timed out at shutdown")


def _overrides(app: FastAPI) -> OverrideStore | None:
    resources = getattr(app.state, "resources", None)
    return getattr(resources, "overrides", None)
