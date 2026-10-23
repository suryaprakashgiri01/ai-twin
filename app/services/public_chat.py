"""Public chat: prepare a visitor's message, run the public graph, then deliver the answer.

Delivery is buffered: the graph finishes, the output guard has passed (or replaced) the whole
answer, and only then do `token` events go out. Events, in order: `meta` (conversation id and
intent, once routing is known), `token`*, `sources`, then `done`. An unexpected failure ends
with `error` instead. Both turns are stored after the answer is decided, with the intent and
the guard verdicts (rule names only) on the assistant turn.
"""

from __future__ import annotations

import re
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import partial
from typing import Any, Protocol, cast

import structlog
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.types import Command
from pydantic import ConfigDict, Field

from app.agents.checkpoint import MongoCheckpointSaver
from app.agents.graphs.public import (
    PublicDeps,
    PublicGraph,
    PublicSettings,
    build_public_graph,
    initial_state,
    public_retriever,
)
from app.agents.prompts import public as prompts
from app.agents.streaming import sse
from app.api.envelope import ApiModel
from app.auth.visitor import Visitor
from app.core.config import Settings, split_list
from app.core.llm import LLMServices
from app.core.resources import Resources
from app.exceptions import AppError, ErrorCode
from app.guardrails.canary_hashes import (
    CanaryHashSource,
    FileCanaryHashes,
    MongoCanaryHashes,
    StaticCanaryHashes,
)
from app.guardrails.input import GuardSettings, InputGuard, OrchestratorGuardModel
from app.guardrails.output import ClassifierJudge, OrchestratorJudge, OutputGuard, SwitchedJudge
from app.guardrails.scope import ProfileTemplates, ScopeRouter
from app.kb.embedding import Embedder
from app.kb.retrieval.context import Source
from app.kb.retrieval.rerank import CrossEncoderReranker
from app.kb.retrieval.retriever import Hit, MongoDocumentFilter
from app.leads.capture import OPPORTUNITY as OPPORTUNITY_INTENT
from app.leads.capture import CaptureDeps, CaptureSettings
from app.leads.service import LeadService
from app.observability.metrics import ANSWER_CACHE
from app.observability.tracing import TraceRun, current_run, tracing
from app.services.answer_cache import AnswerCache, CachedAnswer
from app.services.conversations import ConversationRef, ConversationService, Turn
from app.services.owner_chat import ConversationIndex, MongoConversationIndex
from app.services.profile import ProfileStore
from app.services.resume import ResumeProvider
from llm_orchestrator import Tier
from llm_orchestrator.classify import Classifier, FlaggedClassifier

logger = structlog.get_logger(__name__)

# A hard ceiling for the request body; the input guard enforces the real, lower cap.
REQUEST_MAX_CHARS = 8000
PUBLIC_THREAD_PREFIX = "public:"
INTERRUPT_KEY = "__interrupt__"
# Pending lead drafts (and every other public thread) are pruned after this long.
DRAFT_HOURS = 24
# Words per `token` event: the answer is already complete, this only paces the widget.
WORDS_PER_TOKEN = 3


class PublicChatRequest(ApiModel):
    # No tier, collection, model or filter field: nothing a visitor sends can widen retrieval.
    model_config = ConfigDict(extra="forbid")

    conversation_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")
    message: str = Field(min_length=1, max_length=REQUEST_MAX_CHARS)
    # Cloudflare Turnstile token from the widget; verified only when a lead is submitted.
    turnstile_token: str | None = Field(default=None, max_length=2048)


class PublicChatResponse(ApiModel):
    conversation_id: str
    seq: int | None = None
    intent: str
    answer: str
    sources: list[Source] = Field(default_factory=list)
    citations: list[str] = Field(default_factory=list)
    cache_hit: bool = False
    latency_ms: float | None = None


@dataclass(frozen=True)
class PublicPrepared:
    request: PublicChatRequest
    visitor: Visitor
    conversation_id: str
    # True for the first message of a conversation: only those use the answer cache.
    single_turn: bool
    history: list[Turn] = field(default_factory=list)


