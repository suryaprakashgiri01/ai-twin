from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.core.config import Settings
from app.services.health import DependencyCheck, run_checks

router = APIRouter(tags=["health"])


@router.get("/healthz", summary="Liveness")
async def liveness(request: Request) -> dict[str, str]:
    """The process is up and serving. Checks nothing else, by design: a liveness probe that
    checks dependencies restarts healthy instances during a database outage."""
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
                    }
                }
            },
        }
    },
)
async def readiness(request: Request) -> JSONResponse:
    """Ready to take traffic: every dependency answered within its timeout."""
    settings: Settings = request.app.state.settings
    checks: list[DependencyCheck] = request.app.state.health_checks
    results = await run_checks(checks, settings.health_check_timeout_seconds)
    ready = all(status == "ok" for status in results.values())
    return JSONResponse(
        status_code=200 if ready else 503,
        content={"status": "ready" if ready else "not_ready", "checks": results},
    )
