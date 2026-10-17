"""`require_scopes(...)`: the FastAPI dependency that guards every authenticated route.

Missing key → 401 AUTH_REQUIRED; unknown or revoked key → 401 AUTH_INVALID; a key without the
route's scopes → 403 FORBIDDEN. The `admin` scope satisfies any scope. The key itself is never
logged: only its name is bound to the request's log context.
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

logger = structlog.get_logger(__name__)

API_KEY_HEADER = "X-API-Key"
# `last_used_at` is a hint, not an audit trail: at most one write a minute per key.
TOUCH_INTERVAL = timedelta(minutes=1)

_header = APIKeyHeader(name=API_KEY_HEADER, auto_error=False, description="ai-twin API key")


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


def require_scopes(*required: Scope) -> Callable[..., Awaitable[Principal]]:
    async def dependency(request: Request, api_key: str | None = Security(_header)) -> Principal:
        if not api_key:
            raise AppError(ErrorCode.AUTH_REQUIRED, f"Send an API key in {API_KEY_HEADER}")
        store = _store(request)
        record = await store.find_by_hash(hash_key(api_key))
        if record is None or not record.active:
            raise AppError(ErrorCode.AUTH_INVALID, "API key is unknown or revoked")
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
