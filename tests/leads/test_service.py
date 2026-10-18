"""Opportunities T3: dedupe, caps, Turnstile, triage and the fit note; no PII in logs
(AC5, AC6, AC7)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import structlog
from fakeredis import FakeAsyncRedis
from structlog.testing import capture_logs

from app.leads import service as lead_service
from app.leads.models import LeadIn
from app.leads.service import LeadLimits, LeadService
from app.leads.triage import FitFacts, fit_note
from app.leads.turnstile import Turnstile
from tests.leads.fakes import (
    MemoryLeadStore,
    Notifications,
    StaticFacts,
    TriageClassifier,
    siteverify,
)
from tests.public.fakes import MemorySecurityEvents

LEAD = LeadIn(
    name="Ravi Kumar",
    email="ravi.kumar@acme.example",
    company="Acme",
    role="Senior Backend Engineer",
    message="We build payment APIs in Python; the role is remote-friendly.",
)


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


class Setup:
    def __init__(
        self,
        *,
        label: str | Exception = "recruiter",
        secret: str = "",
        limits: LeadLimits | None = None,
        calls: list[dict[str, str]] | None = None,
    ) -> None:
        self.store = MemoryLeadStore()
        self.notes = Notifications()
        self.redis = FakeAsyncRedis(decode_responses=True)
        self.events = MemorySecurityEvents()
        self.clock = Clock()
        self.classifier = TriageClassifier(label)
        self.service = LeadService(
            self.store,
            self.notes,
            self.redis,
            Turnstile(secret, transport=siteverify(calls=calls)),
            self.classifier,
            StaticFacts(),
            self.events,
            salt="salt",
            limits=limits,
            clock=self.clock,
        )

    async def submit(self, lead: LeadIn = LEAD, **kwargs: Any) -> Any:
        return await self.service.submit(
            lead,
            source=kwargs.pop("source", "form"),
            visitor_hash=kwargs.pop("visitor", "v1"),
            **kwargs,
        )


async def test_a_new_lead_is_triaged_noted_stored_and_queued() -> None:
    s = Setup()
    result = await s.submit(summary="Ravi from Acme.")
    assert result.outcome == "stored" and result.status == "new"
    lead = s.store.leads[result.lead_id]
    assert (lead.triage, lead.triage_confidence) == ("recruiter", 0.8)
    assert lead.fit_note == "Matches role: Backend Engineer; location: Remote; skills: python."
    assert lead.summary == "Ravi from Acme." and lead.source == "form"
    assert lead.expires_at == s.clock.now + timedelta(days=365)
    assert s.notes.count == 1
    assert "Message: We build payment APIs" in s.classifier.seen[0]


async def test_duplicate_within_a_day_is_merged_without_a_second_email() -> None:
    s = Setup()
    first = await s.submit()
    s.clock.now += timedelta(hours=3)
    again = await s.submit(
        LEAD.model_copy(update={"email": "RAVI.kumar@acme.example", "company": " acme "}),
        visitor="v2",
    )
    assert again.outcome == "duplicate" and again.lead_id == first.lead_id
    assert len(s.store.leads) == 1 and s.store.leads[first.lead_id].duplicates == 1
    assert s.notes.count == 1
    s.clock.now += timedelta(hours=22)
    later = await s.submit(visitor="v2")
    assert later.outcome == "stored" and len(s.store.leads) == 2


async def test_third_lead_from_a_visitor_is_refused_with_one_event() -> None:
    s = Setup()
    for company in ("Acme", "Globex"):
        assert (await s.submit(LEAD.model_copy(update={"company": company}))).outcome == "stored"
    third = await s.submit(LEAD.model_copy(update={"company": "Initech"}))
    fourth = await s.submit(LEAD.model_copy(update={"company": "Umbrella"}))
    assert third.refusal == fourth.refusal == "visitor_cap"
    assert len(s.store.leads) == 2 and s.notes.count == 2
    [event] = s.events.events
    assert event["kind"] == "lead_refused" and event["rules"] == ["lead_cap:visitor"]
    assert event["visitor_hash"] == "v1"
    other = await s.submit(LEAD.model_copy(update={"company": "Initech"}), visitor="v2")
    assert other.outcome == "stored"
    s.clock.now += timedelta(days=1, minutes=1)
    assert await s.redis.ttl("lead:v:v1:day") > 0


async def test_global_cap_refuses_every_visitor() -> None:
    s = Setup(limits=LeadLimits(per_visitor_day=5, global_day=2))
    await s.submit(LEAD.model_copy(update={"company": "A"}), visitor="v1")
    await s.submit(LEAD.model_copy(update={"company": "B"}), visitor="v2")
    refused = await s.submit(LEAD.model_copy(update={"company": "C"}), visitor="v3")
    assert refused.refusal == "global_cap"
    assert s.events.events[0]["rules"] == ["lead_cap:global"]


async def test_turnstile_is_verified_only_with_a_secret() -> None:
    calls: list[dict[str, str]] = []
    s = Setup(secret="ts-secret", calls=calls)
    missing = await s.submit()
    invalid = await s.submit(turnstile_token="forged")
    assert missing.refusal == invalid.refusal == "turnstile"
    assert s.store.leads == {}
    valid = await s.submit(turnstile_token="good-token")
    assert valid.outcome == "stored"
    assert [c["response"] for c in calls] == ["forged", "good-token"]
    assert all(c["secret"] == "ts-secret" for c in calls)
    rules = [e["rules"] for e in s.events.events]
    assert rules == [["turnstile:missing"], ["turnstile:invalid"]]
    off = Setup(calls=calls)
    assert (await off.submit()).outcome == "stored"
    assert len(calls) == 2


async def test_turnstile_outage_refuses() -> None:
    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    turnstile = Turnstile("secret", transport=httpx.MockTransport(broken))
    result = await turnstile.verify("token")
    assert (result.ok, result.reason) == (False, "unavailable")


async def test_spam_is_suppressed_and_not_queued() -> None:
    s = Setup(label="spam")
    result = await s.submit()
    assert result.outcome == "stored" and result.status == "suppressed"
    assert s.notes.count == 0


async def test_triage_failure_files_the_lead_as_other() -> None:
    s = Setup(label=RuntimeError("classifier down"))
    result = await s.submit()
    lead = s.store.leads[result.lead_id]
    assert (lead.triage, lead.triage_confidence, lead.status) == ("other", None, "new")


def test_fit_note_without_overlap() -> None:
    lead = LEAD.model_copy(update={"role": "Chef", "message": "Kitchen work in Paris."})
    note = fit_note(lead, FitFacts(["Backend Engineer"], ["Remote"], ["python"]))
    assert note == "No overlap with preferred roles, locations or published skills."


async def test_logs_carry_a_hash_never_the_lead(monkeypatch: Any) -> None:
    s = Setup()
    with capture_logs() as logs:
        monkeypatch.setattr(lead_service, "logger", structlog.get_logger(lead_service.__name__))
        await s.submit()
        await s.submit()
    text = repr(logs)
    for secret in (LEAD.name, LEAD.email, LEAD.company, "payment APIs", "ravi"):
        assert secret not in text
    assert [line["outcome"] for line in logs] == ["stored", "duplicate"]
    assert all(len(line["email_hash"]) == 16 for line in logs)
