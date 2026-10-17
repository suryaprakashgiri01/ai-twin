"""An owner graph wired to scripted models and in-memory tools, for graph and service tests."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, cast

from fakeredis import FakeAsyncRedis
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessageChunk, BaseMessage, UsageMetadata
from langchain_core.outputs import ChatGenerationChunk, ChatResult
from pydantic import Field

from app.agents.generation import AnswerGenerator, ChatGenerator
from app.agents.graphs.owner import (
    OwnerDeps,
    OwnerGraph,
    OwnerSettings,
    OwnerState,
    build_owner_graph,
    initial_state,
)
from app.agents.streaming import StreamingGenerator
from app.agents.tools.profile import ProfileTool
from app.agents.tools.records import RecordGroup, RecordItem, RecordsQuery, RecordsResult
from app.kb.retrieval.rerank import FusedOrderReranker
from app.kb.retrieval.retriever import Hit, SearchFilters
from app.services.conversations import ConversationService, Turn
from app.services.profile import RecordIn
from llm_orchestrator import Orchestrator, Tier
from tests.fakes import MemoryConversationStore
from tests.ingestion.fakes import MemoryProfileStore
from tests.llm_orchestrator.fakes import Factory, inventory, orchestrator, spec

TODAY = date(2026, 10, 17)
CONF_TEXT = "SECRET-CONF offer letter: base 99 units"
PRIV_TEXT = "PRIV-NOTE learned raft leader election at the meetup"


def hit(doc: str, tier: Tier, text: str, ordinal: int = 0) -> Hit:
    return Hit(
        document_id=doc,
        ordinal=ordinal,
        tier=tier,
        title=f"Doc {doc}",
        category="notes",
        text=text,
        score=1.0,
        collection="kb_private",
    )


def route_json(route: str, query: str, records: dict[str, Any] | None = None) -> str:
    return json.dumps({"route": route, "query": query, "records": records})


class StreamModel(BaseChatModel):
    """Streams scripted chunks: one play per call; a string is a chunk, an exception is raised,
    a float sleeps that many seconds."""

    plays: list[list[Any]]
    calls: int = 0
    seen: list[list[BaseMessage]] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "scripted-stream"

    def _generate(
        self, messages: list[BaseMessage], stop: Any = None, run_manager: Any = None, **kwargs: Any
    ) -> ChatResult:
        raise NotImplementedError

    async def _astream(
        self, messages: list[BaseMessage], stop: Any = None, run_manager: Any = None, **kwargs: Any
    ) -> AsyncIterator[ChatGenerationChunk]:
        self.seen.append(list(messages))
        play = self.plays[min(self.calls, len(self.plays) - 1)]
        self.calls += 1
        for step in play:
            if isinstance(step, BaseException):
                raise step
            if isinstance(step, float):
                await asyncio.sleep(step)
                continue
            usage = UsageMetadata(input_tokens=1, output_tokens=1, total_tokens=2)
            yield ChatGenerationChunk(message=AIMessageChunk(content=step, usage_metadata=usage))


@dataclass
class FakeSearcher:
    hits: list[Hit] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def search(
        self, query: str, *, max_tier: Tier, filters: SearchFilters | None, limit: int
    ) -> list[Hit]:
        self.calls.append({"query": query, "max_tier": max_tier, "filters": filters})
        return [h for h in self.hits if h.tier <= max_tier]


@dataclass
class MemoryRecordsTool:
    rows: list[RecordIn] = field(default_factory=list)
    calls: list[RecordsQuery] = field(default_factory=list)

    async def query(self, query: RecordsQuery) -> RecordsResult:
        self.calls.append(query)
        rows = [r for r in self.rows if self._match(r, query)]
        groups: dict[str, int] = {}
        if query.group_by:
            for r in rows:
                key = (
                    r.date.strftime("%Y-%m")
                    if query.group_by == "month"
                    else getattr(r, query.group_by)
                )
                groups[key] = groups.get(key, 0) + 1
        rows.sort(key=lambda r: r.date, reverse=True)
        return RecordsResult(
            query=query,
            total=len(rows),
            groups=[RecordGroup(key=k, count=v) for k, v in groups.items()],
            items=[
                RecordItem(company=r.company, role=r.role, stage=r.stage, date=r.date.date())
                for r in rows[:20]
            ],
        )

    @staticmethod
    def _match(row: RecordIn, q: RecordsQuery) -> bool:
        day = row.date.date()
        return (
            row.kind == q.kind
            and (q.company is None or row.company.lower() == q.company.lower())
            and (q.stage is None or row.stage.lower() == q.stage.lower())
            and (q.date_from is None or day >= q.date_from)
            and (q.date_to is None or day <= q.date_to)
        )


def persona_records() -> list[RecordIn]:
    def rec(kind: str, company: str, day: str, stage: str = "screen") -> RecordIn:
        return RecordIn.model_validate(
            {
                "kind": kind,
                "company": company,
                "role": "Backend Engineer",
                "stage": stage,
                "date": f"{day}T10:00:00",
            }
        )

    return [
        rec("interview", "Acme Widgets", "2026-09-04"),
        rec("interview", "Globex Labs", "2026-09-18", "onsite"),
        rec("interview", "Initech Cloud", "2026-09-29"),
        rec("interview", "Acme Widgets", "2026-08-21"),
        rec("application", "Globex Labs", "2026-09-02", "applied"),
        rec("application", "Umbrella Data", "2026-09-10", "applied"),
    ]


@dataclass
class Agent:
    graph: OwnerGraph
    factory: Factory
    orchestrator: Orchestrator
    searcher: FakeSearcher
    records: MemoryRecordsTool
    profile: MemoryProfileStore
    conversations: ConversationService
    store: MemoryConversationStore
    redis: FakeAsyncRedis

    async def run(
        self,
        question: str,
        *,
        conversation_id: str | None = None,
        max_tier: Tier = Tier.CONFIDENTIAL,
        **extra: Any,
    ) -> tuple[OwnerState, list[str]]:
        if conversation_id is None:
            conversation_id = (await self.conversations.start("owner")).id
        history = await self.conversations.history(conversation_id)
        state = initial_state(
            conversation_id=conversation_id,
            question=question,
            history=history,
            max_tier=max_tier,
            **extra,
        )
        tokens: list[str] = []
        final: Any = None
        async for item in self.graph.astream(state, stream_mode=["custom", "values"]):
            mode, chunk = cast(tuple[str, Any], item)
            if mode == "custom":
                tokens.append(chunk["token"])
            else:
                final = chunk
        await self.conversations.drain()
        return final, tokens

    async def history(self, conversation_id: str) -> list[Turn]:
        return await self.conversations.history(conversation_id)


def make_agent(
    *,
    router: list[Any] | None = None,
    cloud: list[Any] | None = None,
    local: list[Any] | None = None,
    hits: list[Hit] | None = None,
    generator: AnswerGenerator | None = None,
    settings: OwnerSettings | None = None,
    streams: dict[str, list[list[Any]]] | None = None,
    idle_timeout: float = 5.0,
) -> Agent:
    """`streams` swaps generator models for streaming ones and uses `StreamingGenerator`."""
    specs = [spec("router", roles=["router"], max_tier="confidential")]
    scripts: dict[str, list[Any]] = {"router": router or [ValueError("no router script")]}
    if cloud is not None:
        specs.append(spec("cloud", roles=["generator"], max_tier="private"))
        scripts["cloud"] = cloud
    if local is not None:
        specs.append(spec("local", roles=["generator"], max_tier="confidential", tags=["local"]))
        scripts["local"] = local
    factory = Factory(scripts)
    for model_id, plays in (streams or {}).items():
        factory.models[model_id] = StreamModel(plays=plays)  # type: ignore[assignment]
    orch = orchestrator(inventory(*specs), factory, max_attempts=1)
    if streams is not None:
        generator = StreamingGenerator(orch, idle_timeout_seconds=idle_timeout)
    redis = FakeAsyncRedis(decode_responses=True)
    store = MemoryConversationStore()
    conversations = ConversationService(redis, store, visitor_salt="salt", retry_base_delay=0)
    searcher = FakeSearcher(hits=list(hits or []))
    records = MemoryRecordsTool(rows=persona_records())
    profile = MemoryProfileStore()
    deps = OwnerDeps(
        orchestrator=orch,
        retriever=searcher,
        reranker=FusedOrderReranker(),
        records=records,
        profile=ProfileTool(profile),
        conversations=conversations,
        generator=generator or ChatGenerator(orch),
        settings=settings or OwnerSettings(),
        today=lambda: TODAY,
    )
    return Agent(
        build_owner_graph(deps),
        factory,
        orch,
        searcher,
        records,
        profile,
        conversations,
        store,
        redis,
    )


def days_ago(n: int) -> datetime:
    return datetime(2026, 10, 17) - timedelta(days=n)
