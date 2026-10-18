"""Security events from the public surface: what happened and which rule fired, never the text.

Written for every input-guard block, meta probe, output-guard block, canary hit (critical),
grounding fail-open, rate-limit burst and auth failure on public routes, for a refused lead
(over a cap, failed Turnstile) and for a lead email that failed its last attempt. Details hold rule
names, layers, scores and ids only. A failed write is logged and dropped: recording an event
must never break a visitor's reply. Bursts (rate limits, auth failures) are recorded once per
window via a Redis `SET NX`, so a flood leaves one event, not thousands. Emailing events is a
later feature (observability-alerts).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Literal, Protocol

import structlog
from redis.asyncio import Redis

from app.cache.redis import security_burst_key
from app.db.documents import SecurityEvent

logger = structlog.get_logger(__name__)

EventKind = Literal[
    "input_blocked",
    "meta_probe",
    "output_blocked",
    "canary_hit",
    "grounding_fail_open",
    "rate_limited",
    "auth_failed",
    "lead_delivery_failed",
    "lead_refused",
]
Severity = Literal["low", "medium", "high", "critical"]
Detail = str | int | float | bool | None


class SecurityEventWriter(Protocol):
    async def record(
        self,
        kind: EventKind,
        severity: Severity,
        *,
        rules: Sequence[str] = (),
        visitor_hash: str | None = None,
        details: Mapping[str, Detail] | None = None,
    ) -> None: ...


class MongoSecurityEvents:
    async def record(
        self,
        kind: EventKind,
        severity: Severity,
        *,
        rules: Sequence[str] = (),
        visitor_hash: str | None = None,
        details: Mapping[str, Detail] | None = None,
    ) -> None:
        body = {"rules": list(rules), **(details or {})}
        try:
            await SecurityEvent(
                kind=kind, severity=severity, details=body, visitor_hash=visitor_hash
            ).insert()
        except Exception as exc:
            logger.warning("security event not stored", kind=kind, error_type=type(exc).__name__)
            return
        log = logger.error if severity == "critical" else logger.info
        log("security event", kind=kind, severity=severity, rules=list(rules))


async def first_in_window(redis: Redis, scope: str, window_seconds: int) -> bool:
    """True once per `window_seconds` for `scope`: the burst's first event is the one kept."""
    return bool(await redis.set(security_burst_key(scope), 1, ex=window_seconds, nx=True))
