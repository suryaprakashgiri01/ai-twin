"""Submitting a lead, shared by the assistant's capture loop and the form endpoint.

In order: Turnstile (when configured), duplicate check, caps, triage + fit note, store, wake the
outbox. A duplicate (same email and company within a day) is merged into the existing lead and
sends nothing. A visitor may leave LEAD_VISITOR_DAILY_CAP leads a day and everyone together
LEAD_DAILY_CAP; over either, the submit is refused politely and a security event is written.
`spam` triage stores the lead as `suppressed`: no email, it goes into the daily digest.

Logs carry the lead id, outcome, triage label and a salted hash of the address, never a name,
an address or a message.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol

import structlog
from redis.asyncio import Redis

from app.cache.redis import lead_global_key, lead_visitor_key
from app.core.config import Settings
from app.core.llm import LLMServices
from app.core.resources import Resources
from app.db.documents import LeadDetails, LeadFields, utcnow
from app.leads.models import LeadIn, LeadSource, dedupe_key, email_hash
from app.leads.store import LeadStore
from app.leads.triage import FitFactsSource, PublicFitFacts, fit_note, triage
from app.leads.turnstile import Turnstile
from app.observability.tracing import tracing
from app.services.security_events import SecurityEventWriter, first_in_window
from llm_orchestrator.classify import Classifier

logger = structlog.get_logger(__name__)

DAY = 86_400
# A burst of the same refusal from one visitor is recorded once per this many seconds.
REFUSAL_EVENT_WINDOW = 3600

Outcome = Literal["stored", "duplicate", "refused"]
Refusal = Literal["turnstile", "visitor_cap", "global_cap"]


class Notifier(Protocol):
    def notify(self) -> None: ...


@dataclass(frozen=True)
class LeadLimits:
    per_visitor_day: int = 2
    global_day: int = 30
    dedupe_hours: int = 24
    retention_days: int = 365


@dataclass(frozen=True)
class SubmitResult:
    outcome: Outcome
    lead_id: str | None = None
    status: str | None = None
    refusal: Refusal | None = None


class LeadService:
    def __init__(
        self,
        store: LeadStore,
        outbox: Notifier,
        redis: Redis,
        turnstile: Turnstile,
        classifier: Classifier,
        facts: FitFactsSource,
        events: SecurityEventWriter,
        *,
        salt: str,
        limits: LeadLimits | None = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._store = store
        self._outbox = outbox
        self._redis = redis
        self._turnstile = turnstile
        self._classifier = classifier
        self._facts = facts
        self._events = events
        self._salt = salt
        self._limits = limits or LeadLimits()
        self._clock = clock

    async def submit(
        self,
        lead: LeadIn,
        *,
        source: LeadSource,
        visitor_hash: str,
        turnstile_token: str | None = None,
        conversation_id: str | None = None,
        summary: str | None = None,
    ) -> SubmitResult:
        # Lead fields are masked in every trace; the run records outcome and triage only.
        with tracing().run(
            "lead-submit",
            "leads",
            input=lead.model_dump(include={"name", "email", "company", "role", "message"}),
            session_id=conversation_id,
            metadata={"source": source},
        ) as trace:
            result = await self._submit(
                lead,
                source=source,
                visitor_hash=visitor_hash,
                turnstile_token=turnstile_token,
                conversation_id=conversation_id,
                summary=summary,
            )
            trace.tag(f"outcome:{result.outcome}")
            trace.meta(outcome=result.outcome, status=result.status, kind=result.refusal)
            return result

    async def _submit(
        self,
        lead: LeadIn,
        *,
        source: LeadSource,
        visitor_hash: str,
        turnstile_token: str | None,
        conversation_id: str | None,
        summary: str | None,
    ) -> SubmitResult:
        now = self._clock()
        hashed = email_hash(lead.email, self._salt)
        verified = await self._turnstile.verify(turnstile_token)
        if not verified.ok:
            await self._refused(visitor_hash, f"turnstile:{verified.reason}", "low")
            return self._log(SubmitResult("refused", refusal="turnstile"), source, hashed)
        email_key, company_key = dedupe_key(lead.email), dedupe_key(lead.company)
        since = now - timedelta(hours=self._limits.dedupe_hours)
        existing = await self._store.recent_duplicate(email_key, company_key, since)
        if existing is not None:
            await self._store.merge_duplicate(existing.id, now)
            result = SubmitResult("duplicate", lead_id=existing.id, status=existing.status)
            return self._log(result, source, hashed)
        refusal = await self._over_cap(visitor_hash, now)
        if refusal is not None:
            await self._refused(visitor_hash, f"lead_cap:{refusal.removesuffix('_cap')}", "medium")
            return self._log(SubmitResult("refused", refusal=refusal), source, hashed)
        triaged = await triage(self._classifier, lead)
        fields = LeadFields(
            name=lead.name,
            email=lead.email,
            company=lead.company,
            role=lead.role,
            message=lead.message,
            details=LeadDetails(**lead.details()),
            source=source,
            status="suppressed" if triaged.label == "spam" else "new",
            visitor_hash=visitor_hash,
            email_key=email_key,
            company_key=company_key,
            summary=summary,
            triage=triaged.label,
            triage_confidence=triaged.confidence,
            fit_note=fit_note(lead, await self._facts.facts()),
            conversation_id=conversation_id,
            created_at=now,
            updated_at=now,
            expires_at=now + timedelta(days=self._limits.retention_days),
        )
        stored = await self._store.insert(fields)
        await self._count(visitor_hash, now)
        if stored.status == "new":
            self._outbox.notify()
        result = SubmitResult("stored", lead_id=stored.id, status=stored.status)
        return self._log(result, source, hashed, triage=triaged.label)

    async def _over_cap(self, visitor_hash: str, now: datetime) -> Refusal | None:
        visitor, total = await self._redis.mget(
            lead_visitor_key(visitor_hash), lead_global_key(_day(now))
        )
        if int(visitor or 0) >= self._limits.per_visitor_day:
            return "visitor_cap"
        if int(total or 0) >= self._limits.global_day:
            return "global_cap"
        return None

    async def _count(self, visitor_hash: str, now: datetime) -> None:
        visitor_key = lead_visitor_key(visitor_hash)
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.incr(visitor_key)
            pipe.ttl(visitor_key)
            pipe.incr(lead_global_key(_day(now)))
            pipe.expire(lead_global_key(_day(now)), 2 * DAY)
            _, ttl, _, _ = await pipe.execute()
        # The visitor's day starts with their first lead.
        if int(ttl) < 0:
            await self._redis.expire(visitor_key, DAY)

    async def _refused(
        self, visitor_hash: str, rule: str, severity: Literal["low", "medium"]
    ) -> None:
        if await first_in_window(self._redis, f"lead:{visitor_hash}:{rule}", REFUSAL_EVENT_WINDOW):
            await self._events.record(
                "lead_refused", severity, rules=[rule], visitor_hash=visitor_hash
            )

    @staticmethod
    def _log(
        result: SubmitResult, source: str, hashed: str, *, triage: str | None = None
    ) -> SubmitResult:
        logger.info(
            "lead submitted",
            outcome=result.outcome,
            source=source,
            lead_id=result.lead_id,
            status=result.status,
            refusal=result.refusal,
            triage=triage,
            email_hash=hashed,
        )
        return result


def _day(now: datetime) -> str:
    return now.astimezone(UTC).strftime("%Y%m%d")


def lead_limits(settings: Settings) -> LeadLimits:
    return LeadLimits(
        per_visitor_day=settings.lead_visitor_daily_cap,
        global_day=settings.lead_daily_cap,
        retention_days=settings.lead_retention_days,
    )


def build_lead_service(
    settings: Settings, resources: Resources, llm: LLMServices, outbox: Notifier
) -> LeadService:
    return LeadService(
        resources.leads,
        outbox,
        resources.redis,
        Turnstile(
            settings.turnstile_secret_key.get_secret_value(), url=settings.turnstile_verify_url
        ),
        llm.classifier,
        PublicFitFacts(resources.profile),
        resources.security_events,
        salt=settings.visitor_hash_salt.get_secret_value(),
        limits=lead_limits(settings),
    )
