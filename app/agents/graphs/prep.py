"""The prep graph: plan → retrieve → compose → finalize.

Answers interview and application questions in the owner's first-person voice, from the vault.
`plan` detects the question type and writes a few sub-queries; `retrieve` gathers document
cards and chunks for all of them (up to confidential); `compose` writes the answer at the
context's tier, enforces any length limit, then runs the outward-safety filter before a single
token leaves. Answers are streamed only after filtering, as one piece.

Logs carry counts, tiers, the type and the model; never the question or the answer.
"""

from __future__ import annotations

import re
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, TypedDict

import structlog
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.agents.context import OwnerContext
from app.agents.generation import AnswerGenerator, Generation, MidStreamError
from app.agents.graphs.owner import (
    ErrorInfo,
    Searcher,
    choose,
    floor_tier,
    history_messages,
    turn_tier,
    visible_history,
)
from app.agents.models import AUTO_CHOICE, ModelChoice
from app.agents.prep.filter import OutwardFilter
from app.agents.prompts import prep as prompts
from app.agents.prompts.prep import PrepType
from app.exceptions import ErrorCode
from app.kb.retrieval.context import Source, build_context
from app.kb.retrieval.rerank import Reranker
from app.kb.retrieval.retriever import Hit
from app.services.conversations import ConversationRef, ConversationService, Turn
from llm_orchestrator import (
    NoEligibleModelError,
    Orchestrator,
    OrchestratorError,
    OverrideRejectedError,
    Prefer,
    Profiler,
    Rejection,
    Role,
    Tier,
)

logger = structlog.get_logger(__name__)

Revise = Literal["shorter", "longer", "variant"]

_TYPE_RULES: list[tuple[PrepType, re.Pattern[str]]] = [
    (
        "pitch",
        re.compile(
            r"\b(tell (me|us) (a bit )?about yourself|introduce yourself|walk (me|us) through "
            r"your (resume|cv|background|career))\b",
            re.I,
        ),
    ),
    (
        "behavioural",
        re.compile(
            r"\b(tell (me|us) about a time|describe a (time|situation)|give (me |us )?an example"
            r"|most challenging|biggest (challenge|failure|mistake)|conflict|disagree|proud of"
            r"|a time when|you led|you handled)\b",
            re.I,
        ),
    ),
    (
        "scenario",
        re.compile(
            r"\b(how would you|what would you do|what('s| is) your (step[- ]by[- ]step )?(process|"
            r"approach)|step[- ]by[- ]step|walk (me|us) through how|if (your|the|a)\b.*\b("
            r"increase|increases|drops?|fails?|spikes?|slows?)|design a|diagnos|troubleshoot)",
            re.I,
        ),
    ),
    ("project", re.compile(r"\b(project|built|architecture of|deep dive)\b", re.I)),
]
_APPLICATION = re.compile(
    r"\b(why do you want|why (are you|should we)|cover letter|in (\d+) (words|characters)|"
    r"application|motivation|describe yourself in)\b",
    re.I,
)
_TYPE_QUERIES: dict[PrepType, tuple[str, ...]] = {
    "behavioural": (
        "most challenging technical project I led: the problem, my role and the impact",
        "achievements with measurable results and metrics",
    ),
    "scenario": (
        "performance, latency, debugging and production incidents I worked on",
        "monitoring, observability and reliability work",
    ),
    "application": ("my projects, achievements and skills", "my experience and roles"),
    "pitch": ("my experience summary, current role and skills", "my strongest projects"),
    "project": ("architecture, technologies and results of my projects",),
}
_NUMBER = re.compile(r"(?<![\w.])\d+(?:[.,]\d+)?\s?(?:%|x|ms|s|k|m|tb|gb|mb)?(?!\w)", re.I)
# Redaction placeholders (app.kb.ingestion.redaction) that reached an answer.
_PLACEHOLDER = re.compile(
    r"\[(PERSON|EMAIL|PHONE|CARD|IBAN|IP|PAN|AADHAAR|UAN|IFSC|BANK_ACCOUNT|"
    r"PASSPORT|ROLL_NUMBER|DOB)\]"
)
_LIST_MARKER = re.compile(r"(?m)^\s*\d+[.)]\s")


def detect_type(question: str, *, limited: bool = False) -> PrepType:
    """The question type from wording alone; a length limit means an application field."""
    if limited or _APPLICATION.search(question):
        return "application"
    for kind, pattern in _TYPE_RULES:
        if pattern.search(question):
            return kind
    return "behavioural"