class PublicChat(Protocol):
    async def prepare(self, request: PublicChatRequest, visitor: Visitor) -> PublicPrepared: ...

    def sse(self, prepared: PublicPrepared) -> AsyncIterator[str]: ...

    async def answer(self, prepared: PublicPrepared) -> PublicChatResponse: ...


def thread_id(prepared: PublicPrepared) -> str:
    """Public checkpoint threads are namespaced, so their 24 h pruning touches nothing else."""
    return f"{PUBLIC_THREAD_PREFIX}{prepared.conversation_id}"


def token_pieces(answer: str, words: int = WORDS_PER_TOKEN) -> list[str]:
    parts = re.findall(r"\S+\s*", answer)
    return ["".join(parts[i : i + words]) for i in range(0, len(parts), words)] or [answer]


@dataclass
class Outcome:
    """The decided answer and what goes into the conversation record."""

    intent: str
    answer: str
    sources: list[Source]
    guards: dict[str, Any]
    text: str
    model_id: str | None = None
    tokens: int = 0
    cache_hit: bool = False
    seq: int | None = None
    latency_ms: float | None = None

    @classmethod
    def from_state(cls, state: dict[str, Any]) -> Outcome:
        names = ("input_guard", "scope", "output_guard")
        # A guard that did not run this turn is left empty by `initial_state`.
        guards: dict[str, Any] = {name: state[name] for name in names if state.get(name)}
        return cls(
            intent=state.get("intent") or "about_owner",
            answer=state.get("answer") or prompts.NO_CONTEXT,
            sources=state.get("sources", []),
            guards=guards,
            text=state.get("text") or state["question"],
            model_id=state.get("model_id"),
            tokens=state.get("input_tokens", 0) + state.get("output_tokens", 0),
        )

    @classmethod
    def from_cache(cls, cached: CachedAnswer, question: str) -> Outcome:
        return cls(
            intent=cached.intent,
            answer=cached.answer,
            sources=cached.sources,
            guards={**cached.guards, "cache": "hit"},
            text=question,
            model_id=cached.model,
            cache_hit=True,
        )


def guard_rules(guards: dict[str, Any]) -> list[str]:
    """Rule names the guards fired this turn (input rules, the output rule), for traces."""
    rules = list(guards.get("input_guard", {}).get("rules") or [])
    if rule := guards.get("output_guard", {}).get("rule"):
        rules.append(rule)
    return rules


def _trace_public(trace: TraceRun, outcome: Outcome) -> None:
    if outcome.intent == OPPORTUNITY_INTENT:
        trace.restrict("leads")
    rules = guard_rules(outcome.guards)
    trace.tag(
        f"intent:{outcome.intent}",
        "cache_hit" if outcome.cache_hit else "",
        *(f"rule:{rule}" for rule in rules),
    )
    trace.meta(
        intent=outcome.intent,
        cache_hit=outcome.cache_hit,
        model=outcome.model_id,
        verdicts=rules,
        seq=outcome.seq,
        prompt_version=prompts.PROMPT_VERSION,
    )
    trace.output(outcome.answer)
    output = outcome.guards.get("output_guard")
    # A cached answer was scored when it was generated.
    if output and not outcome.cache_hit:
        trace.score("output_guard", bool(output["passed"]), comment=output.get("rule"))
        trace.score("grounding", str(output["grounding"]), data_type="CATEGORICAL")


def cacheable(state: dict[str, Any]) -> bool:
    """Guarded answers only: passed the output guard with grounding checked, or the fixed
    no-context reply. Refusals cost nothing to repeat and are never cached."""
    if state.get("intent") != "about_owner":
        return False
    guard = state.get("output_guard")
    if not guard:
        return bool(state.get("answer") == prompts.NO_CONTEXT)
    return bool(guard["passed"]) and guard["grounding"] in ("supported", "skipped")


