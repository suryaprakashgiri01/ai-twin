"""Fakes for opportunity tests: an in-memory lead store, a recording SMTP stand-in, a scripted
triage classifier and a fake Turnstile siteverify. No network."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from email.message import EmailMessage
from typing import Any

import httpx

from app.db.documents import LeadFields
from app.leads.email import MailError
from app.leads.store import StoredLead
from app.leads.triage import FitFacts
from llm_orchestrator import Tier
from llm_orchestrator.classify import Classification

OWNER = "owner@example.org"


class MemoryLeadStore:
    def __init__(self) -> None:
        self.leads: dict[str, StoredLead] = {}

    async def insert(self, fields: LeadFields) -> StoredLead:
        lead = StoredLead(id=fields.uid, **fields.model_dump())
        self.leads[lead.id] = lead
        return lead

    async def get(self, lead_id: str) -> StoredLead | None:
        return self.leads.get(lead_id)

    async def update(self, lead_id: str, changes: Mapping[str, Any]) -> StoredLead | None:
        lead = self.leads.get(lead_id)
        if lead is None:
            return None
        self.leads[lead_id] = lead.model_copy(update=dict(changes))
        return self.leads[lead_id]

    async def recent_duplicate(
        self, email_key: str, company_key: str, since: datetime
    ) -> StoredLead | None:
        found = [
            lead
            for lead in self.leads.values()
            if lead.email_key == email_key
            and lead.company_key == company_key
            and lead.created_at >= since
        ]
        return max(found, key=lambda lead: lead.created_at) if found else None

    async def merge_duplicate(self, lead_id: str, now: datetime) -> None:
        lead = self.leads[lead_id]
        self.leads[lead_id] = lead.model_copy(
            update={"duplicates": lead.duplicates + 1, "updated_at": now}
        )

    async def due(self, now: datetime, limit: int) -> list[StoredLead]:
        due = [
            lead
            for lead in self.leads.values()
            if lead.status == "new"
            and lead.emailed_at is None
            and (lead.next_attempt_at is None or lead.next_attempt_at <= now)
        ]
        return sorted(due, key=lambda lead: lead.created_at)[:limit]

    async def undigested_suppressed(self, before: datetime) -> list[StoredLead]:
        return [
            lead
            for lead in self.leads.values()
            if lead.status == "suppressed" and lead.digested_at is None and lead.created_at < before
        ]

    async def list(self, status: str | None, triage: str | None, limit: int) -> list[StoredLead]:
        rows = [
            lead
            for lead in self.leads.values()
            if (status is None or lead.status == status)
            and (triage is None or lead.triage == triage)
        ]
        return sorted(rows, key=lambda lead: lead.created_at, reverse=True)[:limit]


@dataclass
class FakeSmtp:
    """Records every message with its envelope recipient; fails while `failures` > 0."""

    owner: str = OWNER
    sender: str = OWNER
    failures: int = 0
    sent: list[tuple[list[str], EmailMessage]] = field(default_factory=list)
    attempts: int = 0

    async def send(self, message: EmailMessage) -> None:
        self.attempts += 1
        if self.failures:
            self.failures -= 1
            raise MailError("SMTPConnectError")
        self.sent.append(([self.owner], message))

    def recipients(self) -> set[str]:
        """Every address mail went to: envelope plus To/Cc/Bcc headers."""
        found: set[str] = set()
        for envelope, message in self.sent:
            found |= set(envelope)
            for header in ("To", "Cc", "Bcc"):
                if message[header]:
                    found.add(str(message[header]))
        return found


@dataclass
class Notifications:
    count: int = 0

    def notify(self) -> None:
        self.count += 1


@dataclass
class TriageClassifier:
    label: str | Exception = "recruiter"
    seen: list[str] = field(default_factory=list)

    async def classify(
        self, text: str, *, question: str, labels: Mapping[str, str], tier: Tier
    ) -> Classification:
        assert tier == Tier.PUBLIC
        self.seen.append(text)
        if isinstance(self.label, Exception):
            raise self.label
        return Classification(
            label=self.label, probabilities={self.label: 0.8}, confidence=0.8, model="fake"
        )


@dataclass
class StaticFacts:
    value: FitFacts = field(
        default_factory=lambda: FitFacts(["Backend Engineer"], ["Remote", "Bengaluru"], ["python"])
    )

    async def facts(self) -> FitFacts:
        return self.value


def siteverify(
    valid: str = "good-token", calls: list[dict[str, str]] | None = None
) -> httpx.MockTransport:
    """A fake Turnstile siteverify that accepts exactly `valid`."""

    def handler(request: httpx.Request) -> httpx.Response:
        form = dict(httpx.QueryParams(request.content.decode()))
        if calls is not None:
            calls.append(form)
        if form.get("response") == valid:
            return httpx.Response(200, json={"success": True})
        return httpx.Response(
            200, json={"success": False, "error-codes": ["invalid-input-response"]}
        )

    return httpx.MockTransport(handler)
