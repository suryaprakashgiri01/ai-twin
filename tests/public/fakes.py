"""Fakes and a wired public graph for public-agent tests: scripted models, no network."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from fakeredis import FakeAsyncRedis

from app.agents.graphs.public import PublicDeps, PublicSettings, build_public_graph
from app.agents.prompts.public import GENERATE_V1
from app.auth.visitor import Visitor
from app.guardrails.canary_hashes import StaticCanaryHashes, marker_hash
from app.guardrails.input import GuardSettings, InputGuard
from app.guardrails.output import OutputGuard
from app.guardrails.scope import INTENT_LABELS, ProfileTemplates, ScopeRouter
from app.kb.canaries import new_marker
from app.kb.retrieval.rerank import FusedOrderReranker
from app.kb.retrieval.retriever import Hit
from app.leads.capture import CaptureDeps, CaptureSettings
from app.leads.service import LeadService
from app.services.answer_cache import AnswerCache
from app.services.conversations import ConversationService, Turn
from app.services.owner_chat import ConversationMeta, ConversationSummary
from app.services.public_chat import PublicChatRequest, PublicChatService
from llm_orchestrator import Orchestrator, Tier
from llm_orchestrator.classify import Classification
from tests.fakes import MemoryConversationStore
from tests.ingestion.fakes import MemoryProfileStore
from tests.llm_orchestrator.fakes import Factory, inventory, orchestrator, spec

PUBLIC_FACT = "I built the observability pipeline on ClickHouse with OpenTelemetry collectors."
PRIVATE_FACT = "Quorvex scheduler retro: the private lesson about Raft timeouts."
ANSWER = "I built the observability pipeline on ClickHouse [1]."
EMAIL = "kavya.testpal@example.com"
MARKER = new_marker()


def public_hit(doc: str = "pub1", text: str = PUBLIC_FACT, tier: Tier = Tier.PUBLIC) -> Hit:
    return Hit(
        document_id=doc,
        ordinal=0,
        tier=tier,
        title="Observability platform",
        category="project",
        text=text,
        score=1.0,
        collection="kb_public",
    )


@dataclass
class FakeGuardModel:
    scores: list[float | Exception] = field(default_factory=lambda: [0.01])
    seen: list[str] = field(default_factory=list)

    async def score(self, text: str) -> float:
        self.seen.append(text)
        step = self.scores[min(len(self.seen) - 1, len(self.scores) - 1)]
        if isinstance(step, Exception):
            raise step
        return step


@dataclass
class ScriptedClassifier:
    label: str | Exception = "about_owner"
    seen: list[str] = field(default_factory=list)

    async def classify(
        self, text: str, *, question: str, labels: Mapping[str, str], tier: Tier
    ) -> Classification:
        self.seen.append(text)
        assert tier == Tier.PUBLIC and set(labels) == set(INTENT_LABELS)
        if isinstance(self.label, Exception):
            raise self.label
        return Classification(
            label=self.label, probabilities={self.label: 1.0}, confidence=0.9, model="fake"
        )


@dataclass
class FakeJudge:
    verdict: bool | Exception = True
    delay: float = 0.0
    calls: int = 0
    log: list[str] | None = None

    async def supported(self, *, question: str, context: str, answer: str) -> bool:
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.log is not None:
            self.log.append("guard_done")
        if isinstance(self.verdict, Exception):
            raise self.verdict
        return self.verdict


@dataclass
class MemorySecurityEvents:
    events: list[dict[str, Any]] = field(default_factory=list)

    async def record(
        self,
        kind: str,
        severity: str,
        *,
        rules: Sequence[str] = (),
        visitor_hash: str | None = None,
        details: Mapping[str, Any] | None = None,
        surface: str | None = None,
    ) -> None:
        self.events.append(
            {
                "kind": kind,
                "severity": severity,
                "rules": list(rules),
                "visitor_hash": visitor_hash,
                "details": dict(details or {}),
                "surface": surface,
            }
        )

    def kinds(self) -> list[str]:
        return [e["kind"] for e in self.events]


@dataclass
class FakePublicSearcher:
    hits: list[Hit] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def search(self, query: str, *, max_tier: Tier, limit: int) -> list[Hit]:
        self.calls.append({"query": query, "max_tier": max_tier, "limit": limit})
        return list(self.hits)


class PublicMemoryIndex:
    def __init__(self, store: MemoryConversationStore) -> None:
        self.store = store
        self.locked: set[str] = set()

    async def get(self, conversation_id: str) -> ConversationMeta | None:
        meta = self.store.conversations.get(conversation_id)
        if meta is None:
            return None
        turns = self.store.turns.get(conversation_id, {})
        stamps = [t.created_at for t in turns.values()]
        return ConversationMeta(
            id=conversation_id,
            channel=meta["channel"],
            visitor_hash=meta["visitor_hash"],
            created_at=min(stamps),
            updated_at=max(stamps),
            message_count=max(turns, default=0),
        )

    async def list_owner(self, limit: int) -> list[ConversationSummary]:
        return []

    async def list_channel(
        self, limit: int, channel: str, visitor_hash: str | None = None
    ) -> list[ConversationSummary]:
        return []

    async def by_name(self, name: str) -> ConversationMeta | None:
        return None

    async def names_like(self, base: str) -> set[str]:
        return set()

    async def set_title(
        self, conversation_id: str, name: str | None, description: str | None
    ) -> None:
        return None

    async def lock_name(self, conversation_id: str) -> None:
        self.locked.add(conversation_id)

    async def save_refresh(
        self, conversation_id: str, name: str | None, description: str, titled_turns: int
    ) -> None:
        await self.set_title(conversation_id, name, description)

    async def save_summary(
        self, conversation_id: str, summary: str, summarized_turns: int, tier: str
    ) -> None:
        return None

    async def turns(self, conversation_id: str, limit: int) -> list[Turn]:
        return [t for _, t in sorted(self.store.turns.get(conversation_id, {}).items())][:limit]


@dataclass
class Public:
    service: PublicChatService
    factory: Factory
    orchestrator: Orchestrator
    guard_model: FakeGuardModel
    classifier: ScriptedClassifier
    searcher: FakePublicSearcher
    judge: FakeJudge
    profile: MemoryProfileStore
    events: MemorySecurityEvents
    conversations: ConversationService
    store: MemoryConversationStore
    redis: FakeAsyncRedis

    async def ask(
        self,
        message: str,
        *,
        visitor: str = "visitor-a",
        conversation_id: str | None = None,
        turnstile_token: str | None = None,
    ) -> list[tuple[str, dict[str, Any]]]:
        prepared = await self.service.prepare(
            PublicChatRequest(
                message=message, conversation_id=conversation_id, turnstile_token=turnstile_token
            ),
            Visitor(hash=visitor, forwarded=True),
        )
        events = [e async for e in self.service.events(prepared)]
        await self.conversations.drain()
        return events

    def generations(self) -> int:
        return self.factory.calls("gen")


def answer_of(events: list[tuple[str, dict[str, Any]]]) -> str:
    return "".join(d["text"] for n, d in events if n == "token")


def sse_events(body: str) -> list[tuple[str, dict[str, Any]]]:
    events = []
    for frame in body.strip().split("\n\n"):
        name, data = frame.split("\n", 1)
        events.append((name.removeprefix("event: "), json.loads(data.removeprefix("data: "))))
    return events


def make_public(
    *,
    generator: list[Any] | None = None,
    label: str | Exception = "about_owner",
    hits: list[Hit] | None = None,
    judge: FakeJudge | None = None,
    guard_scores: list[float | Exception] | None = None,
    searcher: Any = None,
    has_resume: bool = True,
    cache_ttl: int = 3600,
    leads: LeadService | None = None,
    checkpointer: Any = None,
    max_turns: int = 6,
    reply_time: str | None = None,
) -> Public:
    factory = Factory({"gen": generator or [ANSWER]})
    orch = orchestrator(
        inventory(spec("gen", roles=["generator"], max_tier="public")), factory, max_attempts=1
    )
    guard_model = FakeGuardModel(guard_scores or [0.01])
    classifier = ScriptedClassifier(label)
    judge = judge or FakeJudge()
    profile = MemoryProfileStore()
    events = MemorySecurityEvents()

    async def resume_available() -> bool:
        return has_resume

    redis = FakeAsyncRedis(decode_responses=True)
    store = MemoryConversationStore()
    conversations = ConversationService(redis, store, visitor_salt="salt", retry_base_delay=0)
    search = searcher or FakePublicSearcher(hits=list(hits if hits is not None else [public_hit()]))
    output_guard = OutputGuard(
        canaries=StaticCanaryHashes([marker_hash(MARKER)]),
        system_prompts=[GENERATE_V1],
        allowlist=[EMAIL],
        judge=judge,
    )

    async def profile_reply_time() -> str | None:
        return reply_time

    capture = None
    if leads is not None:
        capture = CaptureDeps(
            orchestrator=orch,
            service=leads,
            output_guard=output_guard,
            reply_time=profile_reply_time,
            settings=CaptureSettings(max_turns=max_turns, owner_name="Kavya"),
        )
    deps = PublicDeps(
        input_guard=InputGuard(guard_model, GuardSettings(max_chars=800)),
        router=ScopeRouter(classifier),
        templates=ProfileTemplates(profile, resume_available),
        retriever=search,
        reranker=FusedOrderReranker(),
        orchestrator=orch,
        output_guard=output_guard,
        events=events,
        settings=PublicSettings(),
        capture=capture,
    )
    service = PublicChatService(
        build_public_graph(deps, checkpointer if capture is not None else None),
        conversations,
        PublicMemoryIndex(store),
        AnswerCache(redis, cache_ttl),
    )
    return Public(
        service,
        factory,
        orch,
        guard_model,
        classifier,
        search,
        judge,
        profile,
        events,
        conversations,
        store,
        redis,
    )


async def collect(stream: AsyncIterator[str]) -> str:
    return "".join([chunk async for chunk in stream])
