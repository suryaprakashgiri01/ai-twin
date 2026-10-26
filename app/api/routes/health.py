from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.core.config import Settings
from app.core.llm import LLMServices
from app.services.health import DependencyCheck, run_checks

router = APIRouter(tags=["health"])


@router.get("/livez", summary="Liveness")
@router.get("/healthz", summary="Liveness (alias; Cloud Run's ingest claims /healthz itself)")
async def liveness(request: Request) -> dict[str, str]:
    """The process is up and serving. Checks nothing else, by design: a liveness probe that
    checks dependencies restarts healthy instances during a database outage.

    `/livez` is the path to use on Cloud Run: its ingest layer answers `/healthz` with a 404
    of its own before the request ever reaches the container (see dev-notes/deploy-public.md)."""
    settings: Settings = request.app.state.settings
    return {"status": "ok", "version": settings.service_version}


@router.get(
    "/readyz",
    summary="Readiness",
    responses={
        503: {
            "description": "A dependency is unreachable",
            "content": {
                "application/json": {
                    "example": {
                        "status": "not_ready",
                        "checks": {"mongodb": "ok", "redis": "unavailable", "qdrant": "ok"},
                        "models": {
                            "router": 2,
                            "classifier": 2,
                            "generator": 2,
                            "judge": 2,
                            "guard": 1,
                        },
                    }
                }
            },
        }
    },
)
async def readiness(request: Request) -> JSONResponse:
    """Ready to take traffic: every dependency answered within its timeout.

    `models` counts usable models per role (enabled, key configured). It is informational and
    does not affect readiness.
    """
    settings: Settings = request.app.state.settings
    checks: list[DependencyCheck] = request.app.state.health_checks
    results = await run_checks(checks, settings.health_check_timeout_seconds)
    ready = all(status == "ok" for status in results.values())
    llm: LLMServices = request.app.state.llm
    return JSONResponse(
        status_code=200 if ready else 503,
        content={
            "status": "ready" if ready else "not_ready",
            "checks": results,
            "models": await llm.orchestrator.model_counts(),
        },
    )
