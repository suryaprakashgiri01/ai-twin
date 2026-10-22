"""Prep: interview and application answers in the owner's voice, over the owner's vault.

Conversations are owner conversations with `mode: prep`, so they list, continue, summarise and
re-title like the others. Events: `meta` (type, sub-queries), `token` (the filtered answer, in
one piece), `sources`, then `done` (tier, model, removed details, checks) or `error`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, cast

import structlog
from pydantic import ConfigDict, Field

from app.agents.context import ContextBuilder, OwnerContext
from app.agents.graphs.owner import OwnerDeps
from app.agents.graphs.prep import (
    PrepDeps,
    PrepGraph,
    PrepSettings,
    PrepState,
    Revise,
    build_prep_graph,
    initial_state,
)
from app.agents.prep.filter import OutwardFilter
from app.agents.prompts import prep as prompts
from app.agents.prompts.prep import PrepType
from app.agents.streaming import StreamingGenerator, sse
from app.api.envelope import ApiModel
from app.core.config import Settings
from app.core.llm import LLMServices
from app.core.resources import Resources
from app.db.documents import ConversationMode
from app.exceptions import AppError, ErrorCode
from app.kb.ingestion.guard import EmployerGuard
from app.kb.retrieval.context import Source
from app.observability.tracing import tracing
from app.services.conversations import ConversationService, Turn
from app.services.intake import IntakeIndex
from app.services.naming import describe, fallback_name, slugify_title, unique_name
from app.services.owner_chat import TokenCounts, TurnHook, custom_event
from llm_orchestrator import Prefer, Tier

logger = structlog.get_logger(__name__)

MODE: ConversationMode = "prep"


class PrepRequest(ApiModel):
    model_config = ConfigDict(extra="forbid")

    conversation_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")
    message: str = Field(min_length=1, max_length=4000)
    # The question type; detected from the wording when left out.
    kind: PrepType | None = None
    words: int | None = Field(default=None, ge=10, le=2000)
    chars: int | None = Field(default=None, ge=50, le=10000)
    # Rewrite the previous answer instead of answering `message`.
    revise: Revise | None = None
    model: str | None = Field(default=None, max_length=100)
    prefer: Prefer | None = None


class PrepResponse(ApiModel):
    conversation_id: str
    seq: int | None = None
    kind: PrepType
    tier: str | None = None
    model: str | None = None
    answer: str
    sources: list[Source] = Field(default_factory=list)
    # "2 ticket keys → “a tracked ticket”": what the outward filter changed.
    removed: list[str] = Field(default_factory=list)
    # What to check before sending: placeholders, unsourced numbers, length.
    checks: list[str] = Field(default_factory=list)
    withheld: int = 0
    latency_ms: float | None = None
    tokens: TokenCounts = Field(default_factory=TokenCounts)


@dataclass(frozen=True)
class Prepared:
    request: PrepRequest
    conversation_id: str
    history: list[Turn]
    context: OwnerContext | None = None


class PrepService:
    def __init__(
        self,
        graph: PrepGraph,
        conversations: ConversationService,
        index: IntakeIndex,
        *,
        known_models: frozenset[str] = frozenset(),
        context: ContextBuilder | None = None,
        on_turn: TurnHook | None = None,
    ) -> None:
        self._graph = graph
        self._conversations = conversations
        self._index = index
        self._known_models = known_models
        self._context = context
        self._on_turn = on_turn

    async def prepare(self, request: PrepRequest) -> Prepared:
        if request.model and self._known_models and request.model not in self._known_models:
            raise AppError(ErrorCode.INVALID_INPUT, "Unknown model id", details=[request.model])
        if request.conversation_id is None:
            if request.revise:
                raise AppError(ErrorCode.INVALID_INPUT, "Nothing to revise in a new conversation")
            ref = await self._conversations.start("owner")
            await self._index.set_mode(ref.id, MODE)
            await self._title(ref.id, request.message)
            return Prepared(request, ref.id, [], await self._load_context(None, []))
        meta = await self._index.get(request.conversation_id)
        history = await self._conversations.history(request.conversation_id)
        if (meta is not None and meta.channel != "owner") or (meta is None and not history):
            raise AppError(ErrorCode.NOT_FOUND, "Conversation not found")
        if meta is not None and meta.mode != MODE:
            raise AppError(
                ErrorCode.CONFLICT, f"That is an {meta.mode} conversation; continue it there"
            )
        context = await self._load_context(request.conversation_id, history)
        return Prepared(request, request.conversation_id, history, context)

    async def _load_context(
        self, conversation_id: str | None, history: list[Turn]
    ) -> OwnerContext | None:
        if self._context is None:
            return None
        return await self._context.load(conversation_id, history[-1].seq if history else 0)

    async def _title(self, conversation_id: str, message: str) -> None:
        try:
            base = slugify_title(message, fallback_name(conversation_id))
            name = unique_name(base, await self._index.names_like(base))
            await self._index.set_title(conversation_id, name, describe(message) or None)
        except Exception as exc:
            logger.warning("conversation naming failed", error_type=type(exc).__name__)

    def _state(self, prepared: Prepared) -> PrepState:
        r = prepared.request
        return initial_state(
            conversation_id=prepared.conversation_id,
            question=r.message,
            history=prepared.history,
            max_tier=Tier.CONFIDENTIAL,
            kind_hint=r.kind,
            words=r.words,
            chars=r.chars,
            revise=r.revise,
            model=r.model,
            prefer=r.prefer,
            context=prepared.context,
        )

    async def events(self, prepared: Prepared) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        state = dict(self._state(prepared))
        cid = prepared.conversation_id
        with tracing().run("prep", "owner", input=state["question"], session_id=cid) as trace:
            try:
                async for item in self._graph.astream(
                    cast(PrepState, state), stream_mode=["updates", "custom"]
                ):
                    mode, chunk = cast(tuple[str, Any], item)
                    for event in self._on_chunk(cid, state, mode, chunk):
                        yield event
            except AppError as exc:
                yield "error", _error(cid, exc.code, exc.message)
            except Exception as exc:
                logger.error("prep failed", error_type=type(exc).__name__)
                yield "error", _error(cid, ErrorCode.INTERNAL_ERROR, "Prep failed")
            finally:
                trace.output(str(state.get("answer", "")))
                trace.meta(prompt_version=prompts.PROMPT_VERSION, kind=state.get("kind"))

    def _on_chunk(
        self, cid: str, state: dict[str, Any], mode: str, chunk: Any
    ) -> list[tuple[str, dict[str, Any]]]:
        if mode == "custom":
            return [custom_event(chunk)]
        events: list[tuple[str, dict[str, Any]]] = []
        for node, update in chunk.items():
            state.update(update or {})
            if node == "finalize" and self._on_turn is not None:
                self._on_turn(cid, state.get("seq"))
            event = _node_event(cid, node, state)
            if event is not None:
                events.append(event)
        return events

    async def sse(self, prepared: Prepared) -> AsyncIterator[str]:
        async for event, data in self.events(prepared):
            yield sse(event, data)

    async def answer(self, prepared: Prepared) -> PrepResponse:
        """The non-streaming form: the `done` fields plus the answer and its sources."""
        done: dict[str, Any] = {}
        last: dict[str, Any] = {}
        parts: list[str] = []
        sources: list[Any] = []
        async for event, data in self.events(prepared):
            if event == "token":
                parts.append(str(data.get("text", "")))
            elif event == "sources":
                sources = list(data.get("sources", []))
            elif event == "error":
                last = data
            elif event == "done":
                done = data
        if last and not done:
            raise AppError(ErrorCode(last["code"]), last["message"])
        return PrepResponse.model_validate({**done, "answer": "".join(parts), "sources": sources})


def response(cid: str, state: dict[str, Any]) -> PrepResponse:
    tier: Tier | None = state.get("tier")
    return PrepResponse(
        conversation_id=cid,
        seq=state.get("seq"),
        kind=state["kind"],
        tier=tier.label if tier is not None else None,
        model=state.get("model_id"),
        answer=state.get("answer", ""),
        sources=state.get("sources", []),
        removed=state.get("removed", []),
        checks=state.get("checks", []),
        withheld=state.get("withheld", 0),
        latency_ms=state.get("latency_ms"),
        tokens=TokenCounts(
            input=state.get("input_tokens", 0), output=state.get("output_tokens", 0)
        ),
    )


def _node_event(cid: str, node: str, state: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    if node == "plan":
        return "meta", {"conversation_id": cid, "kind": state["kind"], "queries": state["queries"]}
    error = state.get("error")
    if node == "compose" and error is None:
        sources: list[Source] = state.get("sources", [])
        return "sources", {"sources": [s.model_dump(mode="json") for s in sources]}
    if node == "finalize":
        if error is not None:
            return "error", _error(cid, error.code, error.message)
        return "done", response(cid, state).model_dump(mode="json", exclude={"answer", "sources"})
    return None


def _error(cid: str, code: ErrorCode, message: str) -> dict[str, Any]:
    return {"code": code.value, "message": message, "conversation_id": cid, "incomplete": True}


def prep_deps(
    settings: Settings, resources: Resources, llm: LLMServices, owner: OwnerDeps
) -> PrepDeps:
    """Shares the owner graph's retriever and reranker, so their models load once."""
    return PrepDeps(
        orchestrator=llm.orchestrator,
        retriever=owner.retriever,
        reranker=owner.reranker,
        conversations=resources.conversations,
        generator=StreamingGenerator(
            llm.orchestrator, idle_timeout_seconds=settings.llm_local_timeout_seconds
        ),
        filter=OutwardFilter(denylist=EmployerGuard(settings.denylist_path).rules),
        settings=PrepSettings(),
        models=llm.agents.live("prep"),
        profiler=llm.profiler,
    )


def build_prep(
    settings: Settings,
    resources: Resources,
    llm: LLMServices,
    owner: OwnerDeps,
    *,
    index: IntakeIndex,
    context: ContextBuilder | None = None,
    on_turn: TurnHook | None = None,
) -> PrepService:
    return PrepService(
        build_prep_graph(prep_deps(settings, resources, llm, owner)),
        resources.conversations,
        index,
        known_models=frozenset(llm.orchestrator.inventory.models),
        context=context,
        on_turn=on_turn,
    )
