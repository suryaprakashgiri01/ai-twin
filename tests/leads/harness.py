"""A public assistant with lead capture wired to fakes: scripted models, an in-memory checkpointer,
a fake SMTP, triage classifier and Turnstile. `Capture.say` runs one turn in one conversation."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import httpx
from fakeredis import FakeAsyncRedis
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver

from app.leads.outbox import LeadOutbox, OutboxSettings
from app.leads.service import LeadLimits, LeadService
from app.leads.turnstile import Turnstile
from app.services.public_chat import public_serde
from tests.leads.fakes import FakeSmtp, MemoryLeadStore, StaticFacts, TriageClassifier
from tests.public.fakes import MemorySecurityEvents, Public, answer_of, make_public


def extraction(**fields: Any) -> str:
    """What the scripted model returns for one extraction call."""
    return json.dumps(fields)


@dataclass
class Capture:
    public: Public
    store: MemoryLeadStore
    smtp: FakeSmtp
    outbox: LeadOutbox
    events: MemorySecurityEvents
    triage: TriageClassifier
    saver: BaseCheckpointSaver[Any]
    redis: FakeAsyncRedis
    conversation_id: str | None = None
    replies: list[str] = field(default_factory=list)

    async def say(
        self, message: str, *, visitor: str = "visitor-a", token: str | None = None
    ) -> str:
        events = await self.public.ask(
            message, visitor=visitor, conversation_id=self.conversation_id, turnstile_token=token
        )
        names = [n for n, _ in events]
        assert "error" not in names, events
        self.conversation_id = events[0][1]["conversation_id"]
        reply = answer_of(events)
        self.replies.append(reply)
        return reply

    def intent(self, events: list[tuple[str, dict[str, Any]]]) -> str:
        return str(events[0][1]["intent"])

    async def pending(self) -> bool:
        config: Any = {"configurable": {"thread_id": f"public:{self.conversation_id}"}}
        snapshot = await self.public.service._graph.aget_state(config)
        return bool(snapshot.interrupts)


def make_capture(
    generator: list[Any],
    *,
    label: str = "opportunity",
    triage: str | Exception = "recruiter",
    smtp: FakeSmtp | None = None,
    turnstile_secret: str = "",
    siteverify: httpx.MockTransport | None = None,
    limits: LeadLimits | None = None,
    max_turns: int = 6,
    reply_time: str | None = "within two working days",
    redis: FakeAsyncRedis | None = None,
    saver: BaseCheckpointSaver[Any] | None = None,
) -> Capture:
    store = MemoryLeadStore()
    smtp = smtp or FakeSmtp()
    redis = redis or FakeAsyncRedis(decode_responses=True)
    events = MemorySecurityEvents()
    outbox = LeadOutbox(store, smtp, events, redis, OutboxSettings(backoff_seconds=(60, 300)))
    classifier = TriageClassifier(triage)
    service = LeadService(
        store,
        outbox,
        redis,
        Turnstile(turnstile_secret, transport=siteverify),
        classifier,
        StaticFacts(),
        events,
        salt="salt",
        limits=limits,
    )
    saver = saver or InMemorySaver(serde=public_serde())
    public = make_public(
        generator=generator,
        label=label,
        leads=service,
        checkpointer=saver,
        max_turns=max_turns,
        reply_time=reply_time,
    )
    # One security event log for the graph and the lead service.
    public.events.events = events.events
    return Capture(public, store, smtp, outbox, events, classifier, saver, redis)
