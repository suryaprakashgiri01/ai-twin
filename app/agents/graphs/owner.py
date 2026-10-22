"""The owner graph: route → retrieve | records | profile → generate → finalize.

Overview questions ("summarise all my projects") run in broad mode: document cards first, more
results and a larger context. A short follow-up whose own search finds nothing reuses the
documents the previous answer cited.

Every node logs one `owner step` line with counts, tiers, model and latency. No question,
context or answer text is ever logged at INFO or above. Answer text reaches the caller through
LangGraph's custom stream (`{"token": ...}` chunks) and the final state.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal, Protocol, TypedDict, cast

import structlog
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Checkpointer
from pydantic import BaseModel

from app.agents.context import OwnerContext
from app.agents.generation import AnswerGenerator, Generation, MidStreamError
from app.agents.models import AUTO_CHOICE, ModelChoice
from app.agents.prompts import owner as prompts
from app.agents.routing import (
    GuardResult,
    Route,
    RouteDecision,
    guard_records,
    heuristic_route,
    looks_like_records,
    looks_like_write,
    records_query,
)
from app.agents.tools.profile import ProfileFacts, ProfileTool
from app.agents.tools.records import RecordsQuery, RecordsResult, RecordsTool
from app.db.documents import Tier as TierLabel
from app.db.documents import utcnow
from app.exceptions import ErrorCode
from app.kb.retrieval.context import Source, build_context
from app.kb.retrieval.rerank import Reranker
from app.kb.retrieval.retriever import Hit, SearchFilters
from app.services.conversations import ConversationRef, ConversationService, Turn
from llm_orchestrator import (
    NoEligibleModelError,
    Orchestrator,
    OrchestratorError,
    OverrideRejectedError,
    Prefer,
    Rejection,
    Role,
    Tier,
)

logger = structlog.get_logger(__name__)

# Router input: each history turn is cut to this many characters.
ROUTER_TURN_CHARS = 600
NEEDS_PRIVATE = "That needs private records; ask again with max_tier private or higher."
_BROAD = re.compile(
    r"\b(summar(y|ise|ize)|overview|all (of )?my|list (all |my )|every|each of my|"
    r"what (projects|work) (have|did) i)\b",
    re.IGNORECASE,
)


# Words that ask for more of the same without naming a topic ("give me the summary").
_FOLLOW_UP_WORDS = frozenset(
    [
        "a",
        "an",
        "and",
        "about",
        "again",
        "all",
        "any",
        "brief",
        "briefly",
        "can",
        "could",
        "detail",
        "details",
        "do",
        "elaborate",
        "else",
        "explain",
        "expand",
        "give",
        "in",
        "it",
        "its",
        "me",
        "more",
        "of",
        "on",
        "please",
        "short",
        "shorter",
        "longer",
        "summarise",
        "summarize",
        "summary",
        "tell",
        "that",
        "the",
        "them",
        "these",
        "this",
        "those",
        "what",
        "you",
    ]
)


def is_broad(question: str) -> bool:
    """An overview question, answered from document cards rather than a few chunks."""
    return bool(_BROAD.search(question))


def is_vague_follow_up(question: str) -> bool:
    """A follow-up that names no topic of its own, so only the previous answer can ground it."""
    words = re.findall(r"[a-z']+", question.lower())
    return bool(words) and all(w in _FOLLOW_UP_WORDS for w in words)


class Searcher(Protocol):
    async def search(
        self,
        query: str,
        *,
        max_tier: Tier,
        filters: SearchFilters | None,
        limit: int,
        cards_only: bool = False,
        document_ids: Sequence[str] | None = None,
    ) -> list[Hit]: ...


@dataclass(frozen=True)
class OwnerSettings:
    top_k: int = 6
    candidates: int = 20
    context_tokens: int = 3000
    history_turns: int = 8
    # Broad mode: overview questions.
    broad_top_k: int = 12
    broad_context_tokens: int = 6000
    # A follow-up this short may reuse the previous answer's documents.
    follow_up_words: int = 12


@dataclass(frozen=True)
class OwnerDeps:
    orchestrator: Orchestrator
    retriever: Searcher
    reranker: Reranker
    records: RecordsTool
    profile: ProfileTool
    conversations: ConversationService
    generator: AnswerGenerator
    settings: OwnerSettings = field(default_factory=OwnerSettings)
    # config/agents.yaml for this agent; a caller's state may pass another (the super agent).
    models: ModelChoice = AUTO_CHOICE
    today: Callable[[], date] = field(default=lambda: utcnow().date())


class ErrorInfo(BaseModel):
    code: ErrorCode
    message: str


class OwnerState(TypedDict, total=False):
    # input
    conversation_id: str
    question: str
    history: list[Turn]
    max_tier: Tier
    filters: SearchFilters | None
    model: str | None
    prefer: Prefer | None
    # Raises every model call to at least this tier (the intake assistant passes confidential,
    # so only local models answer there).
    min_tier: Tier | None
    # Overrides the graph's own model choice (the super agent passes its own).
    models: ModelChoice | None
    # The owner's active preferences and the conversation summary (never for visitors).
    context: OwnerContext | None
    started: float
    # route
    route: Route
    query: str
    route_source: Literal["model", "heuristic"]
    router_model: str | None
    records_query: RecordsQuery | None
    # gathered
    broad: bool
    reused: bool
    hits: list[Hit]
    records: RecordsResult | None
    profile: ProfileFacts | None
    # generated
    answer: str
    tier: Tier
    model_id: str | None
    sources: list[Source]
    withheld: int
    incomplete: bool
    error: ErrorInfo | None
    input_tokens: int
    output_tokens: int
    # finalized
    seq: int
    latency_ms: float


@dataclass
class Plan:
    """What generate will do: a fixed answer, or messages for a model at `tier`."""

    tier: Tier
    messages: list[BaseMessage] = field(default_factory=list)
    fixed: str | None = None
    sources: list[Source] = field(default_factory=list)
    withheld: int = 0


def turn_tier(turn: Turn) -> Tier:
    return Tier.parse(turn.max_tier) if turn.max_tier else Tier.PRIVATE


def floor_tier(max_tier: Tier) -> Tier:
    """The owner's own question counts as private unless the caller capped the tier lower."""
    return min(Tier.PRIVATE, max_tier)


