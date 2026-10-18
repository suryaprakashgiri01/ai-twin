"""Security events and alert email status (vault). Events are listed without any text: kind,
severity, rule names, a visitor-hash prefix, surface and times."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel

from app.auth.dependencies import Principal, require_scopes
from app.exceptions import AppError, ErrorCode
from app.security.alerts import VISITOR_PREFIX, StoredEvent, safe_rule
from app.security.worker import AlertWorker
from app.services.admin_ops import key_actor
from app.services.audit import AuditLog
from app.services.security_events import EventKind

router = APIRouter(tags=["admin: security"])

AdminKey = Annotated[Principal, Depends(require_scopes("admin"))]
SeverityQuery = Literal["low", "medium", "high", "critical"]


class SecurityEventOut(BaseModel):
    id: str
    kind: str
    severity: str
    rules: list[str]
    # The first characters of the visitor's salted hash: enough to see repeats, not to track.
    visitor: str | None
    surface: str | None
    created_at: datetime
    alerted_at: datetime | None
    digested_at: datetime | None

    @classmethod
    def of(cls, event: StoredEvent) -> SecurityEventOut:
        return cls(
            id=event.id,
            kind=event.kind,
            severity=event.severity,
            rules=[safe_rule(r) for r in event.rules],
            visitor=event.visitor_hash[:VISITOR_PREFIX] if event.visitor_hash else None,
            surface=event.surface,
            created_at=event.created_at,
            alerted_at=event.alerted_at,
            digested_at=event.digested_at,
        )


class PolicyOut(BaseModel):
    name: str
    mode: str
    rule: str
    cooldown_remaining_seconds: int
    last_sent: datetime | None


class AlertStatus(BaseModel):
    smtp_configured: bool
    cooldown_minutes: int
    digest_hour: int
    timezone: str
    last_digest: datetime | None
    policies: list[PolicyOut]


class AlertTestResult(BaseModel):
    sent: bool


def _worker(request: Request) -> AlertWorker:
    worker: AlertWorker | None = getattr(request.app.state, "alert_worker", None)
    if worker is None:
        raise AppError(ErrorCode.DEPENDENCY_UNAVAILABLE, "The alert worker is not running")
    return worker


@router.get("/security/events", summary="Security events, newest first (no text)")
async def list_events(
    _principal: AdminKey,
    request: Request,
    kind: Annotated[EventKind | None, Query()] = None,
    severity: Annotated[SeverityQuery | None, Query()] = None,
    since: Annotated[datetime | None, Query(description="ISO 8601, e.g. 2026-10-19T00:00:00Z")] = (
        None
    ),
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
) -> list[SecurityEventOut]:
    events = await _worker(request).store.recent(
        kind=kind, severity=severity, since=since, limit=limit
    )
    return [SecurityEventOut.of(e) for e in events]


@router.get("/alerts/status", summary="Alert policies, cooldowns, last sent, SMTP configured")
async def alert_status(_principal: AdminKey, request: Request) -> AlertStatus:
    worker = _worker(request)
    s = worker.settings
    return AlertStatus(
        smtp_configured=worker.configured,
        cooldown_minutes=int(s.cooldown.total_seconds() // 60),
        digest_hour=s.digest_hour,
        timezone=str(s.timezone),
        last_digest=await worker.last_digest(),
        policies=[
            PolicyOut(
                name=p.name,
                mode=p.mode,
                rule=p.rule,
                cooldown_remaining_seconds=p.cooldown_seconds,
                last_sent=p.last_sent,
            )
            for p in await worker.status()
        ],
    )


@router.post("/alerts/test", summary="Send one test alert email (audited)")
async def test_alert(principal: AdminKey, request: Request) -> AlertTestResult:
    """Sends `ai-twin alert · low · test` to the alert recipient. 503 when SMTP is not
    configured; `sent: false` when the send itself failed (the error is in the logs)."""
    worker = _worker(request)
    if not worker.configured:
        raise AppError(
            ErrorCode.DEPENDENCY_UNAVAILABLE,
            "Alert email is not configured (SMTP_HOST, LEAD_EMAIL_FROM, ALERT_EMAIL_TO or "
            "LEAD_EMAIL_TO)",
        )
    sent = await worker.send_test()
    audit: AuditLog = request.app.state.resources.audit
    await audit.record(
        actor=key_actor(principal.name),
        action="alerts.test",
        target="alerts",
        details={"sent": sent},
    )
    return AlertTestResult(sent=sent)
