"""The public graph: input_guard → scope → (fixed reply | profile template | retrieve → generate
→ output_guard).

Isolation is in code: the retriever is built with `kb_public` as its only collection and every
search runs at `Tier.PUBLIC` with no filters. Nothing in the state, the request or a prompt can
name a tier or a collection. Refusals end at `scope` with a fixed reply: no retrieval, no
generation. A generated answer is complete when it reaches `output_guard`, so nothing a visitor
sees has skipped it; the service sends tokens only once the graph is done.

Every node logs one `public step` line with counts, intent, rule names and latency. No visitor
text, context or answer text is logged.
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Protocol, TypedDict

import structlog
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from qdrant_client import AsyncQdrantClient

from app.agents.prompts import public as prompts
from app.guardrails.input import BLOCKED_REPLY, InputGuard
from app.guardrails.output import OutputGuard
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
from app.services.conversations import Turn
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


@dataclass(frozen=True)
class PublicDeps:
    input_guard: InputGuard
    router: ScopeRouter
    templates: ProfileTemplates
    retriever: PublicSearcher
    reranker: Reranker
    orchestrator: Orchestrator
    output_guard: OutputGuard
    settings: PublicSettings = field(default_factory=PublicSettings)


class PublicState(TypedDict, total=False):
    # input
    conversation_id: str
    question: str
    history: list[Turn]
    visitor_hash: str
    started: float
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
        if intent in FIXED_INTENTS:
            update["answer"] = FIXED_REPLIES[intent]
        elif intent in PROFILE_INTENTS:
            update["answer"] = await self.deps.templates.reply(intent)
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
            _step("generate", started, sources=0, fixed=True)
            return {"answer": prompts.NO_CONTEXT, "sources": [], "context": "", "generated": False}
        rendered = context.render()
        messages: list[BaseMessage] = [
            SystemMessage(content=prompts.GENERATE_V1),
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
        return {
            "answer": result.text.strip(),
            "sources": context.sources,
            "context": rendered,
            "generated": True,
            "model_id": result.model_id,
            "input_tokens": result.usage.input_tokens,
            "output_tokens": result.usage.output_tokens,
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
        _step("output_guard", started, passed=verdict.passed, rule=verdict.rule)
        return update


def _after_input(state: PublicState) -> str:
    return "end" if state.get("intent") == BLOCKED_INTENT else "scope"


def _after_scope(state: PublicState) -> str:
    return "retrieve" if state["intent"] == "about_owner" else "end"


def _after_generate(state: PublicState) -> str:
    return "output_guard" if state.get("generated") else "end"


PublicGraph = CompiledStateGraph[PublicState, None, PublicState, PublicState]


def build_public_graph(deps: PublicDeps) -> PublicGraph:
    nodes = PublicNodes(deps)
    graph = StateGraph(PublicState)
    graph.add_node("input_guard", nodes.input_guard)
    graph.add_node("scope", nodes.scope)
    graph.add_node("retrieve", nodes.retrieve)
    graph.add_node("generate", nodes.generate)
    graph.add_node("output_guard", nodes.output_guard)
    graph.add_edge(START, "input_guard")
    graph.add_conditional_edges("input_guard", _after_input, {"scope": "scope", "end": END})
    graph.add_conditional_edges("scope", _after_scope, {"retrieve": "retrieve", "end": END})
    graph.add_edge("retrieve", "generate")
    graph.add_conditional_edges(
        "generate", _after_generate, {"output_guard": "output_guard", "end": END}
    )
    graph.add_edge("output_guard", END)
    return graph.compile()


def initial_state(
    *, conversation_id: str, question: str, history: list[Turn], visitor_hash: str
) -> PublicState:
    return PublicState(
        conversation_id=conversation_id,
        question=question,
        history=history,
        visitor_hash=visitor_hash,
        started=time.perf_counter(),
    )