class PublicChatService:
    def __init__(
        self,
        graph: PublicGraph,
        conversations: ConversationService,
        index: ConversationIndex,
        cache: AnswerCache | None = None,
    ) -> None:
        self._graph = graph
        self._conversations = conversations
        self._index = index
        self._cache = cache

    async def prepare(self, request: PublicChatRequest, visitor: Visitor) -> PublicPrepared:
        """Everything that can fail with a plain error response, before any event is sent."""
        if request.conversation_id is None:
            ref = await self._conversations.start("public", visitor_hash=visitor.hash)
            return PublicPrepared(request, visitor, ref.id, single_turn=True)
        meta = await self._index.get(request.conversation_id)
        history = await self._conversations.history(request.conversation_id)
        foreign = meta is not None and (
            meta.channel != "public" or meta.visitor_hash != visitor.hash
        )
        # A brand-new conversation may not have reached MongoDB yet; Redis already has it.
        if foreign or (meta is None and not history):
            raise AppError(ErrorCode.NOT_FOUND, "Conversation not found")
        return PublicPrepared(
            request, visitor, request.conversation_id, single_turn=False, history=history
        )

    async def events(self, prepared: PublicPrepared) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        async for event in self._events(prepared, []):
            yield event

    async def _events(
        self, prepared: PublicPrepared, decided: list[Outcome]
    ) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        """The event stream; the decided outcome is also appended to `decided`."""
        with tracing().run(
            "public-chat",
            "public",
            input=prepared.request.message,
            session_id=prepared.conversation_id,
            metadata={"turns": len(prepared.history)},
        ) as trace:
            async for event in self._traced_events(prepared, decided):
                yield event
            if decided:
                _trace_public(trace, decided[0])
            else:
                trace.tag("outcome:error")

    async def _traced_events(
        self, prepared: PublicPrepared, decided: list[Outcome]
    ) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        started = time.perf_counter()
        try:
            version, cached = await self._lookup(prepared)
            if cached is not None:
                yield "meta", self._meta(prepared, cached.intent)
                outcome = Outcome.from_cache(cached, prepared.request.message)
            else:
                state: dict[str, Any] = {}
                async for event in self._run_graph(prepared, state):
                    yield event
                outcome = Outcome.from_state(state)
                if version is not None and cacheable(state):
                    await self._store(version, prepared, outcome)
            outcome.latency_ms = round((time.perf_counter() - started) * 1000, 1)
            await self._persist(prepared, outcome)
            decided.append(outcome)
        except AppError as exc:
            yield "error", {"code": exc.code.value, "message": exc.message}
            return
        except Exception as exc:
            logger.error("public chat failed", error_type=type(exc).__name__)
            yield "error", {"code": ErrorCode.INTERNAL_ERROR.value, "message": "Chat failed"}
            return
        for event in self._deliver(prepared, outcome):
            yield event

    async def _run_graph(
        self, prepared: PublicPrepared, state: dict[str, Any]
    ) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        """Run the graph into `state`, announcing the intent as soon as it is known. A thread
        paused on a lead confirmation is resumed with the message instead of started anew."""
        values = initial_state(
            conversation_id=prepared.conversation_id,
            question=prepared.request.message,
            history=prepared.history,
            visitor_hash=prepared.visitor.hash,
            turnstile_token=prepared.request.turnstile_token,
        )
        state.update(values)
        config: RunnableConfig = {"configurable": {"thread_id": thread_id(prepared)}}
        graph_input: Any = dict(values)
        options: dict[str, Any] = {}
        if self._graph.checkpointer is not None:
            # One checkpoint per turn (or pause) instead of one per node.
            options["durability"] = "exit"
            snapshot = await self._graph.aget_state(config)
            if snapshot.values.get("lead"):
                # A lead is being gathered: whatever the visitor types may be its fields.
                current_run().restrict("leads")
            if snapshot.interrupts:
                graph_input = Command(resume=prepared.request.message, update=dict(values))
        async for item in self._graph.astream(
            graph_input, config, stream_mode="updates", **options
        ):
            for node, update in cast(dict[str, Any], item).items():
                if node == INTERRUPT_KEY:
                    continue
                state.update(update or {})
                if node == "scope" or (node == "input_guard" and update and "intent" in update):
                    yield "meta", self._meta(prepared, state["intent"])

    async def _lookup(self, prepared: PublicPrepared) -> tuple[int | None, CachedAnswer | None]:
        """Only a conversation's first message can be answered from (or stored in) the cache."""
        if not prepared.single_turn or self._cache is None or not self._cache.enabled:
            return None, None
        version, cached = await self._cache.lookup(prepared.request.message)
        ANSWER_CACHE.labels("hit" if cached is not None else "miss").inc()
        return version, cached

    async def _store(self, version: int, prepared: PublicPrepared, outcome: Outcome) -> None:
        assert self._cache is not None
        cached = CachedAnswer(
            intent=outcome.intent,
            answer=outcome.answer,
            sources=outcome.sources,
            model=outcome.model_id,
            guards=outcome.guards,
        )
        await self._cache.store(version, prepared.request.message, cached)

    @staticmethod
    def _meta(prepared: PublicPrepared, intent: str) -> dict[str, Any]:
        return {"conversation_id": prepared.conversation_id, "intent": intent}

    def _deliver(
        self, prepared: PublicPrepared, outcome: Outcome
    ) -> list[tuple[str, dict[str, Any]]]:
        events: list[tuple[str, dict[str, Any]]] = [
            ("token", {"text": piece}) for piece in token_pieces(outcome.answer)
        ]
        events.append(
            (
                "sources",
                {
                    "sources": [s.model_dump(mode="json") for s in outcome.sources],
                    "citations": [s.document_id for s in outcome.sources],
                },
            )
        )
        response = self._response(prepared, outcome)
        events.append(("done", response.model_dump(mode="json", exclude={"answer", "sources"})))
        return events

    @staticmethod
    def _response(prepared: PublicPrepared, outcome: Outcome) -> PublicChatResponse:
        return PublicChatResponse(
            conversation_id=prepared.conversation_id,
            seq=outcome.seq,
            intent=outcome.intent,
            answer=outcome.answer,
            sources=outcome.sources,
            citations=[s.document_id for s in outcome.sources],
            cache_hit=outcome.cache_hit,
            latency_ms=outcome.latency_ms,
        )

    async def _persist(self, prepared: PublicPrepared, outcome: Outcome) -> None:
        ref = ConversationRef(
            id=prepared.conversation_id, channel="public", visitor_hash=prepared.visitor.hash
        )
        user = Turn(role="user", content=outcome.text, intent=outcome.intent)
        await self._conversations.append_turn(ref, user)
        assistant = await self._conversations.append_turn(
            ref,
            Turn(
                role="assistant",
                content=outcome.answer,
                intent=outcome.intent,
                guards=outcome.guards,
                max_tier="public",
                model=outcome.model_id,
                tokens=outcome.tokens or None,
                latency_ms=outcome.latency_ms,
                citations=[s.document_id for s in outcome.sources],
                cache_hit=outcome.cache_hit,
            ),
        )
        outcome.seq = assistant.seq
        logger.info(
            "public turn",
            conversation_id=prepared.conversation_id,
            seq=assistant.seq,
            intent=outcome.intent,
            sources=len(outcome.sources),
            cache_hit=outcome.cache_hit,
            latency_ms=outcome.latency_ms,
        )

    async def sse(self, prepared: PublicPrepared) -> AsyncIterator[str]:
        async for event, data in self.events(prepared):
            yield sse(event, data)

    async def answer(self, prepared: PublicPrepared) -> PublicChatResponse:
        """The non-streaming form: the `done` fields plus the answer and its sources."""
        decided: list[Outcome] = []
        async for event, data in self._events(prepared, decided):
            if event == "error":
                raise AppError(ErrorCode(data["code"]), data["message"])
        return self._response(prepared, decided[0])


