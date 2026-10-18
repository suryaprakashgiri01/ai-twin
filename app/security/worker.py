"""The alert worker: a background loop (the lead outbox's pattern) that emails security events.

Each pass checks every immediate policy (skipping one that is cooling down), emails what is due
and marks those events `alerted_at`; then, once a day after `ALERT_DIGEST_HOUR` in
`OWNER_TIMEZONE`, it sends the digest of every event not yet digested and marks them
`digested_at`. The visitor path never waits on SMTP: events are stored first and read here.

If SMTP fails the error is logged at error level and nothing is marked: the same events go out
on a later pass. Mail goes to `ALERT_EMAIL_TO` (else `LEAD_EMAIL_TO`) only, through the lead
mailer and its recipient guard. Logs carry policy names, counts and error type names only.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from email.message import EmailMessage
from zoneinfo import ZoneInfo

import structlog
from redis.asyncio import Redis

from app.cache.redis import alert_cooldown_key, alert_digest_key, alert_last_key
from app.core.config import Settings
from app.db.documents import utcnow
from app.leads.email import Mailer, MailError, MailSettings, SmtpMailer
from app.observability.metrics import ALERT_EMAILS, EMAILS
from app.security.alerts import (
    POLICIES,
    EventStore,
    MongoEventStore,
    Policy,
    StoredEvent,
    render_alert,
    render_digest,
    render_test,
    should_alert,
)

logger = structlog.get_logger(__name__)

DIGEST_POLICY = "digest"
TEST_POLICY = "test"


@dataclass(frozen=True)
class AlertSettings:
    cooldown: timedelta = timedelta(minutes=30)
    digest_hour: int = 9
    poll_seconds: float = 30.0
    timezone: ZoneInfo = field(default_factory=lambda: ZoneInfo("UTC"))
    # Pending events older than this are left to the digest (bounds a first start and a
    # long SMTP outage); the digest itself looks back this far at most.
    pending_lookback: timedelta = timedelta(hours=6)
    digest_lookback: timedelta = timedelta(days=3)


@dataclass(frozen=True)
class PolicyStatus:
    name: str
    mode: str
    rule: str
    cooldown_seconds: int
    last_sent: datetime | None


def _parse_time(raw: object) -> datetime | None:
    if isinstance(raw, bytes):
        raw = raw.decode()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw))
    except ValueError:
        return None


def _error_code(exc: Exception) -> str:
    return exc.code if isinstance(exc, MailError) else type(exc).__name__


class AlertWorker:
    def __init__(
        self,
        store: EventStore,
        mailer: Mailer | None,
        redis: Redis,
        settings: AlertSettings | None = None,
        *,
        policies: Sequence[Policy] = POLICIES,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._store = store
        self._mailer = mailer
        self._redis = redis
        self._s = settings or AlertSettings()
        self._policies = list(policies)
        self._clock = clock
        self._warned = False

    @property
    def configured(self) -> bool:
        return self._mailer is not None

    @property
    def settings(self) -> AlertSettings:
        return self._s

    # --- one pass -----------------------------------------------------------------------------

    async def tick(self, now: datetime | None = None) -> None:
        now = now or self._clock()
        await self.alert_due(now)
        await self.digest(now)

    async def alert_due(self, now: datetime) -> int:
        """Email every policy that is due and not cooling down. Returns emails sent."""
        if self._mailer is None:
            if not self._warned:
                logger.warning("alert email not configured; security events are stored only")
                self._warned = True
            return 0
        sent = 0
        for policy in self._policies:
            if policy.mode != "digest" and await self._alert(self._mailer, policy, now):
                sent += 1
        return sent

    async def _alert(self, mailer: Mailer, policy: Policy, now: datetime) -> bool:
        if await self._redis.exists(alert_cooldown_key(policy.name)):
            return False
        pending = await self._store.pending(policy.kinds, now - self._s.pending_lookback)
        last = await self._last_sent(policy.name)
        if not should_alert(policy, pending, now, last, self._s.cooldown):
            return False
        follow_up = last is not None and all(e.created_at > last for e in pending)
        message = render_alert(
            policy,
            pending,
            mailer,
            self._s.timezone,
            cooldown=self._s.cooldown,
            follow_up=follow_up,
        )
        if not await self._send(mailer, policy.name, message, len(pending)):
            return False
        await self._store.mark_alerted([e.id for e in pending], now)
        seconds = int(self._s.cooldown.total_seconds())
        await self._redis.set(alert_cooldown_key(policy.name), now.isoformat(), ex=seconds)
        await self._redis.set(alert_last_key(policy.name), now.isoformat(), ex=7 * 86_400)
        return True

    async def _last_sent(self, policy: str) -> datetime | None:
        return _parse_time(await self._redis.get(alert_last_key(policy)))

    async def digest(self, now: datetime) -> int:
        """Once a local day after the digest hour. Returns how many events it covered."""
        if self._mailer is None:
            return 0
        local = now.astimezone(self._s.timezone)
        if local.hour < self._s.digest_hour:
            return 0
        key = alert_digest_key(local.strftime("%Y%m%d"))
        if not await self._redis.set(key, now.isoformat(), ex=2 * 86_400, nx=True):
            return 0
        events = await self._store.undigested(now - self._s.digest_lookback, now)
        if not events:
            logger.info("security digest skipped: nothing to report")
            return 0
        message = render_digest(events, local.date(), self._mailer, self._s.timezone)
        if not await self._send(self._mailer, DIGEST_POLICY, message, len(events)):
            # Let a later pass try again.
            await self._redis.delete(key)
            return 0
        await self._store.mark_digested([e.id for e in events], now)
        return len(events)

    async def send_test(self) -> bool:
        """One test email (the admin route audits it). False when SMTP is not configured or
        the send failed."""
        if self._mailer is None:
            return False
        message = render_test(self._mailer, self._s.timezone, self._clock())
        return await self._send(self._mailer, TEST_POLICY, message, 0)

    async def _send(self, mailer: Mailer, policy: str, message: EmailMessage, events: int) -> bool:
        kind = "alert_digest" if policy == DIGEST_POLICY else "alert"
        try:
            await mailer.send(message)
        except Exception as exc:
            ALERT_EMAILS.labels(policy, "failed").inc()
            EMAILS.labels(kind, "failed").inc()
            logger.error(
                "alert email failed", policy=policy, events=events, error_code=_error_code(exc)
            )
            return False
        ALERT_EMAILS.labels(policy, "sent").inc()
        EMAILS.labels(kind, "sent").inc()
        logger.info("alert email sent", policy=policy, events=events)
        return True

    # --- status -------------------------------------------------------------------------------

    async def status(self) -> list[PolicyStatus]:
        rows = []
        for policy in self._policies:
            ttl = await self._redis.ttl(alert_cooldown_key(policy.name))
            rows.append(
                PolicyStatus(
                    name=policy.name,
                    mode=policy.mode,
                    rule=policy.describe(),
                    cooldown_seconds=max(int(ttl), 0),
                    last_sent=await self._last_sent(policy.name),
                )
            )
        return rows

    async def last_digest(self, now: datetime | None = None) -> datetime | None:
        """When today's (else yesterday's) digest was handled, if it was."""
        now = now or self._clock()
        for days in (0, 1):
            day = (now - timedelta(days=days)).astimezone(self._s.timezone).strftime("%Y%m%d")
            when = _parse_time(await self._redis.get(alert_digest_key(day)))
            if when is not None:
                return when
        return None

    # --- the worker ---------------------------------------------------------------------------

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception as exc:
                logger.error("alert worker pass failed", error_type=type(exc).__name__)
            await asyncio.sleep(self._s.poll_seconds)

    @contextlib.asynccontextmanager
    async def running(self) -> AsyncIterator[AlertWorker]:
        task = asyncio.create_task(self.run(), name="alert-worker")
        try:
            yield self
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


def alert_settings(settings: Settings) -> AlertSettings:
    return AlertSettings(
        cooldown=timedelta(minutes=settings.alert_cooldown_minutes),
        digest_hour=settings.alert_digest_hour,
        poll_seconds=settings.alert_poll_seconds,
        timezone=ZoneInfo(settings.owner_timezone),
    )


def build_alert_worker(settings: Settings, redis: Redis) -> AlertWorker:
    mail = MailSettings.for_alerts(settings)
    return AlertWorker(
        MongoEventStore(),
        SmtpMailer(mail) if mail is not None else None,
        redis,
        alert_settings(settings),
    )


__all__ = ["AlertSettings", "AlertWorker", "PolicyStatus", "StoredEvent", "build_alert_worker"]