def sub_queries(question: str, kind: PrepType) -> list[str]:
    return [question, *_TYPE_QUERIES[kind]]


def word_count(text: str) -> int:
    return len(re.findall(r"\S+", text))


def fit_limit(text: str, words: int | None, chars: int | None) -> str:
    """Cut at the last whole sentence inside the limit (the model is asked first)."""
    if words and word_count(text) > words:
        text = " ".join(re.findall(r"\S+", text)[:words])
    if chars and len(text) > chars:
        text = text[:chars]
    if (words or chars) and not text.rstrip().endswith((".", "!", "?")):
        cut = max(text.rfind(". "), text.rfind("! "), text.rfind("? "))
        text = text[: cut + 1] if cut > len(text) // 2 else text.rstrip()
    return text.strip()


def unsourced_numbers(answer: str, context: str) -> list[str]:
    """Numbers in the answer that the context never states. List markers and small counts
    ("two services", "3 steps") are left alone."""
    seen = {m.group(0).replace(" ", "").lower() for m in _NUMBER.finditer(context)}
    found: list[str] = []
    for match in _NUMBER.finditer(_LIST_MARKER.sub("", answer)):
        value = match.group(0).replace(" ", "").lower()
        small = value.isdigit() and int(value) <= 10
        if not small and value not in seen and value not in found:
            found.append(value)
    return found


@dataclass(frozen=True)
class PrepSettings:
    top_k: int = 8
    candidates: int = 24
    cards: int = 8
    context_tokens: int = 6000
    history_turns: int = 6


@dataclass(frozen=True)
class PrepDeps:
    orchestrator: Orchestrator
    retriever: Searcher
    reranker: Reranker
    conversations: ConversationService
    generator: AnswerGenerator
    filter: OutwardFilter
    settings: PrepSettings = field(default_factory=PrepSettings)
    models: ModelChoice = AUTO_CHOICE
    profiler: Profiler | None = None


class PrepState(TypedDict, total=False):
    # input
    conversation_id: str
    question: str
    history: list[Turn]
    max_tier: Tier
    kind_hint: PrepType | None
    words: int | None
    chars: int | None
    revise: Revise | None
    model: str | None
    prefer: Prefer | None
    models: ModelChoice | None
    context: OwnerContext | None
    started: float
    # plan
    kind: PrepType
    target: str
    queries: list[str]
    # gathered
    hits: list[Hit]
    # composed
    answer: str
    tier: Tier
    model_id: str | None
    sources: list[Source]
    withheld: int
    removed: list[str]
    checks: list[str]
    error: ErrorInfo | None
    input_tokens: int
    output_tokens: int
    thinking: str
    # finalized
    seq: int
    latency_ms: float


def _ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 1)


def _step(step: str, started: float, **fields: Any) -> None:
    logger.info("prep step", step=step, latency_ms=_ms(started), **fields)


def _previous_question(history: Sequence[Turn]) -> str | None:
    return next((t.content for t in reversed(history) if t.role == "user" and t.route), None)


