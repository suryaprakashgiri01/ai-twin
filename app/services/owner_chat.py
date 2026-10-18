"""Owner chat: load history, run the owner graph, and turn its stream into chat events.

Events, in order: `meta` (after routing), `token`*, `sources`, then `done`; a failure ends with
`error` instead of `done`. The graph's finalize step has already stored both turns by then, a
partial answer marked `incomplete`.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol, cast

import structlog
from pydantic import BaseModel, ConfigDict, Field

from app.agents.graphs.owner import (
    ErrorInfo,
    OwnerDeps,
    OwnerGraph,
    OwnerSettings,
    OwnerState,
    build_owner_graph,
    initial_state,
)
from app.agents.prompts import owner as owner_prompts
from app.agents.streaming import StreamingGenerator, sse
from app.agents.tools.profile import ProfileTool
from app.agents.tools.records import MongoRecordsTool, RecordsResult
from app.core.config import Settings
from app.core.llm import LLMServices
from app.core.resources import Resources
from app.db.documents import Channel, Conversation, ConversationMode, Message, utcnow
from app.exceptions import AppError, ErrorCode
from app.kb.embedding import Embedder
from app.kb.retrieval.context import Source
from app.kb.retrieval.rerank import CrossEncoderReranker
from app.kb.retrieval.retriever import MongoDocumentFilter, Retriever, SearchFilters
from app.observability.tracing import TraceRun, tracing
from app.services.conversations import ConversationService, Turn
from app.services.naming import (
    DESCRIPTION_MAX,
    NAME_MAX,
    NAME_PATTERN,
    describe,
    fallback_name,
    slugify_title,
    unique_name,
)
from llm_orchestrator import Prefer, Tier

logger = structlog.get_logger(__name__)

ChatTier = Literal["public", "private", "confidential"]


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conversation_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")
    message: str = Field(min_length=1, max_length=4000)
    max_tier: ChatTier = "confidential"
    filters: SearchFilters | None = None
    # A model id from config/models.yaml; it must be allowed at the answer's tier.
    model: str | None = Field(default=None, max_length=100)
    prefer: Prefer | None = None


class TokenCounts(BaseModel):
    input: int = 0
    output: int = 0


class ChatResponse(BaseModel):
    conversation_id: str
    seq: int | None = None
    route: str
    query: str
    tier: ChatTier | None = None
    model: str | None = None
    answer: str
    sources: list[Source] = Field(default_factory=list)
    citations: list[str] = Field(default_factory=list)
    withheld: int = 0
    incomplete: bool = False
    latency_ms: float | None = None
    tokens: TokenCounts = Field(default_factory=TokenCounts)
    records: RecordsResult | None = None
    error: ErrorInfo | None = None


class ConversationSummary(BaseModel):
    id: str
    name: str | None = None
    description: str | None = None
    created_at: datetime
    updated_at: datetime
    message_count: int
    mode: ConversationMode = "ask"


class TitleUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, pattern=NAME_PATTERN, max_length=NAME_MAX)
    description: str | None = Field(default=None, min_length=1, max_length=DESCRIPTION_MAX)


class ConversationMeta(ConversationSummary):
    channel: Channel
    visitor_hash: str | None = None
    name_locked: bool = False
    titled_turns: int = 0


_INTERNAL_META = {"channel", "visitor_hash", "name_locked", "titled_turns"}


class ConversationDetail(ConversationSummary):
    turns: list[Turn]


class ConversationIndex(Protocol):
    async def get(self, conversation_id: str) -> ConversationMeta | None: ...

    async def list_owner(self, limit: int) -> list[ConversationSummary]: ...

    async def by_name(self, name: str) -> ConversationMeta | None: ...

    async def names_like(self, base: str) -> set[str]: ...

    async def set_title(
        self, conversation_id: str, name: str | None, description: str | None
    ) -> None: ...

    async def lock_name(self, conversation_id: str) -> None: ...

    async def save_refresh(
        self, conversation_id: str, name: str | None, description: str, titled_turns: int
    ) -> None: ...

    async def turns(self, conversation_id: str, limit: int) -> list[Turn]: ...


class MongoConversationIndex:
    async def get(self, conversation_id: str) -> ConversationMeta | None:
        found = await Conversation.get(conversation_id)
        if found is None:
            return None
        return _meta(found)

    async def by_name(self, name: str) -> ConversationMeta | None:
        found = await Conversation.find_one(
            Conversation.channel == "owner", Conversation.name == name
        )
        return _meta(found) if found is not None else None

    async def names_like(self, base: str) -> set[str]:
        pattern = f"^{re.escape(base)}(-[0-9]+)?$"
        cursor = Conversation.get_pymongo_collection().find(
            {"channel": "owner", "name": {"$regex": pattern}}, {"name": 1}
        )
        return {doc["name"] async for doc in cursor}

    async def lock_name(self, conversation_id: str) -> None:
        await Conversation.get_pymongo_collection().update_one(
            {"_id": conversation_id}, {"$set": {"name_locked": True}}
        )

    async def save_refresh(
        self, conversation_id: str, name: str | None, description: str, titled_turns: int
    ) -> None:
        changes: dict[str, Any] = {"description": description, "titled_turns": titled_turns}
        if name is not None:
            changes["name"] = name
        await Conversation.get_pymongo_collection().update_one(
            {"_id": conversation_id}, {"$set": changes}
        )

    async def set_title(
        self, conversation_id: str, name: str | None, description: str | None
    ) -> None:
        changes = {
            key: value
            for key, value in (("name", name), ("description", description))
            if value is not None
        }
        if not changes:
            return
        now = utcnow()
        # Upsert: a brand-new conversation may reach MongoDB only when its first turn persists.
        await Conversation.get_pymongo_collection().update_one(
            {"_id": conversation_id},
            {
                "$setOnInsert": {
                    "channel": "owner",
                    "visitor_hash": None,
                    "created_at": now,
                    "updated_at": now,
                    "message_count": 0,
                },
                "$set": changes,
            },
            upsert=True,
        )

    async def set_mode(self, conversation_id: str, mode: ConversationMode) -> None:
        now = utcnow()
        await Conversation.get_pymongo_collection().update_one(
            {"_id": conversation_id},
            {
                "$setOnInsert": {
                    "channel": "owner",
                    "visitor_hash": None,
                    "created_at": now,
                    "updated_at": now,
                    "message_count": 0,
                },
                "$set": {"mode": mode},
            },
            upsert=True,
        )

    async def list_owner(self, limit: int) -> list[ConversationSummary]:
        found = (
            await Conversation.find(Conversation.channel == "owner")
            .sort("-updated_at")
            .limit(limit)
            .to_list()
        )
        return [
            ConversationSummary(
                id=c.id,
                name=c.name,
                description=c.description,
                created_at=c.created_at,
                updated_at=c.updated_at,
                message_count=c.message_count,
                mode=c.mode,
            )
            for c in found
        ]

    async def turns(self, conversation_id: str, limit: int) -> list[Turn]:
        messages = (
            await Message.find(Message.conversation_id == conversation_id)
            .sort("+seq")
            .limit(limit)
            .to_list()
        )
        fields = set(Turn.model_fields)
        return [Turn.model_validate(m.model_dump(include=fields)) for m in messages]


def _meta(found: Conversation) -> ConversationMeta:
    return ConversationMeta(
        id=found.id,
        name=found.name,
        description=found.description,
        channel=found.channel,
        visitor_hash=found.visitor_hash,
        created_at=found.created_at,
        updated_at=found.updated_at,
        message_count=found.message_count,
        mode=found.mode,
        name_locked=found.name_locked,
        titled_turns=found.titled_turns,
    )


@dataclass(frozen=True)
class Prepared:
    request: ChatRequest
    conversation_id: str
    history: list[Turn]


@dataclass
class _Run:
    """What the stream has produced so far."""

    conversation_id: str
    state: dict[str, Any]


class OwnerChatService:
    def __init__(
        self,
        graph: OwnerGraph,
        conversations: ConversationService,
        index: ConversationIndex,
        known_models: frozenset[str] = frozenset(),
    ) -> None:
        self._graph = graph
        self._conversations = conversations
        self._index = index
        self._known_models = known_models

    async def prepare(self, request: ChatRequest) -> Prepared:
        """Everything that can fail with a plain error response, before any event is sent."""
        if (
            request.model is not None
            and self._known_models
            and request.model not in self._known_models
        ):
            raise AppError(ErrorCode.VALIDATION_ERROR, "Unknown model id", details=[request.model])
        if request.conversation_id is None:
            ref = await self._conversations.start("owner")
            await self._title(ref.id, request.message)
            return Prepared(request, ref.id, [])
        meta = await self._index.get(request.conversation_id)
        history = await self._conversations.history(request.conversation_id)
        # A brand-new conversation may not have reached MongoDB yet; Redis already has it.
        if (meta is not None and meta.channel != "owner") or (meta is None and not history):
            raise AppError(ErrorCode.NOT_FOUND, "Conversation not found")
        return Prepared(request, request.conversation_id, history)

    async def events(self, prepared: Prepared) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        async for event in self._drive(self._start(prepared)):
            yield event

    @staticmethod
    def _start(prepared: Prepared) -> tuple[_Run, OwnerState]:
        request = prepared.request
        state = initial_state(
            conversation_id=prepared.conversation_id,
            question=request.message,
            history=prepared.history,
            max_tier=Tier.parse(request.max_tier),
            filters=request.filters,
            model=request.model,
            prefer=request.prefer,
        )
        return _Run(prepared.conversation_id, dict(state)), state

    async def _drive(
        self, started: tuple[_Run, OwnerState]
    ) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        run, state = started
        with tracing().run(
            "owner-chat",
            "owner",
            input=state["question"],
            session_id=run.conversation_id,
            metadata={"max_tier": state["max_tier"].label},
        ) as trace:
            try:
                async for item in self._graph.astream(state, stream_mode=["updates", "custom"]):
                    mode, chunk = cast(tuple[str, Any], item)
                    for event in self._on_chunk(run, mode, chunk):
                        yield event
            except AppError as exc:
                yield "error", self._error(run, exc.code, exc.message)
            except Exception as exc:
                logger.error("owner chat failed", error_type=type(exc).__name__)
                yield "error", self._error(run, ErrorCode.INTERNAL_ERROR, "Chat failed")
            finally:
                _trace_owner_run(trace, run.state)

    def _on_chunk(self, run: _Run, mode: str, chunk: Any) -> list[tuple[str, dict[str, Any]]]:
        if mode == "custom":
            return [("token", {"text": chunk["token"]})]
        events: list[tuple[str, dict[str, Any]]] = []
        for node, update in chunk.items():
            run.state.update(update or {})
            if node == "route":
                events.append(("meta", self._meta(run)))
            elif node == "generate" and run.state.get("error") is None:
                events.append(("sources", self._sources(run)))
            elif node == "finalize":
                events.append(self._final(run))
        return events

    @staticmethod
    def _meta(run: _Run) -> dict[str, Any]:
        state = run.state
        return {
            "conversation_id": run.conversation_id,
            "route": state["route"],
            "route_source": state["route_source"],
            "query": state["query"],
            "rewritten": state["query"].strip() != state["question"].strip(),
            "max_tier": state["max_tier"].label,
        }

    @staticmethod
    def _sources(run: _Run) -> dict[str, Any]:
        sources: list[Source] = run.state.get("sources", [])
        return {
            "sources": [s.model_dump(mode="json") for s in sources],
            "citations": [s.document_id for s in sources],
        }

    def _final(self, run: _Run) -> tuple[str, dict[str, Any]]:
        error: ErrorInfo | None = run.state.get("error")
        if error is not None:
            return "error", self._error(run, error.code, error.message)
        return "done", self._response(run).model_dump(mode="json", exclude={"answer", "sources"})

    @staticmethod
    def _error(run: _Run, code: ErrorCode, message: str) -> dict[str, Any]:
        return {
            "code": code.value,
            "message": message,
            "conversation_id": run.conversation_id,
            "seq": run.state.get("seq"),
            "incomplete": True,
        }

    @staticmethod
    def _response(run: _Run) -> ChatResponse:
        state = run.state
        tier: Tier | None = state.get("tier")
        sources: list[Source] = state.get("sources", [])
        return ChatResponse(
            conversation_id=run.conversation_id,
            seq=state.get("seq"),
            route=state.get("route", "chat"),
            query=state.get("query", state["question"]),
            tier=cast(ChatTier, tier.label) if tier is not None else None,
            model=state.get("model_id"),
            answer=state.get("answer", ""),
            sources=sources,
            citations=[s.document_id for s in sources],
            withheld=state.get("withheld", 0),
            incomplete=state.get("incomplete", False),
            latency_ms=state.get("latency_ms"),
            tokens=TokenCounts(
                input=state.get("input_tokens", 0), output=state.get("output_tokens", 0)
            ),
            records=state.get("records"),
            error=state.get("error"),
        )

    async def sse(self, prepared: Prepared) -> AsyncIterator[str]:
        async for event, data in self.events(prepared):
            yield sse(event, data)

    async def answer(self, prepared: Prepared) -> ChatResponse:
        """The non-streaming form: the `done` fields plus the answer and its sources."""
        started = self._start(prepared)
        run = started[0]
        last: dict[str, Any] = {}
        async for event, data in self._drive(started):
            last = data if event == "error" else last
        if last and run.state.get("seq") is None:
            # Nothing was stored: the run failed outside the graph's own error handling.
            raise AppError(ErrorCode(last["code"]), last["message"])
        return self._response(run)

    async def _title(self, conversation_id: str, question: str) -> None:
        """Name a new conversation from its first question. Naming never blocks the chat."""
        try:
            base = slugify_title(question, fallback_name(conversation_id))
            name = unique_name(base, await self._index.names_like(base))
            await self._index.set_title(conversation_id, name, describe(question) or None)
        except Exception as exc:
            logger.warning("conversation naming failed", error_type=type(exc).__name__)

    async def list_conversations(self, limit: int) -> list[ConversationSummary]:
        return await self._index.list_owner(limit)

    async def conversation_by_name(self, name: str) -> ConversationSummary:
        meta = await self._index.by_name(name)
        if meta is None:
            raise AppError(ErrorCode.NOT_FOUND, "Conversation not found")
        return ConversationSummary(**meta.model_dump(exclude={"channel", "visitor_hash"}))

    async def update_title(self, conversation_id: str, update: TitleUpdate) -> ConversationSummary:
        meta = await self._index.get(conversation_id)
        if meta is None or meta.channel != "owner":
            raise AppError(ErrorCode.NOT_FOUND, "Conversation not found")
        if update.name is not None and update.name != meta.name:
            holder = await self._index.by_name(update.name)
            if holder is not None and holder.id != conversation_id:
                raise AppError(ErrorCode.CONFLICT, "That name is already used")
        await self._index.set_title(conversation_id, update.name, update.description)
        if update.name is not None:
            # The owner chose this name: the automatic refresh must never overwrite it.
            await self._index.lock_name(conversation_id)
        changed = meta.model_dump(exclude=_INTERNAL_META)
        changed.update(update.model_dump(exclude_none=True))
        return ConversationSummary(**changed)

    async def conversation(self, conversation_id: str, limit: int) -> ConversationDetail:
        meta = await self._index.get(conversation_id)
        if meta is None or meta.channel != "owner":
            raise AppError(ErrorCode.NOT_FOUND, "Conversation not found")
        turns = await self._index.turns(conversation_id, limit)
        return ConversationDetail(
            **meta.model_dump(exclude={"channel", "visitor_hash"}), turns=turns
        )


def _trace_owner_run(trace: TraceRun, state: dict[str, Any]) -> None:
    tier: Tier | None = state.get("tier")
    trace.tag(f"route:{state.get('route', 'chat')}")
    if state.get("incomplete") or state.get("error") is not None:
        trace.tag("incomplete")
    trace.meta(
        route=state.get("route"),
        route_source=state.get("route_source"),
        tier=tier.label if tier is not None else None,
        model=state.get("model_id"),
        seq=state.get("seq"),
        incomplete=bool(state.get("incomplete")),
        prompt_version=owner_prompts.PROMPT_VERSION,
    )
    trace.output(state.get("answer", ""))


def owner_deps(
    settings: Settings, resources: Resources, llm: LLMServices, embedder: Embedder
) -> OwnerDeps:
    retriever = Retriever(
        resources.qdrant,
        embedder,
        MongoDocumentFilter(),
        prefetch_limit=settings.retrieval_prefetch,
    )
    return OwnerDeps(
        orchestrator=llm.orchestrator,
        retriever=retriever,
        reranker=CrossEncoderReranker(
            settings.models_dir / "fastembed", min_score=settings.rerank_min_score
        ),
        records=MongoRecordsTool(),
        profile=ProfileTool(resources.profile),
        conversations=resources.conversations,
        generator=StreamingGenerator(
            llm.orchestrator, idle_timeout_seconds=settings.llm_timeout_seconds
        ),
        settings=OwnerSettings(
            top_k=settings.retrieval_top_k,
            context_tokens=settings.owner_context_tokens,
            history_turns=settings.owner_history_turns,
        ),
    )


def build_owner_chat(
    settings: Settings, resources: Resources, llm: LLMServices, embedder: Embedder
) -> OwnerChatService:
    return build_owner_chat_from(owner_deps(settings, resources, llm, embedder), resources, llm)


def build_owner_chat_from(
    deps: OwnerDeps, resources: Resources, llm: LLMServices
) -> OwnerChatService:
    return OwnerChatService(
        build_owner_graph(deps),
        resources.conversations,
        MongoConversationIndex(),
        known_models=frozenset(llm.orchestrator.inventory.models),
    )
