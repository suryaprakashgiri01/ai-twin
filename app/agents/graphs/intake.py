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
    failed_items,
    json_safe,
    recent_items,
    resolve_target,
)
from app.agents.intake.batch import (
    looks_like_batch,
    missing_in_batch,
    parse_company_type,
    parse_record_batch,
)
from app.agents.intake.confirm import (
    NO_WORDS,
    YES_WORDS,
    Decision,
    classify_reply,
    retype_matches,
)
from app.agents.intake.drafts import (
    IntakeDraft,
    check,
    day_value,
    find_day,
    github_repos,
    ground,
    local_today,
    merge,
    normalise,
    only_github_repo_urls,
)
from app.agents.intake.labels import (
    camel_to_snake_payload,
    find_json_object,
    merge_labelled,
    parse_labels,
    validate_paste,
)
from app.agents.intake.suggest import suggest
from app.agents.models import AUTO_CHOICE, ModelChoice
from app.agents.prompts import intake as prompts
from app.agents.routing import (
    canonical_kind,
    canonical_platform,
    is_question,
    platform_in,
    record_kind_in,
)
from app.db.documents import UID_PATTERN, utcnow
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
_TYPED_ID = UID_PATTERN
_STATUS = re.compile(r"\bstatus\b|\bis it done\b|\bfinished\b", re.I)
_REMEMBER = re.compile(r"^\s*(please\s+)?remember\b(?!\s+(when|what|which|how|if)\b)", re.I)
_FORGET = re.compile(r"^\s*(please\s+)?(forget|stop remembering)\b", re.I)
_SHOW_MEMORY = re.compile(
    r"\b(show|list|what are)\s+(me\s+)?(my\s+|your\s+)?(memories|memory|preferences)\b|"
    r"\bwhat do you remember\b",
    re.I,
)
# A message that only thanks or acknowledges: never a request, whatever the model reads into it.
_ACK = re.compile(
    r"^\s*(thanks?( you)?|thank u|thx|ty|cheers|ok(ay)?|cool|great|nice|perfect|awesome|"
    r"got it|good|alright|all good|sounds good|noted)"
    r"(\s+(a lot|so much|very much|again|mate|buddy))?[\s!.🙂👍🙏]*$",
    re.I,
)
# A message that reports a job-search step ("had an HR call", "got an invite", "applied to").
_REPORT = re.compile(
    r"\b(had|have|got|received|attended|gave|cleared|scheduled|applied|did|took|finished|"
    r"completed|was invited|invited)\b",
    re.I,
)
_BARE_REPLY = 3
# An imperative on a document: "publish the Lumenrail note", "please approve it".
_DOC_COMMAND = re.compile(
    r"^\s*(?:please\s+|can you\s+|could you\s+)?(publish|unpublish|approve|release)\b", re.I
)
# A remark that something already happened: nothing to do.
_ALREADY = re.compile(r"\balready\b.*\b(done|published|approved|saved|added|there)\b", re.I)
# A chat reply may never claim an action happened: actions report through `execute`.
_CLAIMS = re.compile(
    r"\b(done|saved|published|added|deleted|updated|approved|synced|ingested|recorded)\b", re.I
)
ACK_REPLY = "Glad that helped. Anything else to add or change?"
ALREADY_REPLY = "Then there's nothing to do; I haven't changed anything."
_RETRY = re.compile(r"\b(try (it )?again|retry|one more time|redo)\b", re.I)
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
    if action is None and (verb := _DOC_COMMAND.match(message)):
        return {"delete": "delete_document"}.get(verb.group(1).lower(), verb.group(1).lower())
    reports = record_kind_in(message) and _REPORT.search(message) and not is_question(message)
    if action is None and reports:
        return "add_records" if looks_like_batch(message) else "add_record"
    if action == "add_record" and looks_like_batch(message):
        return "add_records"
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
    # The last action that failed, so "try again" retries it with the same fields.
    failed: dict[str, Any] | None
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
    # config/agents.yaml `intake`: which local model reads the owner's message.
    models: ModelChoice = AUTO_CHOICE
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
_SAY_MEAN = re.compile(r"^when i say\s+(.+?),?\s+i mean\s+(.+?)[.!]?$", re.I)
_ALIAS = re.compile(r"\bwhen i say\b|\bi mean\b|\bstands for\b|=|\bmeans\b", re.I)
_RULE = re.compile(r"\b(always|never|every time|from now on|don't|do not)\b", re.I)


