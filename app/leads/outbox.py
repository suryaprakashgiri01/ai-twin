"""The lead outbox: a background worker that emails stored leads to the owner.

A lead is stored first, then the worker is woken; the visitor's reply never waits on SMTP. Each
due lead gets one attempt per pass. A failure schedules the next attempt with backoff; after the
last attempt the lead becomes `failed` and a `lead_delivery_failed` event is written. Delivery
state lives on the lead, so a restart picks up where it left off. Suppressed (spam) leads are
never emailed one by one: they go into one digest a day, covering the days before today in
`OWNER_TIMEZONE`. Hourly housekeeping hooks (pruning public drafts) run on the same loop.

Logs carry lead ids, attempt counts and error type names only.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import structlog
from redis.asyncio import Redis

from app.cache.redis import lead_digest_key
from app.core.config import Settings
from app.db.documents import utcnow
from app.leads.email import Mailer, MailError, MailSettings, SmtpMailer, render_digest, render_lead
from app.leads.store import LeadStore, StoredLead
from app.services.security_events import SecurityEventWriter

logger = structlog.get_logger(__name__)

Hook = Callable[[datetime], Awaitable[object]]


@dataclass(frozen=True)
class OutboxSettings:
    max_attempts: int = 3
    # Wait before attempt 2, 3, ...; the first attempt is immediate.
    backoff_seconds: tuple[float, ...] = (60.0, 300.0)
    poll_seconds: float = 30.0
    batch: int = 20
    housekeeping_seconds: float = 3600.0
    timezone: ZoneInfo = field(default_factory=lambda: ZoneInfo("UTC"))


def _error_code(exc: Exception) -> str:
    return exc.code if isinstance(exc, MailError) else type(exc).__name__


class LeadOutbox:
    def __init__(
        self,
        store: LeadStore,
        mailer: Mailer | None,
        events: SecurityEventWriter,
        redis: Redis,
        settings: OutboxSettings | None = None,
        *,
        hooks: Sequence[Hook] = (),
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._store = store
        self._mailer = mailer
        self._events = events
        self._redis = redis
        self._s = settings or OutboxSettings()
        self._hooks = list(hooks)
        self._clock = clock
        self._wake = asyncio.Event()
        self._last_housekeeping: datetime | None = None
        self._warned = False

    @property
    def configured(self) -> bool:
        return self._mailer is not None

    def notify(self) -> None:
        """A lead was stored: run a pass now instead of at the next poll."""
        self._wake.set()

    # --- one pass -----------------------------------------------------------------------------

    async def tick(self, now: datetime | None = None) -> None:
        now = now or self._clock()
        await self.deliver_due(now)
        await self.digest(now)
        await self.housekeeping(now)

    async def deliver_due(self, now: datetime) -> int:
        """One attempt for every due lead. Returns how many were emailed."""
        if self._mailer is None:
            if not self._warned:
                logger.warning("lead email not configured; leads are stored, not emailed")
                self._warned = True
            return 0
        sent = 0
        for lead in await self._store.due(now, self._s.batch):
            sent += await self._attempt(self._mailer, lead, now)
        return sent

    async def _attempt(self, mailer: Mailer, lead: StoredLead, now: datetime) -> int:
        attempts = lead.attempts + 1
        try:
            await mailer.send(render_lead(lead, mailer, self._s.timezone))
        except Exception as exc:
            await self._failed(lead, attempts, _error_code(exc), now)
            return 0
        await self._store.update(
            lead.id,
            {
                "emailed_at": now,
                "attempts": attempts,
                "last_error_code": None,
                "next_attempt_at": None,
                "updated_at": now,
            },
        )
        logger.info("lead emailed", lead_id=lead.id, attempts=attempts)
        return 1

    async def _failed(self, lead: StoredLead, attempts: int, code: str, now: datetime) -> None:
        changes: dict[str, object] = {
            "attempts": attempts,
            "last_error_code": code,
            "updated_at": now,
        }
        final = attempts >= self._s.max_attempts
        if final:
            changes |= {"status": "failed", "next_attempt_at": None}
        else:
            backoff = self._s.backoff_seconds
            wait = backoff[min(attempts - 1, len(backoff) - 1)] if backoff else 0.0
            changes["next_attempt_at"] = now + timedelta(seconds=wait)
        await self._store.update(lead.id, changes)
        logger.warning(
            "lead email failed", lead_id=lead.id, attempts=attempts, error_code=code, final=final
        )
        if final:
            await self._events.record(
                "lead_delivery_failed",
                "high",
                rules=[f"smtp:{code}"],
                details={"lead_id": lead.id, "attempts": attempts},
            )

    async def digest(self, now: datetime) -> int:
        """Once a day: one email listing suppressed leads from before today. Returns how many
        leads it covered (0 when nothing was due or today's digest already went)."""
        if self._mailer is None:
            return 0
        local = now.astimezone(self._s.timezone)
        midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
        leads = await self._store.undigested_suppressed(midnight)
        if not leads:
            return 0
        key = lead_digest_key(local.strftime("%Y%m%d"))
        if not await self._redis.set(key, 1, ex=2 * 86_400, nx=True):
            return 0
        try:
            await self._mailer.send(render_digest(leads, self._mailer, self._s.timezone))
        except Exception as exc:
            # Let the next pass try again.
            await self._redis.delete(key)
            logger.warning("lead digest failed", error_code=_error_code(exc), leads=len(leads))
            return 0
        for lead in leads:
            await self._store.update(lead.id, {"digested_at": now})
        logger.info("lead digest sent", leads=len(leads))
        return len(leads)

    async def housekeeping(self, now: datetime) -> None:
        last = self._last_housekeeping
        if last is not None and (now - last).total_seconds() < self._s.housekeeping_seconds:
            return
        self._last_housekeeping = now
        for hook in self._hooks:
            try:
                await hook(now)
            except Exception as exc:
                logger.warning("lead housekeeping failed", error_type=type(exc).__name__)

    # --- the worker ---------------------------------------------------------------------------

    async def run(self) -> None:
        while True:
            # Cleared before the pass, so a lead stored during it wakes the next one.
            self._wake.clear()
            try:
                await self.tick()
            except Exception as exc:
                logger.error("lead outbox pass failed", error_type=type(exc).__name__)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=self._s.poll_seconds)

    @contextlib.asynccontextmanager
    async def running(self) -> AsyncIterator[LeadOutbox]:
        task = asyncio.create_task(self.run(), name="lead-outbox")
        try:
            yield self
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


def build_outbox(
    settings: Settings,
    store: LeadStore,
    events: SecurityEventWriter,
    redis: Redis,
    hooks: Sequence[Hook] = (),
) -> LeadOutbox:
    mail = MailSettings.from_settings(settings)
    return LeadOutbox(
        store,
        SmtpMailer(mail) if mail is not None else None,
        events,
        redis,
        OutboxSettings(timezone=ZoneInfo(settings.owner_timezone)),
        hooks=hooks,
    )
