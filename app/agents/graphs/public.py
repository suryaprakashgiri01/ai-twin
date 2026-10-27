"""The public graph: input_guard → scope → (fixed reply | profile template | capture | retrieve
→ generate → output_guard).

With lead capture configured (`PublicDeps.capture`), an `opportunity` or an open draft goes to
`capture` (`app.leads.capture`), which may pause at `confirm` and, on an explicit yes, run
`submit`. The graph then needs a checkpointer: the draft lives in the thread's state
(`public:<conversation id>`) between turns. Every turn still starts at `input_guard`, a resumed
confirmation included.

Isolation is in code: the retriever is built with `kb_public` as its only collection and every
search runs at `Tier.PUBLIC` with no filters. Nothing in the state, the request or a prompt can
name a tier or a collection. Refusals end at `scope` with a fixed reply: no retrieval, no
generation. A generated answer is complete when it reaches `output_guard`, so nothing a visitor
sees has skipped it; the service sends tokens only once the graph is done.

Every node logs one `public step` line with counts, intent, rule names and latency. No visitor
text, context or answer text is logged.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Protocol, TypedDict, cast

import structlog
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Checkpointer
from qdrant_client import AsyncQdrantClient

from app.agents.prompts import public as prompts
from app.guardrails.input import BLOCKED_REPLY, InputGuard
from app.guardrails.output import OutputGuard, OutputVerdict
from app.guardrails.scope import (
    FIXED_INTENTS,
    FIXED_REPLIES,
    PROFILE_INTENTS,
    Intent,
    ProfileTemplates,
    ScopeRouter,
)
from app.kb.embedding import Embedder
from app.kb.retrieval.context import Source, build_context
from app.kb.retrieval.rerank import Reranker
from app.kb.retrieval.retriever import DocumentFilter, Hit, Retriever
from app.leads.capture import OPPORTUNITY, CaptureDeps, CaptureNodes
from app.observability.metrics import GUARD_DECISIONS, SCOPE_INTENTS
from app.services.conversations import Turn
from app.services.public_fallback import PublicFallback
from app.services.security_events import Detail, SecurityEventWriter
from llm_orchestrator import Orchestrator, OrchestratorError, Role, Tier

logger = structlog.get_logger(__name__)

# The only collection the public graph can search. Not configurable, by design.
PUBLIC_COLLECTIONS: Mapping[str, Tier] = MappingProxyType({"kb_public": Tier.PUBLIC})
PUBLIC_TIER = Tier.PUBLIC
BLOCKED_INTENT = "blocked"


class PublicSearcher(Protocol):
    async def search(self, query: str, *, max_tier: Tier, limit: int) -> list[Hit]: ...


def public_retriever(
    client: AsyncQdrantClient, embedder: Embedder, documents: DocumentFilter, *, prefetch: int
) -> Retriever:
    """The public graph's retriever: `kb_public` only, whatever else exists in Qdrant."""
    return Retriever(
        client,
        embedder,
        documents,
        collections=PUBLIC_COLLECTIONS,
        prefetch_limit=prefetch,
    )


@dataclass(frozen=True)
class PublicSettings:
    top_k: int = 6
    candidates: int = 20
    context_tokens: int = 1500
    history_turns: int = 4
    # The generation prompt (`prompts.generate_prompt(owner name)`); v1 when unset.
    system_prompt: str = prompts.GENERATE_V1


@dataclass(frozen=True)
class PublicDeps:
    input_guard: InputGuard
    router: ScopeRouter
    templates: ProfileTemplates
    retriever: PublicSearcher
    reranker: Reranker
    orchestrator: Orchestrator
    output_guard: OutputGuard
    events: SecurityEventWriter
    settings: PublicSettings = field(default_factory=PublicSettings)
    # Lead capture; None keeps the fixed contact-form reply for opportunities.
    capture: CaptureDeps | None = None
    # The owner's public projects, used when the knowledge base has nothing to say.
    fallback: PublicFallback | None = None


class PublicState(TypedDict, total=False):
    # input
    conversation_id: str
    question: str
    history: list[Turn]
    visitor_hash: str
    started: float
    # The Turnstile token the widget sends with a confirmation; checked on submit only.
    turnstile_token: str | None
    # An opportunity draft ({stage, draft, asked, turns}); kept across turns by the checkpointer.
    lead: dict[str, Any] | None
    # input_guard
    text: str
    input_guard: dict[str, Any]
    # scope
    intent: str
    scope: dict[str, Any]
    # retrieve
    hits: list[Hit]
    # generate
    context: str
    sources: list[Source]
    answer: str
    generated: bool
    model_id: str | None
    input_tokens: int
    output_tokens: int
    # output_guard
    output_guard: dict[str, Any]


def _ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 1)


def _step(step: str, started: float, **fields: Any) -> None:
    logger.info("public step", step=step, latency_ms=_ms(started), **fields)