# A stage the owner rarely says for these kinds; asking for it would only slow them down.
DEFAULT_STAGE = {"application": "applied", "invite": "invited to apply", "hr_call": "HR call"}


_COMPANY = re.compile(r"\b(?:with|from|at|to)\s+((?:[A-Z][\w&.'-]*)(?:\s+(?:[A-Z][\w&.'-]*|&))*)")
# A title token: capitalised words, plus en/em dash, II, & (e.g. Senior AI Engineer - Inference).
# Not case-folded, so "I applied" cannot be swallowed into the title.
_DASH = "\u2013\u2014"  # en dash, em dash
_TITLE_WORD = rf"[A-Z][\w.+/#{_DASH}]*|[{_DASH}]|II|&|of|and"
_ROLE = re.compile(
    rf"(?i:for (?:an? |the )?)((?:{_TITLE_WORD})(?: (?:{_TITLE_WORD})){{0,12}}) "
    r"(?i:role|position|opening|job)\b"
)
# "applied to (the) X role at Y" / "the X role at Y".
_APPLIED_ROLE = re.compile(
    rf"(?i:applied\s+to\s+(?:an?\s+|the\s+)?|the\s+)"
    rf"((?:{_TITLE_WORD})(?: (?:{_TITLE_WORD})){{0,12}}) "
    r"(?i:role|position|opening|job)\s+at\b"
)
# "for a Senior AI Engineer (Job ID …)": capitalised title; dashes and & allowed inside.
_TITLED_ROLE = re.compile(
    rf"(?i:for (?:an? |the )?)"
    rf"((?:[A-Z][\w.+/#{_DASH}]*)(?: (?:[A-Z][\w.+/#{_DASH}]*|of|and|&|II|[{_DASH}]))*)"
    r"(?=$|[\s(,.;])"
)
_WORK_MODE = re.compile(r"\b(onsite|on-site|remote|hybrid|online)\b", re.I)
_LOCATION = re.compile(
    r"\b(?:for\s+location|located\s+in|based\s+in|location)\s*:?\s+"
    r"([A-Za-z][\w .,'-]{1,80}?)"
    r"(?=\s*,?\s*(?:\b(?:onsite|on-site|remote|hybrid|online)\b)|$|\n|"
    r"\b(?:role|company|work\s*mode|notes|job\s*id)\s*:)",
    re.I,
)
# "in Hyderabad, Telangana" after a role/company cue; avoid matching "in notes".
_IN_PLACE = re.compile(
    r"\bin\s+([A-Z][A-Za-z][\w.-]*(?:\s*,\s*[A-Z][A-Za-z][\w.-]*){0,3})"
    r"(?!\s+notes\b)"
)
# A posting's reference ("Job ID: 0006955", "Req #R-1234"), kept as a note and never a phone.
_REFERENCE = re.compile(
    r"\b(?:job|req(?:uisition)?|ref(?:erence)?|posting|opening)\s*"
    r"(?:id|no\.?|number|#)\s*[:#]?\s*([A-Za-z0-9][\w-]{2,})",
    re.I,
)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE = re.compile(r"\+?\d[\d\s-]{7,}\d")
_CONTACT_NAME = re.compile(
    r"\b(?i:hr|recruiter|contact)(?: person)? (?i:was|is|named|called)\s+"
    r"([A-Z][a-z]+(?: [A-Z][a-z]+)?)"
)
_NOT_COMPANIES = frozenset({"HR", "Naukri", "LinkedIn", "Instahyre", "Indeed", "Monday", "Today"})