class PrepNodes:
    def __init__(self, deps: PrepDeps) -> None:
        self.deps = deps
        self.settings = deps.settings

    async def plan(self, state: PrepState) -> dict[str, Any]:
        started = time.perf_counter()
        target = state["question"]
        if state.get("revise"):
            target = _previous_question(state["history"]) or target
        limited = bool(state.get("words") or state.get("chars"))
        kind = state.get("kind_hint") or detect_type(target, limited=limited)
        queries = sub_queries(target, kind)
        _step("plan", started, kind=kind, queries=len(queries), revise=state.get("revise"))
        return {"kind": kind, "target": target, "queries": queries}

    async def retrieve(self, state: PrepState) -> dict[str, Any]:
        started = time.perf_counter()
        search = self.deps.retriever.search
        cards: list[Hit] = []
        candidates: list[Hit] = []
        for query in state["queries"]:
            cards += await search(
                query,
                max_tier=state["max_tier"],
                filters=None,
                limit=self.settings.cards,
                cards_only=True,
            )
            candidates += await search(
                query, max_tier=state["max_tier"], filters=None, limit=self.settings.candidates
            )
        ranked = await self.deps.reranker.rerank(
            state["target"], _unique(candidates), self.settings.top_k
        )
        hits = _unique([*_unique(cards)[: self.settings.cards], *ranked])
        _step(
            "retrieve",
            started,
            cards=sum(h.card for h in hits),
            hits=len(hits),
            tiers=sorted({h.tier.label for h in hits}),
        )
        return {"hits": hits}

    async def compose(self, state: PrepState) -> dict[str, Any]:
        started = time.perf_counter()
        hits = state.get("hits", [])
        floor = await self._floor(state)
        budget = self.settings.context_tokens
        context = build_context(hits, budget_tokens=budget, ceiling=state["max_tier"])
        tier = max(floor, context.tier or floor)
        if tier >= Tier.CONFIDENTIAL and not await self._can_generate(tier, state):
            context = build_context(hits, budget_tokens=budget, ceiling=Tier.PRIVATE)
            tier = max(floor, context.tier or floor)
        kind = state["kind"]
        words, chars = state.get("words"), state.get("chars")
        system = prompts.system_prompt(kind, prompts.limit_text(words, chars))
        owner = state.get("context")
        if owner is not None:
            system = owner.system(system, tier, self.settings.history_turns)
        rendered = context.render() if not context.empty else "(nothing relevant in the vault)"
        messages = self._messages(state, tier, system, rendered)
        gen = Generation()
        draft, error = await self._generate(state, tier, messages, gen)
        if error is None and draft and _over(draft, words, chars):
            limit = f"{words} words" if words else f"{chars} characters"
            shorter = [*messages, HumanMessage(content=f"{draft}\n\nShorten this to {limit}.")]
            draft = (await self._generate(state, tier, shorter, gen))[0] or draft
        draft = fit_limit(draft, words, chars)
        filtered = self.deps.filter.apply(draft)
        checks = self._checks(filtered.text, rendered, kind, words, chars)
        get_stream_writer()({"token": filtered.text})
        _step(
            "compose",
            started,
            kind=kind,
            tier=tier.label,
            model=gen.model_id,
            sources=len(context.sources),
            withheld=context.withheld,
            removed=len(filtered.removed),
            checks=len(checks),
            words=word_count(filtered.text),
            error_code=error.code.value if error else None,
        )
        return {
            "answer": filtered.text,
            "tier": tier,
            "model_id": gen.model_id,
            "sources": context.sources,
            "withheld": context.withheld,
            "removed": filtered.summary(),
            "checks": checks,
            "error": error,
            "input_tokens": gen.input_tokens,
            "output_tokens": gen.output_tokens,
            "thinking": "".join(gen.thinking),
        }

    def _messages(
        self, state: PrepState, tier: Tier, system: str, context: str
    ) -> list[BaseMessage]:
        history = visible_history(state["history"], tier, self.settings.history_turns)
        revise = state.get("revise")
        ask = prompts.REVISE_V1[revise] if revise else f"Question: {state['target']}"
        return [
            SystemMessage(content=system),
            *history_messages(history),
            HumanMessage(content=f"Context:\n{context}\n\n{ask}"),
        ]

    async def _generate(
        self, state: PrepState, tier: Tier, messages: list[BaseMessage], gen: Generation
    ) -> tuple[str, ErrorInfo | None]:
        parts: list[str] = []
        model, prefer = choose(state, self.deps.models, tier)
        models: ModelChoice = state.get("models") or self.deps.models
        need = await models.need_for(state["target"], tier, self.deps.profiler)
        try:
            async for text in self.deps.generator.stream(
                tier,
                messages,
                gen,
                model=model,
                prefer=prefer,
                need=need,
                on_thinking=lambda t: get_stream_writer()({"thinking": t}),
            ):
                parts.append(text)
        except MidStreamError as exc:
            gen.model_id = exc.model_id
            return "".join(parts), ErrorInfo(
                code=ErrorCode.STREAM_INTERRUPTED, message="The model stopped mid-answer"
            )
        except OrchestratorError as exc:
            logger.warning("prep generation failed", error_type=type(exc).__name__)
            return "", ErrorInfo(code=ErrorCode.MODEL_UNAVAILABLE, message="No model could answer")
        return "".join(parts), None

    @staticmethod
    def _checks(
        answer: str, context: str, kind: PrepType, words: int | None, chars: int | None
    ) -> list[str]:
        checks: list[str] = []
        missing = answer.count(prompts.MISSING_METRIC)
        if missing:
            checks.append(
                f"Fill {missing} {prompts.MISSING_METRIC} placeholder(s) with real figures"
            )
        redacted = sorted(set(_PLACEHOLDER.findall(answer)))
        if redacted:
            checks.append(
                "Replace redacted placeholder(s) "
                + ", ".join(f"[{p}]" for p in redacted)
                + " with the real names before sending"
            )
        numbers = unsourced_numbers(answer, context)
        if numbers:
            checks.append("Not in your sources, verify: " + ", ".join(numbers[:8]))
        if words or chars or kind == "application":
            limit = f" (limit {words} words)" if words else ""
            limit = f" (limit {chars} characters)" if chars and not words else limit
            checks.append(f"Length: {word_count(answer)} words, {len(answer)} characters{limit}")
        return checks

    async def _floor(self, state: PrepState) -> Tier:
        floor = floor_tier(state["max_tier"])
        history = visible_history(state["history"], state["max_tier"], self.settings.history_turns)
        earlier = max((turn_tier(t) for t in history), default=floor)
        if earlier > floor and await self._can_generate(earlier, state):
            return earlier
        return floor

    async def _can_generate(self, tier: Tier, state: PrepState) -> bool:
        model, prefer = choose(state, self.deps.models, tier)
        try:
            await self.deps.orchestrator.select(
                Role.GENERATOR, tier, model=model, prefer=prefer, privileged=True
            )
        except NoEligibleModelError:
            return False
        except OverrideRejectedError as exc:
            return exc.reason != Rejection.UNDER_TIER
        return True

    async def finalize(self, state: PrepState) -> dict[str, Any]:
        started = time.perf_counter()
        ref = ConversationRef(id=state["conversation_id"], channel="owner")
        latency = _ms(state["started"])
        tokens = state.get("input_tokens", 0) + state.get("output_tokens", 0)
        route = f"prep:{state['kind']}"
        conversations = self.deps.conversations
        question = state["question"] if not state.get("revise") else f"/{state['revise']}"
        await conversations.append_turn(ref, Turn(role="user", content=question, route=route))
        assistant = await conversations.append_turn(
            ref,
            Turn(
                role="assistant",
                content=state.get("answer", ""),
                route=route,
                max_tier=state["tier"].label,
                model=state.get("model_id"),
                tokens=tokens or None,
                latency_ms=latency,
                citations=[s.document_id for s in state.get("sources", [])],
                incomplete=state.get("error") is not None,
                withheld=state.get("withheld", 0),
            ),
        )
        _step("finalize", started, seq=assistant.seq)
        return {"seq": assistant.seq, "latency_ms": latency}


