"""The intake assistant wired to a scripted local model and in-memory stores.

Each `say` drives one owner message through `IntakeService` and collects its events. The local
model plays one scripted JSON reply per extraction (and per confirmation the rules can't
read). `restart` rebuilds the graph and service on the same checkpointer with an empty Redis:
an API restart plus a cache flush.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fakeredis import FakeAsyncRedis
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver

from app.agents.generation import ChatGenerator
from app.agents.graphs.intake import IntakeDeps, build_intake_graph
from app.agents.graphs.owner import OwnerDeps, build_owner_graph
from app.agents.tools.profile import ProfileTool
from app.core.config import Settings
from app.db.documents import ConversationMode
from app.kb.retrieval.rerank import FusedOrderReranker
from app.services.admin_ops import AdminDeps, AdminOps
from app.services.conversations import ConversationService, Turn
from app.services.intake import AssistantRequest, IntakeService
from app.services.owner_chat import ConversationMeta
from app.services.profile import RecordIn
from llm_orchestrator import Orchestrator
from tests.agent.harness import FakeSearcher, MemoryRecordsTool, persona_records
from tests.fakes import MemoryAuditLog, MemoryConversationStore
from tests.ingestion.conftest import Harness
from tests.ingestion.fakes import MemoryProfileStore, MemoryRecordStore
from tests.llm_orchestrator.fakes import Factory, inventory, orchestrator, spec

# Sun 18 Oct 2026, 01:30 in Asia/Kolkata (still the 17th in UTC).
NOW = datetime(2026, 10, 17, 20, 0, tzinfo=UTC)
TZ = ZoneInfo("Asia/Kolkata")
KEY = "owner-cli"


def extract(action: str | None, reply: str = "", **payload: Any) -> str:
    return json.dumps({"action": action, "payload": payload, "reply": reply})


def route(question: str, records: dict[str, Any] | None) -> str:
    return json.dumps({"route": "records", "query": question, "records": records})


class MemoryIntakeIndex:
    def __init__(self, store: MemoryConversationStore) -> None:
        self.store = store
        self.meta: dict[str, dict[str, Any]] = {}

    def _row(self, conversation_id: str) -> dict[str, Any]:
        return self.meta.setdefault(
            conversation_id, {"name": None, "description": None, "mode": "ask"}
        )

    async def get(self, conversation_id: str) -> ConversationMeta | None:
        if conversation_id not in self.meta:
            return None
        row = self.meta[conversation_id]
        turns = self.store.turns.get(conversation_id, {})
        return ConversationMeta(
            id=conversation_id,
            name=row["name"],
            description=row["description"],
            channel="owner",
            created_at=NOW,
            updated_at=NOW,
            message_count=max(turns, default=0),
            mode=row["mode"],
        )

    async def names_like(self, base: str) -> set[str]:
        return {r["name"] for r in self.meta.values() if r["name"] and r["name"].startswith(base)}

    async def set_title(
        self, conversation_id: str, name: str | None, description: str | None
    ) -> None:
        row = self._row(conversation_id)
        row["name"] = name or row["name"]
        row["description"] = description or row["description"]

    async def set_mode(self, conversation_id: str, mode: ConversationMode) -> None:
        self._row(conversation_id)["mode"] = mode


@dataclass
class Reply:
    conversation_id: str
    events: list[tuple[str, dict[str, Any]]]

    def of(self, name: str) -> list[dict[str, Any]]:
        return [data for event, data in self.events if event == name]

    @property
    def text(self) -> str:
        return "".join(d["text"] for d in self.of("token"))

    @property
    def draft(self) -> dict[str, Any] | None:
        drafts = self.of("draft")
        return drafts[-1] if drafts else None

    @property
    def result(self) -> dict[str, Any] | None:
        results = self.of("result")
        return results[-1] if results else None

    @property
    def done(self) -> dict[str, Any]:
        return self.of("done")[-1]


@dataclass
class Twin:
    h: Harness
    tmp: Path
    local: list[Any]
    cloud_only: bool = False
    saver: BaseCheckpointSaver[Any] = field(default_factory=InMemorySaver)
    audit: MemoryAuditLog = field(default_factory=MemoryAuditLog)
    records: MemoryRecordStore = field(default_factory=MemoryRecordStore)
    profile: MemoryProfileStore = field(default_factory=MemoryProfileStore)
    store: MemoryConversationStore = field(default_factory=MemoryConversationStore)
    router: list[Any] = field(default_factory=lambda: [ValueError("no owner router script")])
    owner_rows: list[RecordIn] = field(default_factory=persona_records)

    def __post_init__(self) -> None:
        self.index = MemoryIntakeIndex(self.store)
        self.inbox = self.tmp / "inbox"
        self.inbox.mkdir(exist_ok=True)
        self.factory = Factory({"local": self.local, "cloud": ["cloud must never be called"]})
        specs = [spec("cloud", roles=["router", "generator"], max_tier="private")]
        if not self.cloud_only:
            specs.append(
                spec(
                    "local", roles=["router", "generator"], max_tier="confidential", tags=["local"]
                )
            )
        self.orchestrator: Orchestrator = orchestrator(
            inventory(*specs), self.factory, max_attempts=1
        )
        # The cloud model leads every chain: picking it would mean the tier floor failed.
        self.owner_factory = Factory(
            {"owner-cloud": ["cloud must never be called"], "owner-router": self.router}
        )
        self.owner_orchestrator = orchestrator(
            inventory(
                spec("owner-cloud", roles=["router", "generator"], max_tier="private"),
                spec("owner-router", roles=["router", "generator"], max_tier="confidential"),
            ),
            self.owner_factory,
            max_attempts=1,
        )
        self.owner_records = MemoryRecordsTool(rows=list(self.owner_rows))
        self.settings = Settings(_env_file=None, app_env="test", log_format="json")
        self.ops = AdminOps(
            AdminDeps(
                settings=self.settings,
                kb=self.h.store,
                vectors=self.h.index,
                blobs=self.h.blobs,
                audit=self.audit,
                profile=self.profile,
                records=self.records,
                redis=FakeAsyncRedis(decode_responses=True),
                ingestion=self.h.service,
                pipeline=self.h.pipeline,
            )
        )
        self.restart()

    def restart(self) -> None:
        """A new process: fresh Redis and graph objects; the checkpointer and MongoDB stay."""
        self.redis = FakeAsyncRedis(decode_responses=True)
        self.conversations = ConversationService(
            self.redis, self.store, visitor_salt="salt", retry_base_delay=0
        )
        owner = OwnerDeps(
            orchestrator=self.owner_orchestrator,
            retriever=FakeSearcher(),
            reranker=FusedOrderReranker(),
            records=self.owner_records,
            profile=ProfileTool(self.profile),
            conversations=self.conversations,
            generator=ChatGenerator(self.owner_orchestrator),
            today=lambda: NOW.astimezone(TZ).date(),
        )
        deps = IntakeDeps(
            orchestrator=self.orchestrator,
            ops=self.ops,
            inbox=self.inbox,
            timezone=TZ,
            wait_seconds=5.0,
            owner_graph=build_owner_graph(owner, checkpointer=False),
            conversations=self.conversations,
            now=lambda: NOW,
        )
        self.graph = build_intake_graph(deps, self.saver)
        self.service = IntakeService(self.graph, self.conversations, self.index, self.orchestrator)

    async def say(self, message: str, conversation_id: str | None = None) -> Reply:
        request = AssistantRequest(conversation_id=conversation_id, message=message)
        prepared = await self.service.prepare(request, KEY)
        events = [event async for event in self.service.events(prepared)]
        await self.conversations.drain()
        return Reply(prepared.conversation_id, events)

    @property
    def model_calls(self) -> int:
        return self.factory.models["local"].calls

    async def history(self, conversation_id: str) -> list[Turn]:
        return await self.conversations.history(conversation_id)