def state_floor(state: OwnerState) -> Tier:
    floor = floor_tier(state["max_tier"])
    minimum = state.get("min_tier")
    return max(floor, minimum) if minimum is not None else floor


def visible_history(history: Sequence[Turn], ceiling: Tier, limit: int) -> list[Turn]:
    """Recent turns a model at `ceiling` may read; anything above it is left out."""
    allowed = [t for t in history if turn_tier(t) <= ceiling]
    return allowed[-limit:] if limit else []


def history_messages(turns: Sequence[Turn], max_chars: int | None = None) -> list[BaseMessage]:
    messages: list[BaseMessage] = []
    for turn in turns:
        text = turn.content if max_chars is None else turn.content[:max_chars]
        messages.append(
            HumanMessage(content=text) if turn.role == "user" else AIMessage(content=text)
        )
    return messages


def _ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 1)


def _label(tier: Tier) -> TierLabel:
    return cast(TierLabel, tier.label)


def _step(step: str, started: float, **fields: Any) -> None:
    logger.info("owner step", step=step, latency_ms=_ms(started), **fields)


def choose(state: Any, default: ModelChoice, tier: Tier) -> tuple[str | None, Prefer | None]:
    """The generator model and prefer for a step at `tier` (request > agent config > auto)."""
    models: ModelChoice = state.get("models") or default
    return models.pick(tier, state.get("model"), state.get("prefer"))


