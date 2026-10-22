"""The super agent: one local-only conversation that plans and delegates to the other agents.

    message ─▶ plan (≤ 4 steps) ─▶ for each step: intake | ask | prep | chat ─▶ compose ─▶ store

- **plan:** one structured call (the super agent's model choice at the conversation's tier;
  confidential history means a local model). Rules take over when no model can plan.
- **intake:** the step runs in a linked intake conversation. A draft that only adds or updates
  data is confirmed at once; a delete, publish, unpublish or tier change waits for the owner's
  yes. That reply goes straight back to intake.
- **ask / prep:** the owner and prep graphs run in-process with this conversation's history,
  storing no turns of their own.
- **compose:** one step's output is the answer; several are joined under short headings, with
  the sources of every step.

Everything stays on the owner's machine. Each step keeps its own agent's guards: the tier
rule, intake's drafts and the prep agent's outward-safety filter. Logs carry counts, agents and
models, never the messages.
"""

from __future__ import annotations

import re
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal, cast

import structlog
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from pydantic import BaseModel, ConfigDict, Field

from app.agents.context import ContextBuilder, OwnerContext
from app.agents.graphs.owner import (
    OwnerDeps,
    OwnerGraph,
    build_owner_graph,
    floor_tier,
    history_messages,
    turn_tier,
    visible_history,
)
from app.agents.graphs.owner import initial_state as owner_state
from app.agents.graphs.prep import PrepGraph, build_prep_graph
from app.agents.graphs.prep import initial_state as prep_state
from app.agents.intake.confirm import classify_rules
from app.agents.models import ModelChoice
from app.agents.prompts import super_agent as prompts
from app.agents.routing import looks_like_write
from app.agents.streaming import sse
from app.api.envelope import ApiModel
from app.core.config import Settings
from app.core.llm import LLMServices
from app.core.resources import Resources
from app.db.documents import ConversationMode, utcnow
from app.exceptions import AppError, ErrorCode
from app.kb.retrieval.context import Source
from app.observability.tracing import tracing
from app.services.conversations import (
    ConversationRef,
    ConversationService,
    TraceStep,
    Turn,
    TurnTrace,
    trace_sources,
)
from app.services.intake import AssistantRequest, IntakeIndex, IntakeService
from app.services.naming import describe, fallback_name, slugify_title, unique_name
from app.services.owner_chat import TurnHook
from app.services.prep import prep_deps
from llm_orchestrator import Orchestrator, OrchestratorError, Role, Tier

logger = structlog.get_logger(__name__)

MODE: ConversationMode = "super"
AgentKind = Literal["intake", "ask", "prep", "chat"]
MAX_STEPS = 4
PLAN_TURN_CHARS = 600
PLAN_HISTORY = 6
# Intake drafts the super agent confirms itself: they only add or update data.
AUTO_CONFIRM = frozenset(
    {
        "add_note",
        "ingest_url",
        "ingest_file",
        "ingest_github",
        "add_record",
        "add_records",
        "update_record",
        "update_profile",
        "remember",
        "approve",
        "release",
        "show_review",
        "source_status",
        "show_memory",
    }
)
_REPORT = re.compile(
    r"\b(i have|i had|i got|i received|i applied|i attended|i cleared|scheduled|invited|"
    r"interview (today|tomorrow|at)|applied (to|for))\b",
    re.I,
)
NOT_SAVED = "Nothing was saved: the intake assistant did not recognise a change to make."
HEADINGS: dict[AgentKind, str] = {
    "intake": "Saved",
    "ask": "From your vault",
    "prep": "Your answer",
    "chat": "",
}