def _drop_misreads(
    out: dict[str, Any], message: str, tz: ZoneInfo, now: datetime
) -> dict[str, Any]:
    """A small model's usual slips: a date as the stage, the company as the platform, a cut
    company name, and the contact copied into the notes."""
    stage = str(out.get("stage") or "")
    if stage and find_day(stage, tz, now) is not None:
        out.pop("stage")
    platform = str(out.get("platform") or "")
    if (
        platform
        and platform_in(platform) is None
        and f"via {platform}".casefold() not in (message.casefold())
    ):
        out.pop("platform")
    company, said = str(out.get("company") or ""), _first_company(message)
    if said and company and company != said and said.casefold().startswith(company.casefold()):
        out["company"] = said
    if out.get("notes") and out.get("notes") == (out.get("contact") or _contact(message)):
        out.pop("notes")
    reference = _REFERENCE.search(message)
    if reference and reference.group(1) in str(out.get("contact") or ""):
        out.pop("contact")
    return out


def _first_company(message: str) -> str | None:
    for match in _COMPANY.finditer(message):
        name = match.group(1).strip()
        if name not in _NOT_COMPANIES and not platform_in(name):
            return name
    return None


def _contact(message: str) -> str | None:
    message = _REFERENCE.sub(" ", message)
    parts = [m.group(1) for m in [_CONTACT_NAME.search(message)] if m]
    parts += _EMAIL.findall(message) + [p.strip() for p in _PHONE.findall(message)]
    return " ".join(dict.fromkeys(parts)) or None


def _clean_role(role: str, company: str | None) -> str | None:
    role = re.sub(r"\s+", " ", role).strip(f" -{_DASH}")
    if not role or role == company or role in _NOT_COMPANIES or platform_in(role):
        return None
    return role


def _role(message: str, company: str | None) -> str | None:
    for pattern in (_APPLIED_ROLE, _ROLE):
        match = pattern.search(message)
        cleaned = _clean_role(match.group(1), company) if match else None
        if cleaned:
            return cleaned
    for match in _TITLED_ROLE.finditer(message):
        cleaned = _clean_role(match.group(1), company)
        if cleaned:
            return cleaned
    return None


def _location(message: str) -> str | None:
    if m := _LOCATION.search(message):
        return re.sub(r"\s+", " ", m.group(1)).strip(" .,")
    if m := _IN_PLACE.search(message):
        place = m.group(1).strip(" .,")
        if place.casefold() not in {"notes", "note"} and not platform_in(place):
            return place
    return None


def _work_mode(message: str) -> str | None:
    if not (m := _WORK_MODE.search(message)):
        return None
    word = m.group(1).casefold().replace("-", "")
    if word in {"onsite", "online"}:
        return "onsite" if word == "onsite" else "remote"
    if word in {"remote", "hybrid"}:
        return word
    return None


def _reference_note(message: str) -> str | None:
    m = _REFERENCE.search(message)
    return f"Job ID: {m.group(1)}" if m else None


def _with_reference(notes: str | None, message: str) -> str | None:
    note = _reference_note(message)
    if not note or (notes and note.split(": ", 1)[1] in notes):
        return notes or note
    return f"{notes}; {note}" if notes else note


def _record_defaults(
    payload: dict[str, Any], message: str, tz: ZoneInfo, now: datetime, *, adding: bool = True
) -> dict[str, Any]:
    """Fields a small model missed or misread, from the owner's own words: the kind, company,
    role, date, platform (in its usual spelling), HR contact and a posting's job id, and, when
    adding, the default stage of an application, invite or HR call. Nothing here is invented:
    each value is in the message or left for a question."""
    out = _drop_misreads(dict(payload), message, tz, now)
    company = _first_company(message)
    found: dict[str, Any] = {
        "kind": record_kind_in(message),
        "company": company,
        "role": _role(message, str(out.get("company") or company or "")),
        "platform": platform_in(message),
        "contact": _contact(message),
        "location": _location(message),
        "work_mode": _work_mode(message),
        "company_type": parse_company_type(message),
    }
    if (day := find_day(message, tz, now)) is not None:
        found["date"] = day_value(day)
    for key, value in found.items():
        if value and not out.get(key):
            out[key] = value
    if notes := _with_reference(out.get("notes"), message):
        out["notes"] = notes
    if out.get("platform"):
        out["platform"] = canonical_platform(str(out["platform"]))
    kind = canonical_kind(str(out.get("kind") or ""))
    if adding and kind in DEFAULT_STAGE and not out.get("stage"):
        out["stage"] = DEFAULT_STAGE[kind]
    return out