class OwnerNodes:
    def __init__(self, deps: OwnerDeps) -> None:
        self.deps = deps
        self.settings = deps.settings

    # --- route -----------------------------------------------------------------------------

    async def route(self, state: OwnerState) -> dict[str, Any]:
        started = time.perf_counter()
        question = state["question"]
        history = visible_history(state["history"], state["max_tier"], self.settings.history_turns)
        tier = max([state_floor(state), *(turn_tier(t) for t in history)])
        previous = next((t.content for t in reversed(history) if t.role == "user"), None)
        if looks_like_write(question):
            # No model call: owner chat never writes, so the answer is always the same pointer.
            _step("route", started, route="write", source="rule", tier=tier.label)
            return {"route": "write", "query": question, "route_source": "rule"}
        decision, source, model_id = await self._decide(question, previous, history, tier, state)
        # Only the model's decision is checked; the keyword router already reads the question.
        guarded = (
            guard_records(decision, question, previous, self.deps.today())
            if source == "model"
            else GuardResult(decision)
        )
        decision = guarded.decision
        records = decision.records
        route: Route = decision.route
        if route == "records" and records is None:
            records = records_query(decision.query, self.deps.today())
            route = "records" if records is not None else "kb_search"
        _step(
            "route",
            started,
            route=route,
            source=source,
            tier=tier.label,
            model=model_id,
            history_turns=len(history),
            rewritten=decision.query.strip() != question.strip(),
            query_chars=len(decision.query),
            guard=guarded.guard,
            dropped=list(guarded.dropped),
        )
        return {
            "route": route,
            "query": decision.query,
            "route_source": source,
            "router_model": model_id,
            "records_query": records,
        }

    async def _decide(
        self,
        question: str,
        previous: str | None,
        history: list[Turn],
        tier: Tier,
        state: OwnerState,
    ) -> tuple[RouteDecision, Literal["model", "heuristic"], str | None]:
        system = prompts.ROUTE_V1.format(today=self.deps.today().isoformat())
        messages = [
            SystemMessage(content=system),
            *history_messages(history, ROUTER_TURN_CHARS),
            HumanMessage(content=f"Question: {question}"),
        ]
        try:
            result = await self.deps.orchestrator.structured_call(
                Role.ROUTER,
                tier,
                RouteDecision,
                messages,
                prefer=state.get("prefer"),
                privileged=True,
            )
            return result.value, "model", result.model_id
        except OrchestratorError as exc:
            logger.warning("router fell back to keywords", error_type=type(exc).__name__)
        return heuristic_route(question, previous, self.deps.today()), "heuristic", None

    # --- gather ----------------------------------------------------------------------------

    async def retrieve(self, state: OwnerState) -> dict[str, Any]:
        started = time.perf_counter()
        query = state["query"]
        broad = is_broad(state["question"]) or is_broad(query)
        top_k = self.settings.broad_top_k if broad else self.settings.top_k
        cited = self._follow_up_documents(state)
        if cited and is_vague_follow_up(state["question"]):
            # "Give me the summary" names nothing a search can match: stay on the last answer.
            hits = _merge(await self._cards(query, state, cited), [], top_k)
            hits = _merge(hits, await self._search_in(query, state, cited), 2 * top_k)
            if hits:
                return self._retrieved(started, len(hits), hits, broad=broad, reused=True)
        candidates = await self.deps.retriever.search(
            query,
            max_tier=state["max_tier"],
            filters=state.get("filters"),
            limit=self.settings.candidates,
        )
        hits = await self.deps.reranker.rerank(query, candidates, top_k)
        if broad:
            # Cards first (one per document), then the best chunks for detail.
            hits = _merge(await self._cards(query, state, None), hits, 2 * top_k)
        update: dict[str, Any] = {}
        # A small router can send a counting question to the KB: try the records tool once.
        if not hits and looks_like_records(query):
            fallback = records_query(query, self.deps.today())
            if fallback is not None:
                update = {"route": "records", "records_query": fallback}
        return self._retrieved(started, len(candidates), hits, broad=broad, reused=False, **update)

    @staticmethod
    def _retrieved(
        started: float, candidates: int, hits: list[Hit], *, broad: bool, reused: bool, **extra: Any
    ) -> dict[str, Any]:
        _step(
            "retrieve",
            started,
            candidates=candidates,
            hits=len(hits),
            cards=sum(h.card for h in hits),
            broad=broad,
            reused=reused,
            tiers=sorted({h.tier.label for h in hits}),
            rerouted=extra.get("route") == "records",
        )
        return {"hits": hits, "broad": broad, "reused": reused, **extra}

    async def _cards(
        self, query: str, state: OwnerState, document_ids: Sequence[str] | None
    ) -> list[Hit]:
        return await self.deps.retriever.search(
            query,
            max_tier=state["max_tier"],
            filters=state.get("filters"),
            limit=self.settings.broad_top_k,
            cards_only=True,
            document_ids=document_ids,
        )

    async def _search_in(self, query: str, state: OwnerState, document_ids: list[str]) -> list[Hit]:
        # No rerank: the reranker's score floor is what dropped them for a vague follow-up.
        return await self.deps.retriever.search(
            query,
            max_tier=state["max_tier"],
            filters=state.get("filters"),
            limit=self.settings.top_k,
            document_ids=document_ids,
        )

    def _follow_up_documents(self, state: OwnerState) -> list[str]:
        if len(state["question"].split()) > self.settings.follow_up_words:
            return []
        for turn in reversed(state["history"]):
            if turn.role == "assistant" and turn_tier(turn) <= state["max_tier"]:
                return list(turn.citations)
        return []

    async def records(self, state: OwnerState) -> dict[str, Any]:
        started = time.perf_counter()
        query = state.get("records_query")
        result = None
        if query is not None and state["max_tier"] >= Tier.PRIVATE:
            result = await self.deps.records.query(query)
        _step(
            "records",
            started,
            kind=query.kind if query else None,
            group_by=query.group_by if query else None,
            total=result.total if result else None,
        )
        return {"records": result}

    async def profile(self, state: OwnerState) -> dict[str, Any]:
        started = time.perf_counter()
        facts = None
        if state["max_tier"] >= Tier.PRIVATE:
            facts = await self.deps.profile.get_profile()
        _step("profile", started, found=facts is not None)
        return {"profile": facts}

    # --- generate --------------------------------------------------------------------------

    async def generate(self, state: OwnerState) -> dict[str, Any]:
        started = time.perf_counter()
        plan = await self._plan(state)
        writer = get_stream_writer()
        gen = Generation()
        parts: list[str] = []
        error: ErrorInfo | None = None
        if plan.fixed is not None:
            parts.append(plan.fixed)
            writer({"token": plan.fixed})
        else:
            error = await self._stream(state, plan, gen, parts, writer)
        # The model found nothing to answer with: citing what it was shown would mislead.
        sources = [] if prompts.says_not_in_vault("".join(parts)) else plan.sources
        if plan.withheld and error is None and plan.fixed is None:
            note = prompts.withheld_note(plan.withheld)
            parts.append(note)
            writer({"token": note})
        _step(
            "generate",
            started,
            route=state["route"],
            tier=plan.tier.label,
            model=gen.model_id,
            fallback=gen.fallback_used,
            sources=len(sources),
            withheld=plan.withheld,
            fixed=plan.fixed is not None,
            error_code=error.code.value if error else None,
            input_tokens=gen.input_tokens,
            output_tokens=gen.output_tokens,
        )
        return {
            "answer": "".join(parts),
            "tier": plan.tier,
            "model_id": gen.model_id,
            "sources": sources,
            "withheld": plan.withheld,
            "incomplete": error is not None,
            "error": error,
            "input_tokens": gen.input_tokens,
            "output_tokens": gen.output_tokens,
        }

    async def _stream(
        self,
        state: OwnerState,
        plan: Plan,
        gen: Generation,
        parts: list[str],
        writer: Callable[[Any], None],
    ) -> ErrorInfo | None:
        try:
            model, prefer = choose(state, self.deps.models, plan.tier)
            async for text in self.deps.generator.stream(
                plan.tier, plan.messages, gen, model=model, prefer=prefer
            ):
                parts.append(text)
                writer({"token": text})
        except MidStreamError as exc:
            gen.model_id = exc.model_id
            return ErrorInfo(
                code=ErrorCode.STREAM_INTERRUPTED, message="The model stopped mid-answer"
            )
        except OrchestratorError as exc:
            logger.warning("generation failed", error_type=type(exc).__name__)
            if parts:
                return ErrorInfo(
                    code=ErrorCode.STREAM_INTERRUPTED, message="The model stopped mid-answer"
                )
            return ErrorInfo(code=ErrorCode.MODEL_UNAVAILABLE, message="No model could answer")
        return None

    async def _plan(self, state: OwnerState) -> Plan:
        route = state["route"]
        if route == "kb_search":
            return await self._plan_kb(state)
        if route == "records":
            result = state.get("records")
            text = result.summary() if result else NEEDS_PRIVATE
            floor = state_floor(state)
            return Plan(tier=max(Tier.PRIVATE, floor) if result else floor, fixed=text)
        if route == "write":
            return Plan(tier=state_floor(state), fixed=prompts.WRITE_REDIRECT)
        if route == "profile":
            facts = state.get("profile")
            if facts is None:
                return Plan(tier=state_floor(state), fixed=NEEDS_PRIVATE)
            system = prompts.GENERATE_PROFILE_V1.format(not_in_vault=prompts.NOT_IN_VAULT)
            tier = max(Tier.PRIVATE, state_floor(state))
            return self._with_messages(state, tier, system, f"Profile:\n{facts.render()}")
        tier = max(
            [
                state_floor(state),
                *(
                    turn_tier(t)
                    for t in visible_history(
                        state["history"], state["max_tier"], self.settings.history_turns
                    )
                ),
            ]
        )
        return self._with_messages(state, tier, prompts.GENERATE_CHAT_V1, None)

    async def _plan_kb(self, state: OwnerState) -> Plan:
        hits = state.get("hits", [])
        floor = await self._conversation_floor(state)
        budget = (
            self.settings.broad_context_tokens
            if state.get("broad")
            else self.settings.context_tokens
        )
        context = build_context(hits, budget_tokens=budget, ceiling=state["max_tier"])
        tier = max(floor, context.tier or floor)
        if tier >= Tier.CONFIDENTIAL and not await self._can_generate(tier, state):
            context = build_context(hits, budget_tokens=budget, ceiling=Tier.PRIVATE)
            tier = max(floor, context.tier or floor)
        if context.empty:
            text = prompts.NOT_IN_VAULT
            if context.withheld:
                text += prompts.withheld_note(context.withheld)
            return Plan(tier=floor, fixed=text, withheld=context.withheld)
        system = prompts.GENERATE_KB_V3.format(not_in_vault=prompts.NOT_IN_VAULT)
        plan = self._with_messages(state, tier, system, f"Context:\n{context.render()}")
        plan.sources, plan.withheld = context.sources, context.withheld
        return plan

    async def _conversation_floor(self, state: OwnerState) -> Tier:
        """The request floor, raised to the conversation's highest visible turn when a model
        may read it: a follow-up to a confidential answer must still see that answer."""
        floor = state_floor(state)
        history = visible_history(state["history"], state["max_tier"], self.settings.history_turns)
        earlier = max((turn_tier(t) for t in history), default=floor)
        if earlier > floor and await self._can_generate(earlier, state):
            return earlier
        return floor

    async def _can_generate(self, tier: Tier, state: OwnerState) -> bool:
        model, prefer = choose(state, self.deps.models, tier)
        try:
            await self.deps.orchestrator.select(
                Role.GENERATOR, tier, model=model, prefer=prefer, privileged=True
            )
        except NoEligibleModelError:
            return False
        except OverrideRejectedError as exc:
            # A pinned cloud model may not read confidential text; other refusals surface later.
            return exc.reason != Rejection.UNDER_TIER
        return True

    def _with_messages(
        self, state: OwnerState, tier: Tier, system: str, context: str | None
    ) -> Plan:
        history = visible_history(state["history"], tier, self.settings.history_turns)
        question = state["query"]
        body = f"{context}\n\nQuestion: {question}" if context else question
        owner = state.get("context")
        if owner is not None:
            system = owner.system(system, tier, self.settings.history_turns)
        messages: list[BaseMessage] = [
            SystemMessage(content=system),
            *history_messages(history),
            HumanMessage(content=body),
        ]
        return Plan(tier=tier, messages=messages)

    # --- finalize --------------------------------------------------------------------------

    async def finalize(self, state: OwnerState) -> dict[str, Any]:
        started = time.perf_counter()
        ref = ConversationRef(id=state["conversation_id"], channel="owner")
        latency = _ms(state["started"])
        tokens = state.get("input_tokens", 0) + state.get("output_tokens", 0)
        conversations = self.deps.conversations
        await conversations.append_turn(ref, Turn(role="user", content=state["question"]))
        assistant = await conversations.append_turn(
            ref,
            Turn(
                role="assistant",
                content=state.get("answer", ""),
                route=state["route"],
                max_tier=_label(state["tier"]),
                model=state.get("model_id"),
                tokens=tokens or None,
                latency_ms=latency,
                citations=[s.document_id for s in state.get("sources", [])],
                incomplete=state.get("incomplete", False),
                withheld=state.get("withheld", 0),
            ),
        )
        _step("finalize", started, seq=assistant.seq, incomplete=assistant.incomplete)
        return {"seq": assistant.seq, "latency_ms": latency}


