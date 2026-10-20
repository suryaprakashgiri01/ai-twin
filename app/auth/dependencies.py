"""`require_scopes(...)`: the FastAPI dependency that guards every authenticated route.

Missing key → 401 UNAUTHENTICATED; unknown or revoked key → 401 INVALID_API_KEY; a key without the
route's scopes → 403 FORBIDDEN. The `admin` scope satisfies any scope. The key itself is never
logged: only its name is bound to the request's log context.

Every failure is an `auth_failed` security event with `surface` admin or public (by path), once
per failure code and surface a minute, so a flood leaves one event a minute, not thousands.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

import structlog
from fastapi import Request, Security
from fastapi.security import APIKeyHeader

from app.auth.keys import ApiKeyStore, hash_key
from app.core.context import set_user_id
from app.db.documents import Scope, utcnow
from app.exceptions import AppError, ErrorCode
from app.middleware.routing import route_template
from app.services.security_events import SecurityEventWriter, Surface, first_in_window

logger = structlog.get_logger(__name__)

API_KEY_HEADER = "X-API-Key"
ADMIN_PATH_PREFIX = "/admin/"
AUTH_FAILURES = frozenset(
    {ErrorCode.UNAUTHENTICATED, ErrorCode.INVALID_API_KEY, ErrorCode.FORBIDDEN}
)
# A burst of the same failure is recorded once per this many seconds.
AUTH_EVENT_WINDOW = 60
# `last_used_at` is a hint, not an audit trail: at most one write a minute per key.
TOUCH_INTERVAL = timedelta(minutes=1)

_header = APIKeyHeader(name=API_KEY_HEADER, auto_error=False, description="ai-twin API key")
# For dependencies that wrap `require_scopes` and need the same header scheme.
api_key_header = _header


@dataclass(frozen=True)
class Principal:
    key_id: str
    name: str
    scopes: tuple[Scope, ...]

    def allows(self, required: tuple[Scope, ...]) -> bool:
        return "admin" in self.scopes or set(required) <= set(self.scopes)


def _store(request: Request) -> ApiKeyStore:
    store: ApiKeyStore = request.app.state.resources.api_keys
    return store


def _clock(request: Request) -> datetime:
    clock: Callable[[], datetime] = getattr(request.app.state, "clock", utcnow)
    return clock()


async def record_auth_failure(request: Request, code: ErrorCode) -> None:
    """One `auth_failed` event per code and surface per window; never fails the request."""
    resources = getattr(request.app.state, "resources", None)
    events: SecurityEventWriter | None = getattr(resources, "security_events", None)
    redis = getattr(resources, "redis", None)
    if events is None or redis is None or code not in AUTH_FAILURES:
        return
    surface: Surface = "admin" if request.url.path.startswith(ADMIN_PATH_PREFIX) else "public"
    try:
        if not await first_in_window(redis, f"auth:{surface}:{code.value}", AUTH_EVENT_WINDOW):
            return
    except Exception as exc:
        logger.warning("auth failure not recorded", error_type=type(exc).__name__)
        return
    await events.record(
        "auth_failed",
        "medium",
        rules=[code.value],
        details={"path": route_template(request.scope)},
        surface=surface,
    )


def require_scopes(*required: Scope) -> Callable[..., Awaitable[Principal]]:
    checked = _checked_scopes(*required)

    async def dependency(request: Request, api_key: str | None = Security(_header)) -> Principal:
        try:
            return await checked(request, api_key)
        except AppError as exc:
            await record_auth_failure(request, exc.code)
            raise

    return dependency


def _checked_scopes(*required: Scope) -> Callable[..., Awaitable[Principal]]:
    async def dependency(request: Request, api_key: str | None) -> Principal:
        if not api_key:
            raise AppError(ErrorCode.UNAUTHENTICATED, f"Send an API key in {API_KEY_HEADER}")
        store = _store(request)
        record = await store.find_by_hash(hash_key(api_key))
        if record is None or not record.active:
            raise AppError(ErrorCode.INVALID_API_KEY, "API key is unknown or revoked")
        principal = Principal(key_id=record.id, name=record.name, scopes=tuple(record.scopes))
        set_user_id(f"key:{principal.name}")
        structlog.contextvars.bind_contextvars(user_id=f"key:{principal.name}")
        now = _clock(request)
        if record.last_used_at is None or now - record.last_used_at >= TOUCH_INTERVAL:
            await store.touch(record.id, now)
        if not principal.allows(required):
            raise AppError(
                ErrorCode.FORBIDDEN, "API key lacks the required scope", details=list(required)
            )
        return principal

    return dependency
