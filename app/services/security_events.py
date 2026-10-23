"""Security and ops events: what happened and which rule fired, never the text.

Written for every input-guard block, meta probe, output-guard block, canary hit (critical),
grounding fail-open, rate-limit burst and auth failure (admin and public routes, told apart by
`surface`), for a refused lead (over a cap, failed Turnstile) and for a lead email that failed its
last attempt; and, as ops events, when every model in a chain failed (`llm_unavailable`) and when
an ingestion job failed (`ingestion_failed`). Details hold rule names, layers, scores and ids
only. A failed write is logged and dropped: recording an event must never break a reply. Bursts
(rate limits, auth failures) are recorded once per window via a Redis `SET NX`, so a flood leaves
one event, not thousands. The alert worker (`app.security.worker`) emails them by policy.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Literal, Protocol

import structlog
from redis.asyncio import Redis

from app.cache.redis import security_burst_key
from app.core.telemetry import current_trace_id
from app.db.documents import SecurityEvent
from app.observability.metrics import SECURITY_EVENTS
from llm_orchestrator import Attempt, Role, Tier
from llm_orchestrator.hooks import on_exhausted

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
    "llm_unavailable",
    "ingestion_failed",
]
Severity = Literal["low", "medium", "high", "critical"]
Surface = Literal["admin", "public"]
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
        surface: Surface | None = None,
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
        surface: Surface | None = None,
    ) -> None:
        SECURITY_EVENTS.labels(kind, severity).inc()
        body = {"rules": list(rules), **(details or {})}
        try:
            await SecurityEvent(
                kind=kind,
                severity=severity,
                details=body,
                visitor_hash=visitor_hash,
                surface=surface,
                trace_id=current_trace_id(),
            ).insert()
        except Exception as exc:
            logger.warning("security event not stored", kind=kind, error_type=type(exc).__name__)
            return
        log = logger.error if severity == "critical" else logger.info
        log("security event", kind=kind, severity=severity, rules=list(rules))


async def first_in_window(redis: Redis, scope: str, window_seconds: int) -> bool:
    """True once per `window_seconds` for `scope`: the burst's first event is the one kept."""
    return bool(await redis.set(security_burst_key(scope), 1, ex=window_seconds, nx=True))


# An outage fails every call; one `llm_unavailable` per role and tier per window is enough.
LLM_UNAVAILABLE_WINDOW = 60


class ExhaustedChains:
    """Records `llm_unavailable` when every model in a chain failed (an orchestrator hook).

    The hook is synchronous and runs inside the failing call, so the write is a background
    task; it never delays or breaks the caller.
    """

    def __init__(self, events: SecurityEventWriter, redis: Redis) -> None:
        self._events = events
        self._redis = redis
        self._tasks: set[asyncio.Task[None]] = set()

    def __call__(self, role: Role, tier: Tier, attempts: Sequence[Attempt]) -> None:
        try:
            task = asyncio.get_running_loop().create_task(self._record(role, tier, attempts))
        except RuntimeError:
            return  # no loop: nothing to record into
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _record(self, role: Role, tier: Tier, attempts: Sequence[Attempt]) -> None:
        try:
            scope = f"llm:{role.value}:{tier.label}"
            if not await first_in_window(self._redis, scope, LLM_UNAVAILABLE_WINDOW):
                return
            await self._events.record(
                "llm_unavailable",
                "high",
                rules=sorted({f"{a.model_id}:{a.outcome}" for a in attempts}),
                details={"role": role.value, "tier": tier.label, "attempts": len(attempts)},
            )
        except Exception as exc:
            logger.warning("llm_unavailable not recorded", error_type=type(exc).__name__)

    async def drain(self) -> None:
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)


@contextlib.asynccontextmanager
async def recording_exhausted_chains(
    events: SecurityEventWriter, redis: Redis
) -> AsyncIterator[ExhaustedChains]:
    recorder = ExhaustedChains(events, redis)
    remove = on_exhausted(recorder)
    try:
        yield recorder
    finally:
        remove()
        await recorder.drain()
