"""The owner intake assistant: conversations, the intake graph with its checkpointer, and SSE.

Events, in order: `meta`, then `token` (the reply text, or the owner graph's answer), `result`
after a write, `draft` when a summary waits for confirmation, then `done`. A failure ends with
`error` instead of `done`. Both turns are stored in the conversation like owner chat turns (an
`answer` hand-off stores its own through the owner graph).

The pending draft lives in the graph's checkpoint, keyed by conversation id, not in Redis: a
confirmation survives an API restart and a cache flush.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Literal, Protocol
from zoneinfo import ZoneInfo

import structlog
from langchain_core.runnables import RunnableConfig
from langgraph.types import Command
from pydantic import BaseModel, ConfigDict, Field

from app.agents.checkpoint import MongoCheckpointSaver
from app.agents.context import ContextBuilder, OwnerContext
from app.agents.graphs.intake import (
    INTAKE_TIER,
    IntakeDeps,
    IntakeGraph,
    build_intake_graph,
    reset_values,
)
from app.agents.graphs.owner import OwnerDeps, build_owner_graph
from app.agents.prompts import intake as prompts
from app.agents.streaming import sse
from app.core.config import Settings
from app.core.llm import LLMServices
from app.core.resources import Resources
from app.db.documents import ConversationMode, utcnow
from app.exceptions import AppError, ErrorCode
from app.kb.ingestion.runtime import Ingestion
from app.observability.tracing import TraceRun, tracing
from app.services.admin_ops import AdminDeps, AdminOps
from app.services.conversations import ConversationRef, ConversationService, Turn
from app.services.memory import MemoryService
from app.services.naming import describe, fallback_name, slugify_title, unique_name
from app.services.owner_chat import ConversationMeta, MongoConversationIndex, TurnHook
from llm_orchestrator import Orchestrator, OrchestratorError, Role

logger = structlog.get_logger(__name__)

HISTORY_TURNS = 6
TURN_ROUTE = "intake"


class AssistantRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conversation_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")
    message: str = Field(min_length=1, max_length=4000)


class PendingDraft(BaseModel):
    """A summary waiting for the owner's yes: a draft, a suggestion, or a delete's retype."""

    kind: Literal["draft", "suggestion", "retype"]
    action: str
    summary: str
    payload: dict[str, Any]
    prompt: str
    # True when the owner's last reply couldn't be read and the same draft is asked again.
    reask: bool = False


class AssistantState(BaseModel):
    conversation_id: str
    mode: ConversationMode
    pending: PendingDraft | None = None


class IntakeIndex(Protocol):
    async def get(self, conversation_id: str) -> ConversationMeta | None: ...

    async def names_like(self, base: str) -> set[str]: ...

    async def set_title(
        self, conversation_id: str, name: str | None, description: str | None
    ) -> None: ...

    async def set_mode(self, conversation_id: str, mode: ConversationMode) -> None: ...


@dataclass(frozen=True)
class Prepared:
    request: AssistantRequest
    conversation_id: str
    key_name: str
    history: list[Turn]
    context: OwnerContext | None = None


# A turn's status as the intake outcome score: a write, a discarded draft, or a question back.
INTAKE_OUTCOMES = {"done": "written", "cancelled": "discarded", "asked": "asked", "draft": "asked"}


def _trace_intake(trace: TraceRun, state: dict[str, Any], pending: bool, resumed: bool) -> None:
    status = str(state.get("status") or "chat")
    trace.tag(f"status:{status}")
    trace.meta(
        status=status,
        action=state.get("action"),
        pending=pending,
        resumed=resumed,
        prompt_version=prompts.PROMPT_VERSION,
    )
    trace.output(state.get("reply") or "")
    outcome = INTAKE_OUTCOMES.get("draft" if pending and status != "done" else status)
    if outcome is not None:
        trace.score("intake_outcome", outcome, data_type="CATEGORICAL")


def history_line(turn: Turn) -> dict[str, str]:
    """A turn for the extraction prompt; an intake reply carries its real outcome, so the model
    can tell what already happened from what only was drafted or failed."""
    route = turn.route or ""
    if turn.role == "assistant" and route.startswith(f"{TURN_ROUTE}:"):
        return {"role": "assistant", "content": f"[{route.split(':', 1)[1]}] {turn.content}"}
    return {"role": turn.role, "content": turn.content}


def _config(conversation_id: str) -> RunnableConfig:
    return {"configurable": {"thread_id": conversation_id}}


class IntakeService:
    def __init__(
        self,
        graph: IntakeGraph,
        conversations: ConversationService,
        index: IntakeIndex,
        orchestrator: Orchestrator,
        on_turn: TurnHook | None = None,
        context: ContextBuilder | None = None,
    ) -> None:
        self._context = context
        self._graph = graph
        self._conversations = conversations
        self._index = index
        self._orchestrator = orchestrator
        self._on_turn = on_turn

    async def ensure_local(self) -> None:
        """Refuse before any model call when no local model may read confidential text."""
        try:
            await self._orchestrator.select(Role.ROUTER, INTAKE_TIER, privileged=True)
        except OrchestratorError as exc:
            logger.warning("intake refused: no local model", error_type=type(exc).__name__)
            raise AppError(ErrorCode.MODEL_UNAVAILABLE, prompts.LOCAL_ONLY) from exc

    async def prepare(self, request: AssistantRequest, key_name: str) -> Prepared:
        """Everything that can fail with a plain error response, before any event is sent."""
        await self.ensure_local()
        if request.conversation_id is None:
            ref = await self._conversations.start("owner")
            await self._index.set_mode(ref.id, "intake")
            await self._title(ref.id, request.message)
            return Prepared(request, ref.id, key_name, [], await self._load_context(None, []))
        meta = await self._require(request.conversation_id)
        history = await self._conversations.history(request.conversation_id)
        if meta is None and not history:
            raise AppError(ErrorCode.NOT_FOUND, "Conversation not found")
        context = await self._load_context(request.conversation_id, history)
        return Prepared(request, request.conversation_id, key_name, history, context)

    async def _load_context(
        self, conversation_id: str | None, history: list[Turn]
    ) -> OwnerContext | None:
        if self._context is None:
            return None
        return await self._context.load(conversation_id, history[-1].seq if history else 0)

    async def _require(self, conversation_id: str) -> ConversationMeta | None:
        meta = await self._index.get(conversation_id)
        if meta is not None and meta.channel != "owner":
            raise AppError(ErrorCode.NOT_FOUND, "Conversation not found")
        if meta is not None and meta.mode != "intake":
            raise AppError(
                ErrorCode.CONFLICT,
                "That is an ask conversation; continue it with make chat-continue",
            )
        return meta

    async def _title(self, conversation_id: str, message: str) -> None:
        """Name a new conversation from its first message. Naming never blocks the turn."""
        try:
            base = slugify_title(message, fallback_name(conversation_id))
            name = unique_name(base, await self._index.names_like(base))
            await self._index.set_title(conversation_id, name, describe(message) or None)
        except Exception as exc:
            logger.warning("conversation naming failed", error_type=type(exc).__name__)

    # --- a turn -----------------------------------------------------------------------------

    async def events(self, prepared: Prepared) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        started = time.perf_counter()
        cid = prepared.conversation_id
        config = _config(cid)
        try:
            before = await self._graph.aget_state(config)
        except Exception as exc:
            logger.error("intake state unreadable", error_type=type(exc).__name__)
            yield "error", self._error(cid, ErrorCode.SERVICE_UNAVAILABLE, "State unavailable")
            return
        resumed = bool(before.interrupts)
        yield "meta", {"conversation_id": cid, "mode": "intake", "resumed": resumed}
        values = reset_values() | {
            "conversation_id": cid,
            "message": prepared.request.message,
            "key_name": prepared.key_name,
            "history": [history_line(t) for t in prepared.history[-HISTORY_TURNS:]],
            "context": prepared.context.model_dump() if prepared.context else None,
        }
        graph_input: Any = (
            Command(resume=prepared.request.message, update=values) if resumed else values
        )
        with tracing().run(
            "intake-turn",
            "intake",
            input=prepared.request.message,
            session_id=cid,
            metadata={"mode": "intake", "tier": INTAKE_TIER.label},
        ) as trace:
            try:
                async for item in self._graph.astream(graph_input, config, stream_mode="custom"):
                    yield "token", {"text": item["token"]}
                after = await self._graph.aget_state(config)
            except AppError as exc:
                trace.tag("status:error")
                yield "error", self._error(cid, exc.code, exc.message)
                return
            except Exception as exc:
                trace.tag("status:error")
                logger.error("intake turn failed", error_type=type(exc).__name__)
                yield "error", self._error(cid, ErrorCode.INTERNAL_ERROR, "The assistant failed")
                return
            _trace_intake(trace, after.values, bool(after.interrupts), resumed)
            for event in await self._finish(prepared, after.values, after.interrupts, resumed):
                yield event
        logger.info(
            "intake turn",
            status=after.values.get("status"),
            action=after.values.get("action"),
            resumed=resumed,
            pending=bool(after.interrupts),
            latency_ms=round((time.perf_counter() - started) * 1000, 1),
        )

    async def _finish(
        self, prepared: Prepared, state: dict[str, Any], interrupts: Any, resumed: bool
    ) -> list[tuple[str, dict[str, Any]]]:
        cid = prepared.conversation_id
        status = str(state.get("status") or "chat")
        reply = str(state.get("reply") or "")
        outcome = state.get("outcome")
        events: list[tuple[str, dict[str, Any]]] = []
        if status != "answer" and reply:
            events.append(("token", {"text": reply}))
        if outcome and status == "done":
            events.append(("result", outcome))
        pending = PendingDraft.model_validate(interrupts[0].value) if interrupts else None
        if pending is not None:
            if status != "done":
                status = "draft"
            events.append(("draft", pending.model_dump(mode="json")))
        seq = None
        if status == "answer":
            seq = (outcome or {}).get("seq")
        else:
            text = "\n\n".join(part for part in (reply, pending.prompt if pending else "") if part)
            seq = await self._store(cid, prepared.request.message, text, status)
        if self._on_turn is not None:
            self._on_turn(cid, seq)
        if status == "answer" and (outcome or {}).get("error"):
            error = outcome["error"] if outcome else {}
            events.append(("error", self._error(cid, ErrorCode(error["code"]), error["message"])))
            return events
        events.append(
            (
                "done",
                {
                    "conversation_id": cid,
                    "status": status,
                    "action": (outcome or {}).get("action") or state.get("action"),
                    "pending": pending is not None,
                    "resumed": resumed,
                    "seq": seq,
                    "tier": INTAKE_TIER.label,
                },
            )
        )
        return events

    async def _store(self, conversation_id: str, message: str, reply: str, status: str) -> int:
        ref = ConversationRef(id=conversation_id, channel="owner")
        tier = INTAKE_TIER.label
        await self._conversations.append_turn(
            ref,
            Turn(role="user", content=message, max_tier=tier),
        )
        stored = await self._conversations.append_turn(
            ref,
            Turn(
                role="assistant",
                content=reply,
                route=f"{TURN_ROUTE}:{status}",
                max_tier=tier,
            ),
        )
        return stored.seq

    @staticmethod
    def _error(conversation_id: str, code: ErrorCode, message: str) -> dict[str, Any]:
        return {"code": code.value, "message": message, "conversation_id": conversation_id}

    async def sse(self, prepared: Prepared) -> AsyncIterator[str]:
        async for event, data in self.events(prepared):
            yield sse(event, data)

    # --- reading the pending draft ------------------------------------------------------------

    async def state(self, conversation_id: str) -> AssistantState:
        meta = await self._index.get(conversation_id)
        if meta is None or meta.channel != "owner":
            raise AppError(ErrorCode.NOT_FOUND, "Conversation not found")
        pending = None
        if meta.mode == "intake":
            snapshot = await self._graph.aget_state(_config(conversation_id))
            if snapshot.interrupts:
                pending = PendingDraft.model_validate(snapshot.interrupts[0].value)
        return AssistantState(conversation_id=conversation_id, mode=meta.mode, pending=pending)


def build_intake(
    settings: Settings,
    resources: Resources,
    llm: LLMServices,
    ingestion: Ingestion,
    owner: OwnerDeps,
    *,
    on_turn: TurnHook | None = None,
    context: ContextBuilder | None = None,
    memory: MemoryService | None = None,
) -> tuple[IntakeService, MongoCheckpointSaver]:
    """`owner` is the owner graph's dependencies (`OwnerDeps`); its graph runs inside `answer`
    without checkpoints of its own."""
    ops = AdminOps(
        AdminDeps(
            settings=settings,
            kb=resources.kb,
            vectors=resources.vectors,
            blobs=resources.blobs,
            audit=resources.audit,
            profile=resources.profile,
            records=resources.records,
            redis=resources.redis,
            ingestion=ingestion.service,
            pipeline=ingestion.pipeline,
            leads=resources.leads,
        )
    )
    saver = MongoCheckpointSaver()
    deps = IntakeDeps(
        orchestrator=llm.orchestrator,
        ops=ops,
        inbox=settings.intake_inbox_dir,
        timezone=ZoneInfo(settings.owner_timezone),
        wait_seconds=settings.intake_ingest_wait_seconds,
        owner_graph=build_owner_graph(owner, checkpointer=False),
        conversations=resources.conversations,
        memory=memory,
    )
    service = IntakeService(
        build_intake_graph(deps, saver),
        resources.conversations,
        MongoConversationIndex(),
        llm.orchestrator,
        on_turn=on_turn,
        context=context,
    )
    return service, saver


async def prune_checkpoints(saver: MongoCheckpointSaver, days: int) -> None:
    """Startup housekeeping; a failure is logged, never fatal."""
    try:
        await saver.prune_finished(utcnow() - timedelta(days=days))
    except Exception as exc:
        logger.warning("checkpoint pruning failed", error_type=type(exc).__name__)