# --- wiring ----------------------------------------------------------------------------------


def _jev_on(classifier: Classifier) -> bool:
    return isinstance(classifier, FlaggedClassifier) and classifier.uses_jev(Tier.PUBLIC)


def canary_source(settings: Settings) -> CanaryHashSource:
    """Vault: the `canaries` collection. Public: the synced file deploy-public provisions."""
    if settings.deployment_profile == "vault":
        return MongoCanaryHashes()
    if settings.public_canary_hashes_path is not None:
        return FileCanaryHashes(settings.public_canary_hashes_path)
    logger.warning("no canary hash file configured; the canary scan has nothing to match")
    return StaticCanaryHashes()


def public_serde() -> JsonPlusSerializer:
    """Checkpoint serialisation for public threads: only the state's own types are revived."""
    return JsonPlusSerializer(allowed_msgpack_modules=[Hit, Source, Turn, Tier])


def public_checkpointer() -> MongoCheckpointSaver:
    return MongoCheckpointSaver(serde=public_serde())


async def prune_public(saver: MongoCheckpointSaver, now: datetime) -> int:
    """Drop public threads idle for DRAFT_HOURS, a pending lead confirmation included."""
    return await saver.prune_finished(
        now - timedelta(hours=DRAFT_HOURS), prefix=PUBLIC_THREAD_PREFIX, keep_waiting=False
    )


