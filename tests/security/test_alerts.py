"""observability-alerts T5: alert policies, cooldowns, the digest and SMTP failures (AC7-AC9),
with an in-memory event store, fake Redis and a fake SMTP that records envelope recipients."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from email.message import EmailMessage
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import structlog
from fakeredis import FakeAsyncRedis

from app.cache.redis import alert_cooldown_key
from app.core.config import Settings
from app.db.documents import SecurityEvent
from app.leads.email import MailSettings
from app.security.alerts import _stored
from app.security.worker import AlertSettings, AlertWorker
from tests.leads.fakes import OWNER, FakeSmtp
from tests.security.fakes import T0, MemoryEventStore, minutes

KOLKATA = ZoneInfo("Asia/Kolkata")


@dataclass
class Alerts:
    worker: AlertWorker
    store: MemoryEventStore
    smtp: FakeSmtp
    redis: FakeAsyncRedis

    def subjects(self) -> list[str]:
        return [str(m["Subject"]) for _, m in self.smtp.sent]

    def last(self) -> str:
        return text_of(self.smtp.sent[-1][1])

    async def expire_cooldown(self, policy: str) -> None:
        await self.redis.delete(alert_cooldown_key(policy))


def text_of(message: EmailMessage) -> str:
    plain = message.get_body(("plain",))
    html = message.get_body(("html",))
    assert plain is not None and html is not None
    return f"{message['Subject']}\n{plain.get_content()}\n{html.get_content()}"


def make_alerts(*, digest_hour: int = 9, smtp: FakeSmtp | None = None) -> Alerts:
    store, smtp, redis = MemoryEventStore(), smtp or FakeSmtp(), FakeAsyncRedis()
    worker = AlertWorker(
        store,
        smtp,
        redis,
        AlertSettings(cooldown=minutes(30), digest_hour=digest_hour, timezone=KOLKATA),
        clock=lambda: T0,
    )
    return Alerts(worker, store, smtp, redis)


# --- immediate policies -----------------------------------------------------------------------


async def test_a_canary_hit_sends_one_immediate_email() -> None:
    a = make_alerts()
    a.store.add(
        "canary_hit", T0, severity="critical", rules=["canary_marker"], visitor_hash="v" * 64
    )
    assert await a.worker.alert_due(T0 + minutes(0.5)) == 1
    assert a.subjects() == ["ai-twin alert · critical · canary_hit"]
    body = a.last()
    assert "Events: 1" in body and "canary_marker x1" in body
    assert "vvvvvvvv" in body and "v" * 9 not in body, "only an 8-character hash prefix"
    assert await a.worker.alert_due(T0 + minutes(1)) == 0, "already alerted"


async def test_three_output_blocks_alert_once_and_a_fourth_waits_for_the_cooldown() -> None:
    a = make_alerts()
    for i in range(2):
        a.store.add("output_blocked", T0 + minutes(i), rules=["identifier_leak"])
    assert await a.worker.alert_due(T0 + minutes(2)) == 0, "two is under the threshold"
    a.store.add("output_blocked", T0 + minutes(3), rules=["prompt_leak"])
    assert await a.worker.alert_due(T0 + minutes(3)) == 1
    assert "Events: 3" in a.last()

    a.store.add("output_blocked", T0 + minutes(5), rules=["prompt_leak"])
    assert await a.worker.alert_due(T0 + minutes(6)) == 0, "inside the cooldown"
    assert len(a.smtp.sent) == 1

    await a.expire_cooldown("output_blocked")
    assert await a.worker.alert_due(T0 + minutes(34)) == 1
    follow_up = a.last()
    assert "Events: 1 since the last alert" in follow_up


async def test_below_threshold_events_are_left_to_the_digest() -> None:
    a = make_alerts()
    a.store.add("output_blocked", T0, rules=["identifier_leak"])
    a.store.add("output_blocked", T0 + minutes(15), rules=["identifier_leak"])
    a.store.add("output_blocked", T0 + minutes(30), rules=["identifier_leak"])
    assert await a.worker.alert_due(T0 + minutes(31)) == 0, "never three within 10 minutes"


async def test_input_blocks_count_per_visitor() -> None:
    a = make_alerts()
    for i in range(5):
        a.store.add("input_blocked", T0 + minutes(i), visitor_hash=f"{i}" * 64)
    assert await a.worker.alert_due(T0 + minutes(5)) == 0, "five visitors, one each"
    for i in range(5):
        a.store.add("input_blocked", T0 + minutes(i), visitor_hash="a" * 64)
    assert await a.worker.alert_due(T0 + minutes(5)) == 1
    assert a.subjects() == ["ai-twin alert · medium · input_blocked"]


@pytest.mark.parametrize(
    ("kind", "count", "alerts"),
    [
        ("auth_failed", 4, 0),
        ("auth_failed", 5, 1),
        ("lead_delivery_failed", 1, 1),
        ("llm_unavailable", 5, 1),
        ("ingestion_failed", 4, 0),
        ("ingestion_failed", 5, 1),
        ("meta_probe", 10, 0),
        ("rate_limited", 10, 0),
        ("lead_refused", 10, 0),
        ("grounding_fail_open", 10, 0),
    ],
)
async def test_policy_table(kind: str, count: int, alerts: int) -> None:
    a = make_alerts()
    for i in range(count):
        a.store.add(kind, T0 + minutes(i / 2), surface="admin" if kind == "auth_failed" else None)
    assert await a.worker.alert_due(T0 + minutes(6)) == alerts


async def test_admin_auth_failures_name_their_surface() -> None:
    a = make_alerts()
    for i in range(5):
        a.store.add("auth_failed", T0 + minutes(i), rules=["INVALID_API_KEY"], surface="admin")
    await a.worker.alert_due(T0 + minutes(5))
    assert "Surface: admin x5" in a.last()


# --- the digest -------------------------------------------------------------------------------


async def test_the_digest_goes_once_a_day_after_the_hour_and_lists_counts() -> None:
    a = make_alerts(digest_hour=9)
    a.store.add("meta_probe", T0 - timedelta(hours=10), severity="low", rules=["scope:model"])
    a.store.add("canary_hit", T0 - timedelta(hours=9), severity="critical", rules=["canary"])
    await a.worker.alert_due(T0 - timedelta(hours=9))  # the canary alerted on its own
    a.smtp.sent.clear()

    early = T0 - timedelta(hours=1)  # 08:30 in Kolkata
    assert await a.worker.digest(early) == 0
    assert await a.worker.digest(T0) == 2
    assert a.subjects() == ["ai-twin daily security digest · 2026-10-19"]
    body = a.last()
    assert "- meta_probe · low: 1" in body
    assert "- canary_hit · critical: 1 (1 already alerted)" in body
    assert "scope:model x1" in body
    assert await a.worker.digest(T0 + timedelta(hours=2)) == 0, "once a day"


async def test_an_empty_digest_is_skipped_and_today_is_done() -> None:
    a = make_alerts()
    assert await a.worker.digest(T0) == 0
    assert a.smtp.sent == []
    a.store.add("meta_probe", T0 + minutes(5))
    assert await a.worker.digest(T0 + minutes(10)) == 0, "today's digest was handled"
    assert await a.worker.digest(T0 + timedelta(days=1)) == 1, "tomorrow's has it"


async def test_a_single_meta_probe_is_never_immediate_but_is_digested() -> None:
    a = make_alerts()
    a.store.add("meta_probe", T0 - minutes(10), severity="low")
    assert await a.worker.alert_due(T0) == 0
    assert await a.worker.digest(T0) == 1
    assert "meta_probe" in a.last()


# --- what an email may hold -------------------------------------------------------------------


async def test_emails_never_hold_text_names_or_addresses() -> None:
    visitor_text = "my name is Ravi Kumar, mail ravi.kumar@acme.example"
    doc = SecurityEvent.model_construct(
        id="6710f0f0f0f0f0f0f0f0f0f0",
        kind="canary_hit",
        severity="critical",
        details={
            "rules": ["canary_marker"],
            "conversation_id": "c" * 32,
            "path": "/admin/v1/leads/abc",
            "lead_id": "lead-123",
            "text": visitor_text,
        },
        visitor_hash="f" * 64,
        surface=None,
        created_at=T0,
        alerted_at=None,
        digested_at=None,
    )
    a = make_alerts()
    event = _stored(doc)
    a.store.events[event.id] = event
    a.store.add("lead_delivery_failed", T0, severity="high", rules=["smtp:SMTPConnectError"])
    await a.worker.alert_due(T0 + minutes(1))
    await a.worker.digest(T0 + minutes(2))
    assert len(a.smtp.sent) == 3
    for _, message in a.smtp.sent:
        rendered = message.as_string() + text_of(message)
        for leaked in ("Ravi", "ravi.kumar", "@acme", "c" * 32, "/admin/v1/leads/abc", "lead-123"):
            assert leaked not in rendered
        assert "f" * 9 not in rendered


async def test_rule_names_are_reduced_to_a_safe_alphabet() -> None:
    a = make_alerts()
    a.store.add("canary_hit", T0, rules=["<script>alert(1)</script> a@b.c"])
    await a.worker.alert_due(T0)
    rendered = a.smtp.sent[0][1].as_string()
    assert "<script>" not in rendered and "a@b.c" not in rendered


# --- recipients and SMTP failures -------------------------------------------------------------


async def test_alerts_go_to_the_owner_only() -> None:
    a = make_alerts()
    a.store.add("canary_hit", T0, severity="critical")
    a.store.add("meta_probe", T0 - minutes(1))
    await a.worker.tick(T0)
    assert len(a.smtp.sent) == 2
    assert a.smtp.recipients() == {OWNER}


def test_alert_recipient_is_alert_email_to_else_lead_email_to() -> None:
    base: dict[str, Any] = {
        "smtp_host": "smtp.example.org",
        "lead_email_to": "owner@example.org",
        "lead_email_from": "owner@example.org",
    }
    lead_only = MailSettings.for_alerts(Settings(_env_file=None, **base))
    assert lead_only is not None and lead_only.to == "owner@example.org"
    alerts = MailSettings.for_alerts(
        Settings(_env_file=None, **base, alert_email_to="alerts@example.org")
    )
    assert alerts is not None and alerts.to == "alerts@example.org"
    assert MailSettings.for_alerts(Settings(_env_file=None)) is None


async def test_smtp_failure_keeps_events_pending_for_the_next_pass() -> None:
    a = make_alerts(smtp=FakeSmtp(failures=2))
    canary = a.store.add("canary_hit", T0, severity="critical")
    a.store.add("meta_probe", T0 - minutes(1))
    with structlog.testing.capture_logs() as logs:
        await a.worker.tick(T0)
    assert a.smtp.sent == []
    assert a.store.events[canary.id].alerted_at is None
    assert all(e.digested_at is None for e in a.store.events.values())
    failures = [e for e in logs if e["event"] == "alert email failed"]
    assert [(e["log_level"], e["error_code"]) for e in failures] == [
        ("error", "SMTPConnectError"),
        ("error", "SMTPConnectError"),
    ]

    await a.worker.tick(T0 + minutes(1))
    assert len(a.smtp.sent) == 2, "the alert and the digest went on the retry"
    assert a.store.events[canary.id].alerted_at is not None


async def test_without_smtp_nothing_is_marked() -> None:
    store, redis = MemoryEventStore(), FakeAsyncRedis()
    worker = AlertWorker(store, None, redis, AlertSettings(timezone=KOLKATA))
    store.add("canary_hit", T0)
    await worker.tick(T0)
    assert all(e.alerted_at is None and e.digested_at is None for e in store.events.values())
    assert not worker.configured


async def test_status_reports_cooldowns_and_last_sent() -> None:
    a = make_alerts()
    a.store.add("canary_hit", T0, severity="critical")
    await a.worker.alert_due(T0)
    rows = {row.name: row for row in await a.worker.status()}
    assert rows["canary_hit"].cooldown_seconds > 0
    assert rows["canary_hit"].last_sent == T0
    assert rows["output_blocked"].rule == "immediate when >= 3 in 10 min, else digest"
    assert (
        rows["input_blocked"].rule == "immediate when >= 5 from one visitor in 10 min, else digest"
    )
    assert rows["meta_probe"].mode == "digest" and rows["meta_probe"].last_sent is None