def _settled_without_model(message: str, current: str | None) -> tuple[str, str] | None:
    """(rule, reply) for messages that need no model and must never start a draft."""
    if _ACK.match(message):
        return "ack", ACK_REPLY
    if current is None and _bare_reply(message):
        return "bare_reply", NOTHING_PENDING
    if _ALREADY.search(message) and not _DOC_COMMAND.match(message):
        return "already", ALREADY_REPLY
    return None


def _chat_reply(reply: str) -> str:
    """The model's small talk, unless it claims something was done (only `execute` may)."""
    return reply if reply and not _CLAIMS.search(reply) else CHAT_DEFAULT


_ANSWER_LEAD = re.compile(r"^\s*(?:it(?:'s| is| was)|this is|that's|the)\s+(?:an?\s+)?", re.I)
_ANSWER_WORDS = 8


def _fill_asked(spec: ActionSpec, payload: dict[str, Any], state: IntakeState) -> dict[str, Any]:
    """A short reply to the one free-text field intake asked for is that field's value.

    "screen" or "It is a technical screen." after "I still need: stage" sets the stage; the
    model alone often leaves a bare answer unmapped."""
    asked = state.get("missing") or []
    if len(asked) != 1:
        return payload
    name = asked[0]
    if name in spec.date_fields or name in spec.list_fields or payload.get(name):
        return payload
    message = state["message"].strip()
    if _bare_reply(message) or len(message.split()) > _ANSWER_WORDS:
        return payload
    value = _ANSWER_LEAD.sub("", message).strip(" .!")
    return {**payload, name: value} if value else payload


def _bare_reply(message: str) -> bool:
    """A lone yes or no ("yes", "ok", "nope"), with no request in it ("publish it" is one)."""
    words = re.sub(r"[^\w\s']", " ", message.casefold()).split()
    return 0 < len(words) <= _BARE_REPLY and all(w in YES_WORDS | NO_WORDS for w in words)


_DOC_VERBS = {
    "publish": "publish",
    "approve": "approve",
    "release": "release",
    "unpublish": "unpublish",
    "delete_document": "delete",
    "relabel": "relabel",
}


def _document_ref(action: str, message: str) -> str | None:
    """`publish the Lumenrail note` → "Lumenrail note" when the model left the target out."""
    verb = _DOC_VERBS.get(action)
    if verb is None:
        return None
    match = re.match(
        rf"^\s*(?:please\s+|can you\s+|could you\s+)?{verb}\s+(?:the\s+|my\s+)?(.+?)[\s.!?]*$",
        message,
        re.I,
    )
    ref = match.group(1).strip() if match else ""
    return ref if ref and ref.casefold() not in {"it", "that", "this", "it again"} else None


def _remember_defaults(payload: dict[str, Any], message: str) -> dict[str, Any]:
    """The owner's own words when the model left the text out, and a kind from their wording
    when it left that out too."""
    # The owner's own words win over a small model's paraphrase.
    said = _REMEMBER_PREFIX.sub("", message).strip() if _REMEMBER.search(message) else ""
    text = said or str(payload.get("text") or "").strip()
    if (alias := _SAY_MEAN.match(text)) is not None:
        text = f"{alias.group(1).strip()} = {alias.group(2).strip()}"
        payload = {**payload, "kind": "alias"}
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