async def _reply_time(profile: ProfileStore) -> str | None:
    return (await profile.get()).reply_time


def build_public_chat(
    settings: Settings,
    resources: Resources,
    llm: LLMServices,
    embedder: Embedder,
    resume: ResumeProvider,
    *,
    leads: LeadService | None = None,
    checkpointer: MongoCheckpointSaver | None = None,
) -> PublicChatService:
    """With `leads` (and a checkpointer) an opportunity starts the capture loop; without, it
    gets the fixed contact-form reply."""
    orchestrator, classifier = llm.orchestrator, llm.classifier
    input_guard = InputGuard(
        OrchestratorGuardModel(orchestrator),
        GuardSettings(
            max_chars=settings.public_max_message_chars,
            threshold=settings.prompt_guard_threshold,
            borderline=settings.prompt_guard_borderline,
        ),
        second_opinion=classifier,
        second_opinion_enabled=lambda: _jev_on(classifier),
    )
    system_prompt = prompts.generate_prompt(settings.public_owner_name)
    output_guard = OutputGuard(
        canaries=canary_source(settings),
        # The prompt in use is the one a leak would echo.
        system_prompts=[system_prompt],
        allowlist=split_list(settings.public_contact_allowlist),
        judge=SwitchedJudge(
            default=OrchestratorJudge(orchestrator),
            flagged=ClassifierJudge(classifier),
            use_flagged=lambda: _jev_on(classifier),
        ),
    )
    deps = PublicDeps(
        input_guard=input_guard,
        router=ScopeRouter(classifier),
        templates=ProfileTemplates(resources.profile, resume.available),
        retriever=public_retriever(
            resources.qdrant,
            embedder,
            MongoDocumentFilter(),
            prefetch=settings.retrieval_prefetch,
        ),
        reranker=CrossEncoderReranker(
            settings.models_dir / "fastembed", min_score=settings.rerank_min_score
        ),
        orchestrator=orchestrator,
        output_guard=output_guard,
        events=resources.security_events,
        settings=PublicSettings(
            system_prompt=system_prompt,
            top_k=settings.retrieval_top_k,
            context_tokens=settings.public_context_tokens,
            history_turns=settings.public_history_turns,
        ),
        capture=None
        if leads is None or checkpointer is None
        else CaptureDeps(
            orchestrator=orchestrator,
            service=leads,
            output_guard=output_guard,
            reply_time=partial(_reply_time, resources.profile),
            settings=CaptureSettings(
                max_turns=settings.lead_max_turns, owner_name=settings.public_owner_name
            ),
        ),
    )
    return PublicChatService(
        build_public_graph(deps, checkpointer if deps.capture is not None else None),
        resources.conversations,
        MongoConversationIndex(),
        AnswerCache(resources.redis, settings.public_answer_cache_ttl_seconds),
    )
