"""The intake graph: gather → (ask | confirm → second confirm → execute → suggest) | answer.

Two agents in one graph, run in sequence. The intake agent (`gather`) has the local model
extract an action and field values; code merges them into the draft and decides what is
missing. A complete write draft pauses at `confirm` (LangGraph `interrupt`); the owner's next
message resumes it as yes, no, an edit or something new. Deletes pause again at `second` for
the title or company to be retyped. The executor calls `AdminOps` and reports, then one
deterministic suggestion may become a new pending draft. `answer` hands a question to the
owner graph.

Every model call runs at tier confidential, so only local models are eligible. Log lines carry
step names, action names, field names and counts, never the owner's text or field values.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, TypedDict, cast
from zoneinfo import ZoneInfo

import structlog
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Checkpointer, Command, interrupt

from app.agents.context import OwnerContext
from app.agents.graphs.owner import OwnerGraph, initial_state
from app.agents.intake.actions import (
    ACTIONS,
    ActionSpec,
    RunContext,
    catalog_text,
    json_safe,
    recent_items,
    resolve_target,
)
from app.agents.intake.confirm import Decision, classify_reply, retype_matches
from app.agents.intake.drafts import (
    IntakeDraft,
    check,
    github_repos,
    ground,
    local_today,
    merge,
    normalise,
    only_github_repo_urls,
)
from app.agents.intake.suggest import suggest
from app.agents.prompts import intake as prompts
from app.db.documents import utcnow
from app.exceptions import AppError
from app.services.admin_ops import AdminOps, intake_actor
from app.services.conversations import ConversationService
from app.services.memory import MemoryService
from app.services.naming import describe
from llm_orchestrator import AllModelsFailedError, Orchestrator, OrchestratorError, Role, Tier

logger = structlog.get_logger(__name__)

INTAKE_TIER = Tier.CONFIDENTIAL
HISTORY_TURNS = 6
HISTORY_CHARS = 600
RECENT_KEEP = 10
TITLE_CHARS = 80
EXTRACT_ATTEMPTS = 2
_TYPED_ID = re.compile(r"\b[0-9a-f]{24}\b")
_STATUS = re.compile(r"\bstatus\b|\bis it done\b|\bfinished\b", re.I)
_REMEMBER = re.compile(r"^\s*(please\s+)?remember\b(?!\s+(when|what|which|how|if)\b)", re.I)
_FORGET = re.compile(r"^\s*(please\s+)?(forget|stop remembering)\b", re.I)
_SHOW_MEMORY = re.compile(
    r"\b(show|list|what are)\s+(me\s+)?(my\s+|your\s+)?(memories|memory|preferences)\b|"
    r"\bwhat do you remember\b",
    re.I,
)
_LAST_SOURCE = re.compile(
    r"\b(last|latest|previous) (upload|source|ingest\w*|sync|file|link)\b", re.I
)


def rule_action(message: str, action: str | None) -> str | None:
    """Keyword rules a small model gets wrong: a status question about a source id, asking to
    remember, forget or show memories, and GitHub repository links, which are synced as repos
    rather than fetched as web pages."""
    if _STATUS.search(message) and (_TYPED_ID.search(message) or _LAST_SOURCE.search(message)):
        return "source_status"
    if _SHOW_MEMORY.search(message):
        return "show_memory"
    if _REMEMBER.search(message):
        return "remember"
    if _FORGET.search(message):
        return "forget"
    if action in (None, "ingest_url") and only_github_repo_urls(message):
        return "ingest_github"
    return action


Status = Literal["asked", "draft", "done", "cancelled", "answer", "chat", "error"]

REASK = "Reply yes to save it, no to discard it, or tell me what to change."
DISCARDED = "Discarded; nothing was saved."
CANCELLED_DELETE = "That didn't match, so nothing was deleted."
NOTHING_PENDING = "Nothing is waiting for a yes. Tell me what to add or change."
CHAT_DEFAULT = (
    "I can add notes, records, web pages, inbox files and GitHub repos, update your profile, "
    "and approve, publish or delete documents. What would you like to do?"
)


class IntakeState(TypedDict, total=False):
    # per message (set by the service on every run)
    conversation_id: str
    message: str
    key_name: str
    history: list[dict[str, str]]
    # OwnerContext as plain data: the owner's preferences and the conversation summary.
    context: dict[str, Any] | None
    # the draft being gathered (kept across turns by the checkpointer)
    action: str | None
    payload: dict[str, Any]
    missing: list[str]
    # awaiting a yes: {kind, action, payload, summary, prompt, confirm_text}
    pending: dict[str, Any] | None
    reask: bool
    recent: list[dict[str, Any]]
    # this run's result
    reply: str
    status: Status
    outcome: dict[str, Any] | None
    question: str | None


@dataclass(frozen=True)
class IntakeDeps:
    orchestrator: Orchestrator
    ops: AdminOps
    inbox: Path
    timezone: ZoneInfo
    wait_seconds: float = 30.0
    owner_graph: OwnerGraph | None = None
    conversations: ConversationService | None = None
    memory: MemoryService | None = None
    now: Callable[[], datetime] = field(default=utcnow)
    sleep: Callable[[float], Awaitable[None]] = field(default=asyncio.sleep)


def _ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 1)


def _step(step: str, started: float, **fields: Any) -> None:
    logger.info("intake step", step=step, latency_ms=_ms(started), **fields)


def _owner_context(state: IntakeState) -> OwnerContext | None:
    raw = state.get("context")
    return OwnerContext.model_validate(raw) if raw else None


_REMEMBER_PREFIX = re.compile(r"^\s*(please\s+)?remember\s*(that|this|:)?\s*", re.I)
_ALIAS = re.compile(r"\bwhen i say\b|\bi mean\b|\bstands for\b|=|\bmeans\b", re.I)
_RULE = re.compile(r"\b(always|never|every time|from now on|don't|do not)\b", re.I)


def _remember_defaults(payload: dict[str, Any], message: str) -> dict[str, Any]:
    """The owner's own words when the model left the text out, and a kind from their wording
    when it left that out too."""
    text = str(payload.get("text") or "").strip() or _REMEMBER_PREFIX.sub("", message).strip()
    kind = payload.get("kind")
    if kind not in ("instruction", "preference", "alias"):
        kind = "alias" if _ALIAS.search(text) else "instruction" if _RULE.search(text) else None
    return {**payload, "text": text[:1].upper() + text[1:], "kind": kind or "preference"}


def reset_values() -> dict[str, Any]:
    """Per-run outputs, cleared at the start of every run."""
    return {"reply": "", "status": "chat", "outcome": None, "question": None, "reask": False}


def _cleared() -> dict[str, Any]:
    return {"action": None, "payload": {}, "missing": [], "pending": None}


def _for_owner(hint: str) -> str:
    """Field hints are written for the extraction model ("the owner's words"); say "your"."""
    return hint.replace("the owner's", "your").replace("the owner", "you")


def _field_line(spec: ActionSpec, name: str) -> str:
    hint = spec.fields.get(name, "")
    return f"- {name}: {_for_owner(hint)}" if hint else f"- {name}"


def _missing_text(spec: ActionSpec, missing: list[str], invalid: dict[str, str]) -> str:
    """What is wrong or still needed, one field per line with its type or allowed values."""
    parts = []
    if invalid:
        lines = [f"- {name}: {why}" for name, why in invalid.items()]
        parts.append("These values didn't fit:\n" + "\n".join(lines))
    if missing:
        lines = [_field_line(spec, name) for name in missing]
        parts.append(f"To {spec.label.lower()}, I still need:\n" + "\n".join(lines))
    return "\n".join(parts)


def _sentence(text: str) -> str:
    text = text.strip()
    return text if text.endswith((".", "?", "!")) else f"{text}."


def pending_prompt(pending: dict[str, Any]) -> str:
    if pending["kind"] == "retype":
        return (
            f"This can't be undone. To delete it, type “{pending['confirm_text']}” exactly; "
            "anything else cancels."
        )
    if pending["kind"] == "suggestion":
        return f"{pending['summary']} (yes / no)"
    verb = "Delete this?" if pending.get("confirm_text") else "Save this?"
    return f"{pending['summary']}\n\n{verb} (yes / no / tell me what to change)"


class IntakeNodes:
    def __init__(self, deps: IntakeDeps) -> None:
        self.deps = deps

    # --- gather (the intake agent) ---------------------------------------------------------

    async def gather(self, state: IntakeState) -> Command[str]:
        started = time.perf_counter()
        current = state.get("action")
        try:
            extracted, model_id = await self._extract(state)
        except OrchestratorError as exc:
            _step("gather", started, outcome="model_failed", error_type=type(exc).__name__)
            return Command(
                goto=END,
                update={
                    "reply": "The local model couldn't read that. Please try again "
                    "(is Ollama running? make ollama ARGS=status).",
                    "status": "error",
                },
            )
        action = rule_action(state["message"], extracted.action or current)
        if action is None:
            _step("gather", started, action=None, model=model_id)
            return Command(
                goto=END, update={"reply": extracted.reply or CHAT_DEFAULT, "status": "chat"}
            )
        if action == "answer":
            question = str(extracted.payload.get("question") or state["message"])
            _step("gather", started, action=action, model=model_id)
            return Command(goto="answer", update={"question": question, "status": "answer"})
        spec = ACTIONS[action]
        switched = current is not None and action != current
        base = {} if switched or current is None else dict(state.get("payload") or {})
        given, dropped = ground(
            spec, extracted.payload, state["message"], base, self.deps.timezone, self.deps.now()
        )
        payload = self._defaults(spec, merge(base, given), state["message"])
        return await self._settle(spec, payload, state, started, model_id, switched, dropped)

    async def _settle(
        self,
        spec: ActionSpec,
        payload: dict[str, Any],
        state: IntakeState,
        started: float,
        model_id: str | None,
        switched: bool,
        dropped: list[str],
    ) -> Command[str]:
        deps = self.deps
        payload, invalid = normalise(spec, payload, deps.timezone, deps.now())
        resolution = await resolve_target(
            spec, payload, deps.ops, state.get("recent") or [], deps.inbox, deps.memory
        )
        result = check(spec, resolution.payload)
        invalid |= result.invalid
        _step(
            "gather",
            started,
            action=spec.name,
            model=model_id,
            switched=switched,
            ungrounded=sorted(dropped),
            missing=result.missing,
            invalid=sorted(invalid),
            unresolved=resolution.problem is not None,
        )
        update: dict[str, Any] = {
            "action": spec.name,
            "payload": result.payload,
            "missing": result.missing,
            "pending": None,
        }
        if resolution.problem or result.missing or invalid:
            parts = [resolution.problem or "", _missing_text(spec, result.missing, invalid)]
            text = " ".join(_sentence(p) for p in parts if p)
            return Command(goto=END, update=update | {"reply": text, "status": "asked"})
        if not spec.write:
            return Command(goto="execute", update=update)
        pending = {
            "kind": "draft",
            "action": spec.name,
            "payload": result.payload,
            "summary": (
                f"{resolution.note}\n\n{spec.summary(result.payload)}"
                if resolution.note
                else spec.summary(result.payload)
            ),
            "confirm_text": spec.confirm_text(result.payload),
        }
        return Command(goto="confirm", update=update | {"pending": pending, "status": "draft"})

    @staticmethod
    def _defaults(spec: ActionSpec, payload: dict[str, Any], message: str) -> dict[str, Any]:
        """A note's text is the owner's own message when the model left it out, and an id the
        owner typed names the target, whatever the model made of it."""
        typed_id = _TYPED_ID.search(message)
        if spec.target is not None and typed_id is not None:
            payload = {k: v for k, v in payload.items() if not k.startswith(f"{spec.target}_")} | {
                spec.target: typed_id.group(0)
            }
        if spec.name == "ingest_github" and (repos := github_repos(message)):
            # Exact names from the owner's text beat whatever a small model made of them.
            payload = {**payload, "repos": repos}
        if spec.name == "source_status" and typed_id is not None:
            payload = {**payload, "source_id": typed_id.group(0)}
        if spec.name == "remember":
            payload = _remember_defaults(payload, message)
        if spec.name == "add_note":
            if not payload.get("body"):
                payload = {**payload, "body": message}
            if not payload.get("title"):
                payload = {**payload, "title": describe(payload["body"])[:TITLE_CHARS]}
        return payload

    async def _extract(self, state: IntakeState) -> tuple[IntakeDraft, str | None]:
        deps = self.deps
        today = local_today(deps.timezone, deps.now())
        draft = ""
        if state.get("action"):
            draft = prompts.DRAFT_CONTEXT.format(
                action=state["action"],
                fields=json.dumps(json_safe(state.get("payload") or {}), ensure_ascii=False),
                missing=", ".join(state.get("missing") or []) or "nothing",
            )
        recent = ""
        if state.get("recent"):
            items = [
                f"{r['kind']} {r.get('title') or r.get('company') or r.get('source_id')}"
                for r in (state.get("recent") or [])[-5:]
            ]
            recent = prompts.RECENT_CONTEXT.format(items="; ".join(items))
        system = prompts.EXTRACT_V1.format(
            today=f"{today.isoformat()} ({today.strftime('%A')})",
            catalog=catalog_text(),
            draft=draft,
            recent=recent,
        )
        owner = _owner_context(state)
        if owner is not None:
            system = owner.system(system, INTAKE_TIER, HISTORY_TURNS)
        messages: list[BaseMessage] = [SystemMessage(content=system)]
        for turn in (state.get("history") or [])[-HISTORY_TURNS:]:
            text = turn["content"][:HISTORY_CHARS]
            messages.append(
                HumanMessage(content=text) if turn["role"] == "user" else AIMessage(content=text)
            )
        messages.append(HumanMessage(content=state["message"]))
        # A small local model sometimes stops mid-JSON; one more try is cheap.
        for attempt in range(EXTRACT_ATTEMPTS):
            try:
                result = await deps.orchestrator.structured_call(
                    Role.ROUTER, INTAKE_TIER, IntakeDraft, messages, privileged=True
                )
                return result.value, result.model_id
            except AllModelsFailedError:
                if attempt == EXTRACT_ATTEMPTS - 1:
                    raise
        raise AssertionError("unreachable")  # pragma: no cover

    # --- confirm ---------------------------------------------------------------------------

    async def confirm(self, state: IntakeState) -> Command[str]:
        pending = state.get("pending")
        if not pending:
            return Command(goto=END, update={"reply": NOTHING_PENDING, "status": "chat"})
        # Nothing above this line may have side effects: the node re-runs on resume.
        reply = str(interrupt(self._interrupt_value(pending, state.get("reask", False))))
        started = time.perf_counter()
        decision = await classify_reply(
            reply, pending["summary"], self.deps.orchestrator, INTAKE_TIER
        )
        _step("confirm", started, kind=pending["kind"], decision=decision)
        return self._after_reply(pending, decision, reply)

    @staticmethod
    def _interrupt_value(pending: dict[str, Any], reask: bool) -> dict[str, Any]:
        return {
            "kind": pending["kind"],
            "action": pending["action"],
            "summary": pending["summary"],
            "payload": json_safe(pending["payload"]),
            "prompt": f"{REASK}\n\n{pending_prompt(pending)}" if reask else pending_prompt(pending),
            "reask": reask,
        }

    def _after_reply(
        self, pending: dict[str, Any], decision: Decision | None, reply: str
    ) -> Command[str]:
        if decision is None:
            return Command(goto="confirm", update={"reask": True})
        if decision == "yes":
            spec = ACTIONS[pending["action"]]
            if spec.destructive:
                retype = {**pending, "kind": "retype"}
                return Command(goto="second", update={"pending": retype, "reask": False})
            return Command(goto="execute", update={"reask": False})
        if decision == "no":
            return Command(
                goto=END, update=_cleared() | {"reply": DISCARDED, "status": "cancelled"}
            )
        # An edit changes the draft; anything else (or any reply to a suggestion) starts over.
        keep = decision == "edit" and pending["kind"] == "draft"
        update: dict[str, Any] = {"message": reply, "pending": None, "reask": False}
        if not keep:
            update |= _cleared()
        return Command(goto="gather", update=update)

    async def second(self, state: IntakeState) -> Command[str]:
        pending = state.get("pending") or {}
        typed = str(interrupt(self._interrupt_value(pending, False)))
        matched = retype_matches(str(pending.get("confirm_text") or ""), typed)
        _step("second_confirm", time.perf_counter(), action=pending.get("action"), matched=matched)
        if not matched:
            return Command(
                goto=END, update=_cleared() | {"reply": CANCELLED_DELETE, "status": "cancelled"}
            )
        return Command(goto="execute")

    # --- execute (the executor) ------------------------------------------------------------

    async def execute(self, state: IntakeState) -> Command[str]:
        started = time.perf_counter()
        spec = ACTIONS[str(state.get("action"))]
        payload = dict(state.get("payload") or {})
        ctx = RunContext(
            ops=self.deps.ops,
            actor=intake_actor(state.get("key_name") or "unknown"),
            inbox=self.deps.inbox,
            wait_seconds=self.deps.wait_seconds,
            sleep=self.deps.sleep,
            memory=self.deps.memory,
        )
        assert spec.run is not None
        try:
            outcome = await spec.run(ctx, payload)
        except AppError as exc:
            _step("execute", started, action=spec.name, ok=False, error_code=exc.code.value)
            return Command(
                goto=END,
                update=_cleared()
                | {"reply": f"Couldn't do that: {exc.message}", "status": "error"},
            )
        recent = [*(state.get("recent") or []), *recent_items(outcome)][-RECENT_KEEP:]
        _step("execute", started, action=spec.name, ok=True, write=spec.write)
        update: dict[str, Any] = _cleared() | {
            "outcome": outcome,
            "recent": recent,
            "reply": outcome["message"],
            "status": "done",
        }
        tip = suggest(outcome)
        if tip is None:
            return Command(goto=END, update=update)
        if tip.action is None:
            return Command(goto=END, update=update | {"reply": f"{outcome['message']}\n{tip.text}"})
        # A suggestion is only a new draft: it waits for its own yes.
        update["action"] = tip.action
        update["payload"] = dict(tip.payload or {})
        update["pending"] = {
            "kind": "suggestion",
            "action": tip.action,
            "payload": dict(tip.payload or {}),
            "summary": tip.text,
            "confirm_text": None,
        }
        return Command(goto="confirm", update=update)

    # --- answer (hand-off to the owner graph) ----------------------------------------------

    async def answer(self, state: IntakeState) -> dict[str, Any]:
        started = time.perf_counter()
        owner, conversations = self.deps.owner_graph, self.deps.conversations
        if owner is None or conversations is None:
            return {"reply": "Questions aren't available here.", "status": "error"}
        history = await conversations.history(state["conversation_id"])
        owner_state = initial_state(
            conversation_id=state["conversation_id"],
            question=str(state.get("question") or state["message"]),
            history=history,
            max_tier=INTAKE_TIER,
            min_tier=INTAKE_TIER,
            context=_owner_context(state),
        )
        writer = get_stream_writer()
        final: dict[str, Any] = {}
        async for item in owner.astream(owner_state, stream_mode=["custom", "values"]):
            mode, chunk = cast(tuple[str, Any], item)
            if mode == "custom":
                writer(chunk)
            else:
                final = chunk
        error = final.get("error")
        _step("answer", started, route=final.get("route"), error=error is not None)
        return {
            "reply": final.get("answer", ""),
            "status": "answer",
            "outcome": {
                "action": "answer",
                "route": final.get("route"),
                "model": final.get("model_id"),
                "seq": final.get("seq"),
                "citations": [s.document_id for s in final.get("sources", [])],
                "error": error.model_dump(mode="json") if error is not None else None,
            },
        }


IntakeGraph = CompiledStateGraph[IntakeState, None, IntakeState, IntakeState]


def build_intake_graph(deps: IntakeDeps, checkpointer: Checkpointer) -> IntakeGraph:
    nodes = IntakeNodes(deps)
    graph = StateGraph(IntakeState)
    graph.add_node("gather", nodes.gather, destinations=("confirm", "execute", "answer", END))
    graph.add_node(
        "confirm", nodes.confirm, destinations=("confirm", "second", "execute", "gather", END)
    )
    graph.add_node("second", nodes.second, destinations=("execute", END))
    graph.add_node("execute", nodes.execute, destinations=("confirm", END))
    graph.add_node("answer", nodes.answer)
    graph.add_edge(START, "gather")
    graph.add_edge("answer", END)
    return graph.compile(checkpointer=checkpointer)