def _pasted_fields(
    spec: ActionSpec, message: str, *, partial: bool
) -> tuple[dict[str, Any], dict[str, str]]:
    """JSON the owner pasted, validated like the action's API body; empty when there is none."""
    raw = find_json_object(message)
    if raw is None:
        return {}, {}
    cleaned = camel_to_snake_payload(raw)
    required = spec.required
    complete = bool(required) and all(cleaned.get(name) not in (None, "", []) for name in required)
    return validate_paste(raw, spec.model, partial=partial or not complete)


class IntakeNodes:
    def __init__(self, deps: IntakeDeps) -> None:
        self.deps = deps

    # --- gather (the intake agent) ---------------------------------------------------------

    async def gather(self, state: IntakeState) -> Command[str]:
        started = time.perf_counter()
        current = state.get("action")
        message = state["message"]
        settled = _settled_without_model(message, current)
        if settled is not None:
            # Thanks, a lone yes, or "it's already done": no model call, and never a draft.
            _step("gather", started, action=None, rule=settled[0])
            return Command(goto=END, update=_cleared() | {"reply": settled[1], "status": "chat"})
        try:
            extracted, model_id = await self._extract(state)
        except OrchestratorError as exc:
            # The model could not read it; the owner's words may still name the action.
            fallback = rule_action(message, None)
            _step(
                "gather",
                started,
                outcome="model_failed",
                error_type=type(exc).__name__,
                fallback=fallback,
            )
            if fallback is None or fallback == "answer":
                return Command(
                    goto=END,
                    update={
                        "reply": "The local model couldn't read that. Please say it another "
                        "way (is Ollama running? make ollama ARGS=status).",
                        "status": "error",
                    },
                )
            extracted, model_id = IntakeDraft(action=fallback), None
        action = rule_action(message, extracted.action or current)
        retry = self._retry(state, action)
        if retry is not None:
            action = retry["action"]
        if action is None:
            _step("gather", started, action=None, model=model_id)
            return Command(
                goto=END, update={"reply": _chat_reply(extracted.reply), "status": "chat"}
            )
        if action == "answer":
            question = str(extracted.payload.get("question") or state["message"])
            _step("gather", started, action=action, model=model_id)
            return Command(goto="answer", update={"question": question, "status": "answer"})
        if action == "add_records" or (
            action == "add_record" and looks_like_batch(state["message"])
        ):
            batch_cmd = await self._settle_batch(
                state, started, model_id, current is not None and action != current
            )
            if batch_cmd is not None:
                return batch_cmd
            action = "add_record"
        spec = ACTIONS[action]
        switched = current is not None and action != current
        base = {} if switched or current is None else dict(state.get("payload") or {})
        if retry is not None:
            base = dict(retry["payload"])
        labelled = parse_labels(state["message"], set(spec.fields))
        pasted, paste_errors = _pasted_fields(spec, state["message"], partial=bool(base))
        if paste_errors and not pasted:
            _step("gather", started, action=spec.name, model=model_id, paste_errors=True)
            return Command(
                goto=END,
                update={
                    "action": spec.name,
                    "payload": base,
                    "missing": [],
                    "pending": None,
                    "reply": _missing_text(spec, [], paste_errors),
                    "status": "asked",
                },
            )
        model_payload = {**extracted.payload, **{k: v for k, v in pasted.items() if k != "extras"}}
        given, dropped = ground(
            spec, model_payload, state["message"], base, self.deps.timezone, self.deps.now()
        )
        # Owner-labelled and pasted values always win and were never subject to grounding.
        payload = merge_labelled(merge_labelled(merge(base, given), labelled), pasted)
        dropped = [name for name in dropped if name not in labelled and name not in pasted]
        payload = self._defaults(spec, payload, state["message"])
        if spec.name in ("add_record", "update_record"):
            payload = _record_defaults(
                payload,
                state["message"],
                self.deps.timezone,
                self.deps.now(),
                adding=spec.name == "add_record",
            )
            # Labels win again over regex fallbacks when both spoke.
            payload = merge_labelled(merge_labelled(payload, labelled), pasted)
        if not switched:
            payload = _fill_asked(spec, payload, state)
        return await self._settle(spec, payload, state, started, model_id, switched, dropped)

    async def _settle_batch(
        self,
        state: IntakeState,
        started: float,
        model_id: str | None,
        switched: bool,
    ) -> Command[str] | None:
        """Several records in one message → one confirmation. Returns None to fall back to one."""
        deps = self.deps
        items = parse_record_batch(state["message"], deps.timezone, deps.now())
        if len(items) < 2:
            return None
        single = ACTIONS["add_record"]
        normalised: list[dict[str, Any]] = []
        invalid: dict[str, str] = {}
        for index, raw in enumerate(items, start=1):
            item, bad = normalise(single, raw, deps.timezone, deps.now())
            for key, why in bad.items():
                invalid[f"{index}.{key}"] = why
            normalised.append(item)
        missing_lines = missing_in_batch(normalised)
        spec = ACTIONS["add_records"]
        _step(
            "gather",
            started,
            action=spec.name,
            model=model_id,
            switched=switched,
            batch=len(normalised),
            missing=missing_lines,
            invalid=sorted(invalid),
        )
        payload = {"records": normalised}
        update: dict[str, Any] = {
            "action": spec.name,
            "payload": payload,
            "missing": missing_lines,
            "pending": None,
        }
        if missing_lines or invalid:
            parts = []
            if invalid:
                parts.append(
                    "These values didn't fit:\n"
                    + "\n".join(f"- {name}: {why}" for name, why in invalid.items())
                )
            if missing_lines:
                parts.append(
                    "To save these records, I still need:\n"
                    + "\n".join(f"- {line}" for line in missing_lines)
                )
            return Command(goto=END, update=update | {"reply": "\n".join(parts), "status": "asked"})
        pending = {
            "kind": "draft",
            "action": spec.name,
            "payload": payload,
            "summary": spec.summary(payload),
            "confirm_text": None,
        }
        return Command(goto="confirm", update=update | {"pending": pending, "status": "draft"})

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
        if resolution.settled:
            return Command(
                goto=END,
                update=_cleared() | {"reply": resolution.settled, "status": "chat", "failed": None},
            )
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
    def _retry(state: IntakeState, action: str | None) -> dict[str, Any] | None:
        """The failed action to retry: the owner asks again for it, or says "try again"."""
        failed = state.get("failed")
        if not failed or state.get("action"):
            return None
        if action == failed["action"] or (_RETRY.search(state["message"]) and action is None):
            return failed
        return None

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
        if spec.target == "document" and not payload.get("document") and not typed_id:
            ref = _document_ref(spec.name, message)
            if ref:
                payload = {**payload, "document": ref}
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
        system = prompts.EXTRACT_V2.format(
            today=f"{today.isoformat()} ({today.strftime('%A')})",
            catalog=catalog_text(),
            draft_schema=prompts.draft_schema_json(),
            record_schema=prompts.record_schema_json(),
            records_batch_schema=prompts.records_batch_schema_json(),
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
                model, prefer = deps.models.pick(INTAKE_TIER)
                result = await deps.orchestrator.structured_call(
                    Role.ROUTER,
                    INTAKE_TIER,
                    IntakeDraft,
                    messages,
                    model=model,
                    prefer=prefer,
                    privileged=True,
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
            recent = [*(state.get("recent") or []), *failed_items(payload)][-RECENT_KEEP:]
            return Command(
                goto=END,
                update=_cleared()
                | {
                    "reply": f"Couldn't do that: {exc.message}. Say “try again” to retry.",
                    "status": "error",
                    "recent": recent,
                    "failed": {"action": spec.name, "payload": json_safe(payload)},
                },
            )
        recent = [*(state.get("recent") or []), *recent_items(outcome)][-RECENT_KEEP:]
        _step("execute", started, action=spec.name, ok=True, write=spec.write)
        update: dict[str, Any] = _cleared() | {
            "failed": None,
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