class Step(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent: AgentKind
    instruction: str = Field(min_length=1, max_length=4000)


class SuperPlan(BaseModel):
    steps: list[Step] = Field(min_length=1, max_length=MAX_STEPS)


def rule_plan(message: str) -> SuperPlan:
    """The plan when no model can make one: one step, from wording alone."""
    if looks_like_write(message) or _REPORT.search(message):
        return SuperPlan(steps=[Step(agent="intake", instruction=message)])
    prep_words = re.search(
        r"\b(interview|application|answer (this|the)|tell (me|us) about|how would you|"
        r"why do you want|in \d+ (words|characters))\b",
        message,
        re.I,
    )
    agent: AgentKind = "prep" if prep_words else "ask"
    return SuperPlan(steps=[Step(agent=agent, instruction=message)])


class SuperRequest(ApiModel):
    model_config = ConfigDict(extra="forbid")

    conversation_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")
    message: str = Field(min_length=1, max_length=4000)
    # A model id from config/models.yaml for every step (the tier rule still applies).
    model: str | None = Field(default=None, max_length=100)


Waiting = Literal["draft", "question"] | None


@dataclass
class Told:
    """What one message to the intake conversation produced."""

    conversation_id: str
    text: str = ""
    draft: dict[str, Any] | None = None
    action: str | None = None
    # Intake's turn status: "done" means something was written.
    status: str = ""


@dataclass
class StepResult:
    agent: AgentKind
    text: str = ""
    sources: list[Source] = field(default_factory=list)
    tier: Tier = Tier.PRIVATE
    model: str | None = None
    pending: bool = False
    action: str | None = None
    error: str | None = None
    # The linked intake conversation an intake step ran in.
    intake_conversation: str | None = None
    # An intake step that wrote something (not a draft waiting, not a chat reply).
    saved: bool = False
    waiting: Waiting = None
    # The model's thinking for this step: an owner-only event, and kept in the owner trace.
    thinking: str = ""

    def summary(self) -> dict[str, Any]:
        status = "error" if self.error else "pending" if self.pending else "done"
        return {
            "agent": self.agent,
            "status": status,
            "model": self.model,
            "tier": self.tier.label,
            "action": self.action,
        }


@dataclass(frozen=True)
class Prepared:
    request: SuperRequest
    conversation_id: str
    key_name: str
    history: list[Turn]
    context: OwnerContext | None = None


@dataclass(frozen=True)
class SuperDeps:
    orchestrator: Orchestrator
    conversations: ConversationService
    index: IntakeIndex
    owner_graph: OwnerGraph
    prep_graph: PrepGraph
    intake: IntakeService
    models: ModelChoice
    context: ContextBuilder | None = None
    on_turn: TurnHook | None = None
    today: Callable[[], date] = field(default=lambda: utcnow().date())


def _linked(history: list[Turn]) -> tuple[str | None, Waiting]:
    """The linked intake conversation, and what waits there for the owner: a draft to confirm
    or a question (a missing detail) to answer."""
    for turn in reversed(history):
        if turn.role == "assistant" and turn.guards.get("intake_conversation"):
            waiting = turn.guards.get("intake_waiting")
            if waiting is None and turn.guards.get("intake_pending"):
                waiting = "draft"
            return str(turn.guards["intake_conversation"]), cast(Waiting, waiting)
    return None, None


class SuperAgent:
    def __init__(self, deps: SuperDeps) -> None:
        self.d = deps

    # --- conversation --------------------------------------------------------------------

    async def prepare(self, request: SuperRequest, key_name: str) -> Prepared:
        known = self.d.orchestrator.inventory.models
        if request.model is not None and request.model not in known:
            raise AppError(ErrorCode.INVALID_INPUT, "Unknown model id", details=[request.model])
        if request.conversation_id is None:
            ref = await self.d.conversations.start("owner")
            await self.d.index.set_mode(ref.id, MODE)
            await self._title(ref.id, request.message)
            return Prepared(request, ref.id, key_name, [], await self._context(None, []))
        meta = await self.d.index.get(request.conversation_id)
        history = await self.d.conversations.history(request.conversation_id)
        if (meta is not None and meta.channel != "owner") or (meta is None and not history):
            raise AppError(ErrorCode.NOT_FOUND, "Conversation not found")
        if meta is not None and meta.mode != MODE:
            raise AppError(
                ErrorCode.CONFLICT, f"That is an {meta.mode} conversation; continue it there"
            )
        context = await self._context(request.conversation_id, history)
        return Prepared(request, request.conversation_id, key_name, history, context)

    async def _context(self, cid: str | None, history: list[Turn]) -> OwnerContext | None:
        if self.d.context is None:
            return None
        return await self.d.context.load(cid, history[-1].seq if history else 0)

    async def _title(self, cid: str, message: str) -> None:
        try:
            base = slugify_title(message, fallback_name(cid))
            name = unique_name(base, await self.d.index.names_like(base))
            await self.d.index.set_title(cid, name, describe(message) or None)
        except Exception as exc:
            logger.warning("conversation naming failed", error_type=type(exc).__name__)

    # --- a turn --------------------------------------------------------------------------

    async def events(self, prepared: Prepared) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        cid = prepared.conversation_id
        message = prepared.request.message
        with tracing().run("super-agent", "owner", input=message, session_id=cid) as trace:
            try:
                async for event in self._turn(prepared, trace.output):
                    yield event
            except AppError as exc:
                yield "error", _error(cid, exc.code, exc.message)
            except Exception as exc:
                logger.error("super agent failed", error_type=type(exc).__name__)
                yield "error", _error(cid, ErrorCode.INTERNAL_ERROR, "The super agent failed")

    async def _turn(
        self, prepared: Prepared, output: Callable[[str], None]
    ) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        started = time.perf_counter()
        cid = prepared.conversation_id
        intake_cid, waiting = _linked(prepared.history)
        plan, source = await self._choose_plan(prepared, waiting)
        steps = [s.model_dump() for s in plan.steps]
        yield "meta", {"conversation_id": cid, "mode": MODE, "plan": steps, "plan_source": source}
        results: list[StepResult] = []
        for index, step in enumerate(plan.steps):
            yield "step", {"index": index, "agent": step.agent, "status": "running"}
            result = await self._run(step, prepared, intake_cid, results)
            intake_cid = result.intake_conversation or intake_cid
            results.append(result)
            if result.thinking:
                yield "thinking", {"index": index, "agent": step.agent, "text": result.thinking}
            yield "step", {"index": index, **result.summary()}
        answer = compose(results)
        sources = _sources(results)
        yield "token", {"text": answer}
        yield "sources", {"sources": [s.model_dump(mode="json") for s in sources]}
        seq = await self._store(prepared, answer, results, sources, intake_cid)
        if self.d.on_turn is not None:
            self.d.on_turn(cid, seq)
        tier = max((r.tier for r in results), default=Tier.PRIVATE)
        pending = any(r.pending for r in results)
        yield (
            "done",
            {
                "conversation_id": cid,
                "seq": seq,
                "tier": tier.label,
                "steps": len(results),
                "pending": pending,
                "models": sorted({r.model for r in results if r.model}),
                "latency_ms": round((time.perf_counter() - started) * 1000, 1),
            },
        )
        logger.info(
            "super turn",
            steps=[r.agent for r in results],
            plan_source=source,
            tier=tier.label,
            pending=pending,
            errors=sum(1 for r in results if r.error),
        )
        output(answer)

    async def _choose_plan(self, prepared: Prepared, waiting: Waiting) -> tuple[SuperPlan, str]:
        message = prepared.request.message
        # A yes, no or edit for a waiting draft, or the answer to intake's question (the stage,
        # a date): straight back to intake, which holds the rest of the request.
        if (waiting == "draft" and classify_rules(message) is not None) or waiting == "question":
            return SuperPlan(steps=[Step(agent="intake", instruction=message)]), "reply"
        return await self._plan(prepared)

    async def _plan(self, prepared: Prepared) -> tuple[SuperPlan, str]:
        history = visible_history(prepared.history, Tier.CONFIDENTIAL, PLAN_HISTORY)
        tier = max([Tier.PRIVATE, *(turn_tier(t) for t in history)])
        messages: list[BaseMessage] = [
            SystemMessage(content=prompts.PLAN_V2.format(today=self.d.today().isoformat())),
            *history_messages(history, PLAN_TURN_CHARS),
            HumanMessage(content=f"Owner: {prepared.request.message}"),
        ]
        model, prefer = self.d.models.pick(tier, prepared.request.model)
        try:
            result = await self.d.orchestrator.structured_call(
                Role.GENERATOR,
                tier,
                SuperPlan,
                messages,
                model=model,
                prefer=prefer,
                privileged=True,
            )
            return result.value, f"model:{result.model_id}"
        except OrchestratorError as exc:
            logger.warning("super plan fell back to rules", error_type=type(exc).__name__)
            return rule_plan(prepared.request.message), "rules"

    async def _run(
        self, step: Step, prepared: Prepared, intake_cid: str | None, done: list[StepResult]
    ) -> StepResult:
        try:
            if step.agent == "intake":
                return await self._intake(step, prepared, intake_cid)
            if step.agent == "ask":
                return await self._ask(step, prepared, done)
            if step.agent == "prep":
                return await self._prep(step, prepared, done)
            return StepResult(agent="chat", text=prompts.CHAT_REPLY)
        except AppError as exc:
            return StepResult(agent=step.agent, error=exc.message, text=exc.message)

    async def _ask(self, step: Step, prepared: Prepared, done: list[StepResult]) -> StepResult:
        state = owner_state(
            conversation_id=prepared.conversation_id,
            question=_with_earlier(step.instruction, done),
            history=prepared.history,
            max_tier=Tier.CONFIDENTIAL,
            model=prepared.request.model,
            context=prepared.context,
            models=self.d.models,
        )
        final = cast(dict[str, Any], await self.d.owner_graph.ainvoke(state))
        error = final.get("error")
        return StepResult(
            agent="ask",
            text=str(final.get("answer", "")),
            sources=list(final.get("sources", [])),
            tier=final.get("tier") or floor_tier(Tier.CONFIDENTIAL),
            model=final.get("model_id"),
            error=error.message if error else None,
            thinking=str(final.get("thinking") or ""),
        )

    async def _prep(self, step: Step, prepared: Prepared, done: list[StepResult]) -> StepResult:
        state = prep_state(
            conversation_id=prepared.conversation_id,
            question=_with_earlier(step.instruction, done),
            history=prepared.history,
            model=prepared.request.model,
            context=prepared.context,
            models=self.d.models,
        )
        final = cast(dict[str, Any], await self.d.prep_graph.ainvoke(state))
        text = str(final.get("answer", ""))
        notes = [f"- internal detail removed: {r}" for r in final.get("removed", [])]
        notes += [f"- check before sending: {c}" for c in final.get("checks", [])]
        if notes:
            text = f"{text}\n\n" + "\n".join(notes)
        error = final.get("error")
        return StepResult(
            agent="prep",
            text=text,
            sources=list(final.get("sources", [])),
            tier=final.get("tier") or Tier.PRIVATE,
            model=final.get("model_id"),
            error=error.message if error else None,
            thinking=str(final.get("thinking") or ""),
        )

    async def _intake(self, step: Step, prepared: Prepared, intake_cid: str | None) -> StepResult:
        """Runs the step in the linked intake conversation, confirming additive drafts."""
        told = await self._tell(step.instruction, intake_cid, prepared)
        original = prepared.request.message
        if told.draft is None and told.status == "chat" and step.instruction != original:
            # The planner's rewording missed: intake understands the owner's own sentence best.
            told = await self._tell(original, told.conversation_id, prepared)
        draft = told.draft
        if (
            draft is not None
            and draft.get("kind") == "draft"
            and draft.get("action") in AUTO_CONFIRM
        ):
            told = await self._tell("yes", told.conversation_id, prepared)
            draft = told.draft
        text = told.text
        waiting: Waiting = None
        if draft is not None:
            text = "\n\n".join(p for p in (text, str(draft.get("prompt") or "")) if p)
            waiting = "draft"
        elif told.status == "asked":
            waiting = "question"  # intake needs a detail; the owner's next message answers it
        elif told.status != "done":
            # Intake answered without writing (it did not recognise a change): say so plainly.
            text = "\n\n".join(p for p in (NOT_SAVED, text) if p)
        return StepResult(
            agent="intake",
            text=text,
            tier=Tier.CONFIDENTIAL,
            model="local",
            pending=waiting is not None,
            action=told.action,
            intake_conversation=told.conversation_id,
            saved=waiting is None and told.status == "done",
            waiting=waiting,
        )

    async def _tell(self, message: str, intake_cid: str | None, prepared: Prepared) -> Told:
        ready = await self.d.intake.prepare(
            AssistantRequest(conversation_id=intake_cid, message=message), prepared.key_name
        )
        told = Told(conversation_id=ready.conversation_id)
        parts: list[str] = []
        async for event, data in self.d.intake.events(ready):
            if event == "token":
                parts.append(str(data.get("text", "")))
            elif event == "draft":
                told.draft = data
            elif event == "done":
                told.action, told.status = data.get("action"), str(data.get("status") or "")
                if not data.get("pending"):
                    told.draft = None
            elif event == "error":
                raise AppError(ErrorCode(data["code"]), str(data["message"]))
        told.text = "".join(parts).strip()
        return told

    async def _store(
        self,
        prepared: Prepared,
        answer: str,
        results: list[StepResult],
        sources: list[Source],
        intake_cid: str | None,
    ) -> int:
        ref = ConversationRef(id=prepared.conversation_id, channel="owner")
        tier = max((r.tier for r in results), default=Tier.PRIVATE)
        route = "super:" + ",".join(r.agent for r in results)
        await self.d.conversations.append_turn(
            ref, Turn(role="user", content=prepared.request.message, route=route)
        )
        guards: dict[str, Any] = {"agents": [r.agent for r in results]}
        if intake_cid:
            waiting = next((r.waiting for r in results if r.waiting), None)
            guards |= {
                "intake_conversation": intake_cid,
                "intake_pending": waiting is not None,
                "intake_waiting": waiting,
            }
        stored = await self.d.conversations.append_turn(
            ref,
            Turn(
                role="assistant",
                content=answer,
                route=route,
                max_tier=cast(Any, tier.label),
                model=",".join(sorted({r.model for r in results if r.model})) or None,
                citations=[s.document_id for s in sources],
                incomplete=any(r.error for r in results),
                guards=guards,
                trace=_trace(results, sources, tier),
            ),
        )
        return stored.seq

    async def sse(self, prepared: Prepared) -> AsyncIterator[str]:
        async for event, data in self.events(prepared):
            yield sse(event, data)


def _with_earlier(instruction: str, done: list[StepResult]) -> str:
    """A later step sees what earlier ones saved or found ("add it, then prep me for it")."""
    earlier = [r.text for r in done if r.text and not r.error]
    if not earlier:
        return instruction
    return instruction + "\n\n(Earlier in this request: " + " ".join(earlier)[:1500] + ")"


def compose(results: list[StepResult]) -> str:
    useful = [r for r in results if r.text]
    if len(useful) == 1:
        return useful[0].text
    blocks = []
    for r in useful:
        heading = HEADINGS[r.agent]
        if r.agent == "intake" and not r.saved:
            heading = "Waiting for your answer" if r.pending else "Not saved"
        blocks.append(f"**{heading}**\n{r.text}" if heading else r.text)
    return "\n\n".join(blocks) or "Nothing to do."


def _trace(results: list[StepResult], sources: list[Source], tier: Tier) -> TurnTrace:
    """Each step with its own reasoning, so the console can show it under that step."""
    steps = [TraceStep(**r.summary(), thinking=r.thinking) for r in results]
    return TurnTrace(
        agents=[r.agent for r in results],
        steps=steps,
        models=sorted({r.model for r in results if r.model}),
        tier=tier.label,
        sources=trace_sources(sources),
    ).capped()


def _sources(results: list[StepResult]) -> list[Source]:
    """Every step's sources, one entry per document. Numbers stay as each step cited them."""
    seen: dict[str, Source] = {}
    for r in results:
        for s in r.sources:
            seen.setdefault(s.document_id, s)
    return list(seen.values())


def _error(cid: str, code: ErrorCode, message: str) -> dict[str, Any]:
    return {"code": code.value, "message": message, "conversation_id": cid, "incomplete": True}


def build_super(
    settings: Settings,
    resources: Resources,
    llm: LLMServices,
    owner: OwnerDeps,
    intake: IntakeService,
    *,
    index: IntakeIndex,
    context: ContextBuilder | None = None,
    on_turn: TurnHook | None = None,
) -> SuperAgent:
    models = llm.agents.choice("super")
    return SuperAgent(
        SuperDeps(
            orchestrator=llm.orchestrator,
            conversations=resources.conversations,
            index=index,
            owner_graph=build_owner_graph(owner, persist=False),
            prep_graph=build_prep_graph(prep_deps(settings, resources, llm, owner), persist=False),
            intake=intake,
            models=models,
            context=context,
            on_turn=on_turn,
        )
    )