def _unique(hits: Sequence[Hit]) -> list[Hit]:
    seen: set[tuple[str, int]] = set()
    out: list[Hit] = []
    for hit in hits:
        if hit.key not in seen:
            seen.add(hit.key)
            out.append(hit)
    return out


def _over(text: str, words: int | None, chars: int | None) -> bool:
    return bool((words and word_count(text) > words) or (chars and len(text) > chars))


PrepGraph = CompiledStateGraph[PrepState, None, PrepState, PrepState]


def build_prep_graph(deps: PrepDeps, *, persist: bool = True) -> PrepGraph:
    """`persist=False` ends after `compose`, storing no turns (the super agent stores its own)."""
    nodes = PrepNodes(deps)
    graph = StateGraph(PrepState)
    graph.add_node("plan", nodes.plan)
    graph.add_node("retrieve", nodes.retrieve)
    graph.add_node("compose", nodes.compose)
    graph.add_edge(START, "plan")
    graph.add_edge("plan", "retrieve")
    graph.add_edge("retrieve", "compose")
    if persist:
        graph.add_node("finalize", nodes.finalize)
        graph.add_edge("compose", "finalize")
        graph.add_edge("finalize", END)
    else:
        graph.add_edge("compose", END)
    return graph.compile()


def initial_state(
    *,
    conversation_id: str,
    question: str,
    history: list[Turn],
    max_tier: Tier = Tier.CONFIDENTIAL,
    kind_hint: PrepType | None = None,
    words: int | None = None,
    chars: int | None = None,
    revise: Revise | None = None,
    model: str | None = None,
    prefer: Prefer | None = None,
    context: OwnerContext | None = None,
    models: ModelChoice | None = None,
) -> PrepState:
    return PrepState(
        conversation_id=conversation_id,
        question=question,
        history=history,
        max_tier=max_tier,
        kind_hint=kind_hint,
        words=words,
        chars=chars,
        revise=revise,
        model=model,
        prefer=prefer,
        models=models,
        context=context,
        started=time.perf_counter(),
    )