def _fingerprint(text: str) -> str:
    """A short, stable id for a question: enough to match a log line with a stored
    conversation, without logging what the visitor wrote."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def history_messages(history: Sequence[Turn], limit: int) -> list[BaseMessage]:
    """Recent answered turns only: refusals and blocked messages never reach a model again."""
    answered = [t for t in history if t.intent == "about_owner"][-limit:] if limit else []
    return [
        HumanMessage(content=t.content) if t.role == "user" else AIMessage(content=t.content)
        for t in answered
    ]


class PublicNodes:
    def __init__(self, deps: PublicDeps) -> None:
        self.deps = deps
        self.settings = deps.settings

    async def input_guard(self, state: PublicState) -> dict[str, Any]:
        started = time.perf_counter()
        verdict = await self.deps.input_guard.check(state["question"])
        update: dict[str, Any] = {"text": verdict.text, "input_guard": verdict.summary()}
        if not verdict.allowed:
            update |= {"intent": BLOCKED_INTENT, "answer": BLOCKED_REPLY}
            await self.deps.events.record(
                "input_blocked",
                "low" if verdict.layer == "length" else "medium",
                rules=verdict.rules,
                visitor_hash=state["visitor_hash"],
                details={
                    "layer": verdict.layer,
                    "score": verdict.score,
                    "conversation_id": state["conversation_id"],
                },
            )
        GUARD_DECISIONS.labels(
            f"input:{verdict.layer or 'all'}", "allowed" if verdict.allowed else "blocked"
        ).inc()
        _step("input_guard", started, allowed=verdict.allowed, rules=list(verdict.rules))
        return update

    async def scope(self, state: PublicState) -> dict[str, Any]:
        started = time.perf_counter()
        decision = await self.deps.router.route(state["text"])
        intent: Intent = decision.intent
        update: dict[str, Any] = {
            "intent": intent,
            "scope": {
                "intent": intent,
                "source": decision.source,
                "model": decision.model,
                "corrected": decision.corrected,
            },
        }
        if intent == "meta_probe":
            await self.deps.events.record(
                "meta_probe",
                "low",
                rules=[f"scope:{decision.source}"],
                visitor_hash=state["visitor_hash"],
                details={"conversation_id": state["conversation_id"]},
            )
        if intent in FIXED_INTENTS:
            update["answer"] = FIXED_REPLIES[intent]
        elif intent in PROFILE_INTENTS:
            update["answer"] = await self.deps.templates.reply(intent)
        SCOPE_INTENTS.labels(intent).inc()
        _step("scope", started, intent=intent, source=decision.source)
        return update

    async def retrieve(self, state: PublicState) -> dict[str, Any]:
        started = time.perf_counter()
        query = state["text"]
        candidates = await self.deps.retriever.search(
            query, max_tier=PUBLIC_TIER, limit=self.settings.candidates
        )
        hits = await self.deps.reranker.rerank(query, candidates, self.settings.top_k)
        _step("retrieve", started, candidates=len(candidates), hits=len(hits))
        return {"hits": hits}

    async def generate(self, state: PublicState) -> dict[str, Any]:
        started = time.perf_counter()
        context = build_context(
            state.get("hits", []), budget_tokens=self.settings.context_tokens, ceiling=PUBLIC_TIER
        )
        if context.empty:
            fallback = self._fallback(state, "empty_context")
            if fallback is not None:
                return fallback
            _step("generate", started, sources=0, fixed=True)
            return {"answer": prompts.NO_CONTEXT, "sources": [], "context": "", "generated": False}
        rendered = context.render()
        messages: list[BaseMessage] = [
            SystemMessage(content=self.settings.system_prompt),
            *history_messages(state.get("history", []), self.settings.history_turns),
            HumanMessage(content=f"Context:\n{rendered}\n\nQuestion: {state['text']}"),
        ]
        try:
            result = await self.deps.orchestrator.chat(Role.GENERATOR, PUBLIC_TIER, messages)
        except OrchestratorError as exc:
            logger.warning("public generation failed", error_type=type(exc).__name__)
            _step("generate", started, sources=len(context.sources), error=type(exc).__name__)
            return {"answer": prompts.UNAVAILABLE, "sources": [], "context": "", "generated": False}
        _step(
            "generate",
            started,
            sources=len(context.sources),
            model=result.model_id,
            fallback=result.fallback_used,
            input_tokens=result.usage.input_tokens,
            output_tokens=result.usage.output_tokens,
        )
        answer = result.text.strip()
        if answer == prompts.NO_CONTEXT:
            # The model found nothing usable in the context. The visitor still gets the
            # owner's public projects, and the gap is logged for the owner to close.
            fallback = self._fallback(state, "model_no_context")
            if fallback is not None:
                return fallback
        return {
            "answer": answer,
            "sources": context.sources,
            "context": rendered,
            "generated": True,
            "model_id": result.model_id,
            "input_tokens": result.usage.input_tokens,
            "output_tokens": result.usage.output_tokens,
        }

    def _fallback(self, state: PublicState, reason: str) -> dict[str, Any] | None:
        """The owner's public projects, when the knowledge base has nothing to answer with.

        Logged as a `kb fallback` event (intent, reason and a fingerprint of the question, no
        visitor text) so the knowledge-base gap behind it can be found and closed.
        """
        fallback = self.deps.fallback
        if fallback is None or not fallback.available:
            return None
        logger.info(
            "kb fallback",
            reason=reason,
            intent=state.get("intent"),
            fingerprint=_fingerprint(state.get("text", "")),
            source="public_projects",
        )
        return {
            "answer": fallback.answer(),
            "sources": [],
            "context": fallback.context(),
            "generated": False,
        }

    async def output_guard(self, state: PublicState) -> dict[str, Any]:
        started = time.perf_counter()
        verdict = await self.deps.output_guard.check(
            state["answer"], question=state["text"], context=state.get("context", "")
        )
        update: dict[str, Any] = {"output_guard": verdict.summary()}
        if not verdict.passed:
            # The visitor sees only the safe fallback; the blocked answer goes nowhere.
            update |= {"answer": prompts.NO_CONTEXT, "sources": []}
        GUARD_DECISIONS.labels(
            f"output:{verdict.check or 'all'}", "passed" if verdict.passed else "blocked"
        ).inc()
        await self._record_output(state, verdict)
        _step("output_guard", started, passed=verdict.passed, rule=verdict.rule)
        return update

    async def _record_output(self, state: PublicState, verdict: OutputVerdict) -> None:
        details: dict[str, Detail] = {
            "check": verdict.check,
            "conversation_id": state["conversation_id"],
        }
        if not verdict.passed and verdict.rule is not None:
            canary = verdict.check == "canary"
            await self.deps.events.record(
                "canary_hit" if canary else "output_blocked",
                "critical" if canary else verdict.severity or "high",
                rules=[verdict.rule],
                visitor_hash=state["visitor_hash"],
                details=details,
            )
        elif "grounding_fail_open" in verdict.notes:
            await self.deps.events.record(
                "grounding_fail_open",
                "low",
                rules=["grounding_fail_open"],
                visitor_hash=state["visitor_hash"],
                details=details,
            )


def _after_input(state: PublicState) -> str:
    return "end" if state.get("intent") == BLOCKED_INTENT else "scope"


def _after_scope(state: PublicState) -> str:
    return "retrieve" if state["intent"] == "about_owner" else "end"


def _after_scope_capture(state: PublicState) -> str:
    if state.get("lead") is not None or state["intent"] == OPPORTUNITY:
        return "capture"
    return _after_scope(state)


def _after_generate(state: PublicState) -> str:
    return "output_guard" if state.get("generated") else "end"


PublicGraph = CompiledStateGraph[PublicState, None, PublicState, PublicState]


def build_public_graph(deps: PublicDeps, checkpointer: Checkpointer = None) -> PublicGraph:
    nodes = PublicNodes(deps)
    graph = StateGraph(PublicState)
    graph.add_node("input_guard", nodes.input_guard)
    graph.add_node("scope", nodes.scope)
    graph.add_node("retrieve", nodes.retrieve)
    graph.add_node("generate", nodes.generate)
    graph.add_node("output_guard", nodes.output_guard)
    graph.add_edge(START, "input_guard")
    graph.add_conditional_edges("input_guard", _after_input, {"scope": "scope", "end": END})
    if deps.capture is None:
        graph.add_conditional_edges("scope", _after_scope, {"retrieve": "retrieve", "end": END})
    else:
        # The capture nodes read the same state as plain mappings.
        capture = CaptureNodes(deps.capture)
        graph.add_node(
            "capture",
            cast(Any, capture.capture),
            destinations=("confirm", "submit", "retrieve", END),
        )
        graph.add_node("confirm", cast(Any, capture.confirm), destinations=("input_guard",))
        graph.add_node("submit", cast(Any, capture.submit))
        graph.add_edge("submit", END)
        graph.add_conditional_edges(
            "scope",
            _after_scope_capture,
            {"capture": "capture", "retrieve": "retrieve", "end": END},
        )
    graph.add_edge("retrieve", "generate")
    graph.add_conditional_edges(
        "generate", _after_generate, {"output_guard": "output_guard", "end": END}
    )
    graph.add_edge("output_guard", END)
    return graph.compile(checkpointer=checkpointer)


def initial_state(
    *,
    conversation_id: str,
    question: str,
    history: list[Turn],
    visitor_hash: str,
    turnstile_token: str | None = None,
) -> PublicState:
    """A turn's input. Every per-turn value is reset here: with a checkpointer the thread's
    state carries over, and only the draft (`lead`) may."""
    return PublicState(
        conversation_id=conversation_id,
        question=question,
        history=history,
        visitor_hash=visitor_hash,
        started=time.perf_counter(),
        turnstile_token=turnstile_token,
        text="",
        input_guard={},
        intent="",
        scope={},
        hits=[],
        context="",
        sources=[],
        answer="",
        generated=False,
        model_id=None,
        input_tokens=0,
        output_tokens=0,
        output_guard={},
    )