def _merge(first: list[Hit], second: list[Hit], limit: int) -> list[Hit]:
    """`first` then `second`, one entry per chunk, at most `limit`."""
    seen: set[tuple[str, int]] = set()
    merged: list[Hit] = []
    for hit in [*first, *second]:
        if hit.key not in seen:
            seen.add(hit.key)
            merged.append(hit)
    return merged[:limit]


def _after_route(state: OwnerState) -> str:
    return {"kb_search": "retrieve", "records": "records", "profile": "profile"}.get(
        state["route"], "generate"
    )


def _after_retrieve(state: OwnerState) -> str:
    return "records" if state["route"] == "records" else "generate"


OwnerGraph = CompiledStateGraph[OwnerState, None, OwnerState, OwnerState]


def build_owner_graph(
    deps: OwnerDeps, *, checkpointer: Checkpointer = None, persist: bool = True
) -> OwnerGraph:
    """`checkpointer=False` when another graph runs this one inside a node: it then keeps no
    checkpoints of its own instead of inheriting the caller's. `persist=False` ends after
    `generate`, storing no turns (the super agent stores its own)."""
    nodes = OwnerNodes(deps)
    graph = StateGraph(OwnerState)
    graph.add_node("route", nodes.route)
    graph.add_node("retrieve", nodes.retrieve)
    graph.add_node("records", nodes.records)
    graph.add_node("profile", nodes.profile)
    graph.add_node("generate", nodes.generate)
    if persist:
        graph.add_node("finalize", nodes.finalize)
    graph.add_edge(START, "route")
    graph.add_conditional_edges(
        "route",
        _after_route,
        {
            "retrieve": "retrieve",
            "records": "records",
            "profile": "profile",
            "generate": "generate",
        },
    )
    graph.add_conditional_edges(
        "retrieve", _after_retrieve, {"records": "records", "generate": "generate"}
    )
    graph.add_edge("records", "generate")
    graph.add_edge("profile", "generate")
    if persist:
        graph.add_edge("generate", "finalize")
        graph.add_edge("finalize", END)
    else:
        graph.add_edge("generate", END)
    return graph.compile(checkpointer=checkpointer)


def initial_state(
    *,
    conversation_id: str,
    question: str,
    history: list[Turn],
    max_tier: Tier,
    filters: SearchFilters | None = None,
    model: str | None = None,
    prefer: Prefer | None = None,
    min_tier: Tier | None = None,
    context: OwnerContext | None = None,
    models: ModelChoice | None = None,
) -> OwnerState:
    return OwnerState(
        conversation_id=conversation_id,
        question=question,
        history=history,
        max_tier=max_tier,
        filters=filters,
        model=model,
        prefer=prefer,
        min_tier=min_tier,
        context=context,
        models=models,
        started=time.perf_counter(),
    )
