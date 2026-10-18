"""The capture loop: the public graph's nodes that take an opportunity's details in conversation.

`capture` runs after the input guard and the scope router on every turn while a draft is open
(or when the router says `opportunity`). The model only extracts: email and link come from
patterns in code, a value the model gives must be grounded in the visitor's own words, and
`app.leads.models.check` decides what is valid and what is still missing. One short question per
turn, at most LEAD_MAX_TURNS turns, then the contact-form reply.

A complete draft is shown back and the graph pauses at `confirm` (LangGraph `interrupt`). The
visitor's next message resumes it and goes back through the input guard and the router like any
other; only an explicit yes reaches `submit`. Nothing is stored or emailed before that. A
question about the owner in between is answered as usual and the draft waits.

Replies are fixed templates around the visitor's own values: no generated text reaches the
visitor from here. Logs carry stage, field names and counts, never values.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

import structlog
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langgraph.graph import END
from langgraph.types import Command, interrupt

from app.agents.intake.confirm import classify_rules
from app.agents.prompts import public as prompts
from app.guardrails.output import OutputGuard
from app.guardrails.scope import FIXED_REPLIES
from app.leads.models import (
    EMPLOYMENT_WORDS,
    FIELDS,
    LIMITS,
    REQUIRED,
    LeadDraft,
    check,
    clean,
)
from app.leads.service import LeadService
from app.observability.masking import channel_scope
from app.services.conversations import Turn
from llm_orchestrator import Orchestrator, OrchestratorError, Role, Tier

logger = structlog.get_logger(__name__)

LEAD_TIER = Tier.PUBLIC
OPPORTUNITY = "opportunity"
# Intents a question in the middle of a draft is answered under; the draft waits.
ANSWERABLE = frozenset({"about_owner", "resume", "availability"})
SUMMARY_TURNS = 12
SUMMARY_CHARS = 4000

LABELS = {
    "name": "Name",
    "email": "Email",
    "company": "Company",
    "role": "Role",
    "employment_type": "Type",
    "location": "Location",
    "link": "Link",
    "message": "About",
}
QUESTIONS = {
    "role": "What's the role: a job title, or the kind of engagement?",
    "company": "Which company or organisation is it with?",
    "message": "Could you tell me a little about the opportunity?",
    "name": "And what's your name?",
    "email": "What email address should the reply go to?",
}
INTRO = "Thanks for thinking of me! I can pass this straight on to {owner}."
INVALID = "That {label} doesn't look right ({reason})."
CONFIRM = (
    "Here's what I'll pass on to {owner}:\n{summary}\n\n"
    "Shall I send it? (yes / no / tell me what to change)"
)
CHANGE = 'No problem. What should I change? Or say "cancel" to drop it.'
EXITED = "No problem, nothing was sent. Ask me anything else about my work."
THANKS = "Thanks, I've passed this to {owner}."
REPLY_TIME = " You can expect a reply {reply_time}."
CAPPED = (
    "Thanks! I can't pass on any more messages right now. Please use the contact form on this "
    "site instead."
)
UNVERIFIED = (
    "I couldn't verify this request, so nothing was sent. Please try again, or use the contact "
    "form on this site."
)
FAILED = (
    "Sorry, I couldn't save that just now. Please try again in a moment, or use the contact form "
    "on this site."
)

EXITS = frozenset(
    {
        "never mind",
        "nevermind",
        "cancel",
        "forget it",
        "stop",
        "no thanks",
        "not interested",
        "drop it",
        "cancel it",
    }
)
_EMAIL = re.compile(r"[\w.+'-]+@[\w-]+(?:\.[\w-]+)+")
_URL = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_QUESTION = re.compile(
    r"^\s*(what|which|how|why|when|where|who|do|did|does|are|is|can|could|have|has|would|will)\b",
    re.IGNORECASE,
)
_LEAD_IN = re.compile(
    r"^(it'?s|it is|my name is|i am|i'm|this is|we are|we're|the company is|company is|"
    r"the role is|role is|at|for)\s+",
    re.IGNORECASE,
)
# Words, emails and dotted names ("node.js"), without trailing punctuation.
_WORDS = re.compile(r"[a-z0-9@+#]+(?:[.'-][a-z0-9@+#]+)*")
SHORT_ANSWER = {"name", "company", "role", "location"}


@dataclass(frozen=True)
class CaptureSettings:
    max_turns: int = 6
    owner_name: str = ""

    @property
    def owner(self) -> str:
        return self.owner_name.strip() or "the owner"


@dataclass(frozen=True)
class CaptureDeps:
    orchestrator: Orchestrator
    service: LeadService
    output_guard: OutputGuard
    reply_time: Callable[[], Awaitable[str | None]]
    settings: CaptureSettings = CaptureSettings()


def new_lead() -> dict[str, Any]:
    return {"stage": "gathering", "draft": {}, "asked": None, "turns": 0}


def _words(text: str) -> list[str]:
    return _WORDS.findall(text.casefold())


def is_exit(text: str) -> bool:
    joined = " ".join(re.sub(r"[^\w\s']", " ", text.casefold()).split())
    return joined in EXITS or any(joined.startswith(f"{e} ") for e in EXITS)


def question_like(text: str) -> bool:
    return text.rstrip().endswith("?") or bool(_QUESTION.match(text))


def grounded(name: str, value: str, said: str) -> bool:
    """A model value counts only when it comes from the visitor's own words."""
    if name == "employment_type":
        # Inferred, not copied: it counts only when the visitor used one of its words.
        spoken = f" {' '.join(_words(said.replace('-', ' ')))} "
        return any(f" {alias} " in spoken for alias in EMPLOYMENT_WORDS)
    words = [w for w in _words(value) if len(w) > 1]
    if not words:
        return False
    heard = set(_words(said))
    share = sum(w in heard for w in words) / len(words)
    return share >= (0.5 if name == "message" else 0.67)


def pattern_values(text: str) -> dict[str, str]:
    found: dict[str, str] = {}
    if email := _EMAIL.search(text):
        found["email"] = email.group(0).rstrip(".")
    if url := _URL.search(text):
        found["link"] = url.group(0).rstrip(".,;:!?)")
    return found


def short_answer(field: str, text: str) -> str | None:
    """The whole message as the answer to the question just asked ("Acme Corp")."""
    if field == "message":
        return text if len(text) <= LIMITS["message"] else None
    if field not in SHORT_ANSWER:
        return None
    value = _LEAD_IN.sub("", clean(text)).rstrip(".!")
    return value if value and len(value) <= LIMITS[field] and len(value.split()) <= 8 else None


def summary_lines(values: dict[str, str]) -> str:
    return "\n".join(f"• {LABELS[k]}: {values[k]}" for k in LABELS if values.get(k))


def transcript(history: Sequence[Turn], text: str) -> str:
    lines = [
        f"{'Visitor' if t.role == 'user' else 'Assistant'}: {t.content}"
        for t in history[-SUMMARY_TURNS:]
    ]
    lines.append(f"Visitor: {text}")
    return "\n".join(lines)[-SUMMARY_CHARS:]


def _step(step: str, started: float, **fields: Any) -> None:
    logger.info(
        "public step",
        step=step,
        latency_ms=round((time.perf_counter() - started) * 1000, 1),
        **fields,
    )


class CaptureNodes:
    def __init__(self, deps: CaptureDeps) -> None:
        self.deps = deps
        self.owner = deps.settings.owner

    # --- capture ------------------------------------------------------------------------------

    async def capture(self, state: dict[str, Any]) -> Command[str]:
        started = time.perf_counter()
        text = state["text"]
        lead = dict(state.get("lead") or new_lead())
        opening = state.get("lead") is None
        if not opening and is_exit(text):
            _step("capture", started, stage=lead["stage"], outcome="exit")
            return self._end(None, EXITED)
        if lead["stage"] == "confirm":
            decision = classify_rules(text)
            if decision == "yes":
                return Command(goto="submit", update={"intent": OPPORTUNITY})
            if decision == "no":
                lead["stage"] = "gathering"
                _step("capture", started, stage="confirm", outcome="change")
                return self._end(lead, CHANGE)
        found = await self._extract(state, lead, opening)
        if not found and not opening and question_like(text):
            # A question in between: answer it as usual and keep the draft.
            _step("capture", started, stage=lead["stage"], outcome="handoff")
            intent = state.get("intent")
            if intent == "about_owner":
                return Command(goto="retrieve")
            if intent in ANSWERABLE or lead["stage"] != "confirm":
                return Command(goto=END)
            return self._end(lead, self._confirm_text(lead["draft"]))
        return self._gathered(lead, found, opening, started)

    @staticmethod
    def _reported(invalid: dict[str, str], asked: str | None) -> dict[str, str]:
        """Invalid values worth telling the visitor about: required fields and the one just
        asked for. An optional value the model inferred wrongly is dropped quietly."""
        return {k: v for k, v in invalid.items() if k in REQUIRED or k == asked}

    def _gathered(
        self, lead: dict[str, Any], found: dict[str, str], opening: bool, started: float
    ) -> Command[str]:
        lead["turns"] += 1
        if lead["turns"] > self.deps.settings.max_turns:
            _step("capture", started, outcome="turn_cap", turns=lead["turns"])
            return self._end(None, FIXED_REPLIES["opportunity"])
        result = check({**lead["draft"], **found})
        lead["draft"] = dict(result.values)
        invalid = self._reported(result.invalid, lead.get("asked"))
        _step(
            "capture",
            started,
            stage=lead["stage"],
            found=sorted(found),
            missing=result.missing,
            invalid=sorted(result.invalid),
            turns=lead["turns"],
        )
        parts = [INTRO.format(owner=self.owner)] if opening else []
        parts += [
            INVALID.format(label=LABELS[name].lower(), reason=reason)
            for name, reason in invalid.items()
        ]
        if result.missing:
            lead |= {"stage": "gathering", "asked": result.missing[0]}
            return self._end(lead, " ".join([*parts, QUESTIONS[result.missing[0]]]))
        if invalid:
            lead["asked"] = next(iter(invalid))
            return self._end(lead, " ".join(parts))
        lead |= {"stage": "confirm", "asked": None}
        reply = self._confirm_text(lead["draft"])
        update = {"lead": lead, "answer": reply, "intent": OPPORTUNITY}
        return Command(goto="confirm", update=update)

    def _confirm_text(self, values: dict[str, str]) -> str:
        return CONFIRM.format(owner=self.owner, summary=summary_lines(values))

    @staticmethod
    def _end(lead: dict[str, Any] | None, reply: str) -> Command[str]:
        return Command(goto=END, update={"lead": lead, "answer": reply, "intent": OPPORTUNITY})

    async def _extract(
        self, state: dict[str, Any], lead: dict[str, Any], opening: bool
    ) -> dict[str, str]:
        text = state["text"]
        said = " ".join([*(t.content for t in state.get("history", []) if t.role == "user"), text])
        found = {
            name: str(value).strip()
            for name, value in (await self._model_values(text, lead)).items()
            if value and str(value).strip() and grounded(name, str(value), said)
        }
        found |= pattern_values(text)
        asked = lead.get("asked")
        if asked == "message" and not question_like(text) and text.strip():
            # The answer to "tell me about it" is the visitor's own words, not a model's digest.
            found["message"] = text.strip()
        if found or question_like(text):
            return found
        if asked and (answer := short_answer(asked, text)):
            return {asked: answer}
        if opening and len(text) >= 30:
            return {"message": text}
        return {}

    async def _model_values(self, text: str, lead: dict[str, Any]) -> dict[str, Any]:
        known = json.dumps(lead.get("draft") or {}, ensure_ascii=False)
        messages: list[BaseMessage] = [
            SystemMessage(content=prompts.LEAD_EXTRACT_V1),
            HumanMessage(
                content=(
                    f"Question just asked: {lead.get('asked') or 'none'}\n"
                    f"Already known: {known}\n\nVisitor message: {text}"
                )
            ),
        ]
        try:
            # The extracted fields are a lead's: traced under the lead policy (always masked).
            with channel_scope("leads"):
                result = await self.deps.orchestrator.structured_call(
                    Role.GENERATOR, LEAD_TIER, LeadDraft, messages
                )
        except OrchestratorError as exc:
            logger.warning("lead extraction failed", error_type=type(exc).__name__)
            return {}
        return {k: v for k, v in result.value.model_dump().items() if k in FIELDS}

    # --- confirm ------------------------------------------------------------------------------

    async def confirm(self, state: dict[str, Any]) -> Command[str]:
        lead = state.get("lead") or {}
        # Nothing above the interrupt may have side effects: the node re-runs on resume.
        reply = interrupt({"kind": "lead_confirm", "fields": sorted(lead.get("draft", {}))})
        # The reply goes through the input guard and the router like any other message.
        return Command(goto="input_guard", update={"question": str(reply)})

    # --- submit -------------------------------------------------------------------------------

    async def submit(self, state: dict[str, Any]) -> dict[str, Any]:
        started = time.perf_counter()
        lead = dict(state.get("lead") or {})
        draft = check(lead.get("draft") or {})
        if not draft.complete:
            lead |= {"stage": "gathering"}
            return {"lead": lead, "answer": CHANGE, "intent": OPPORTUNITY}
        summary = await self._summary(state)
        try:
            result = await self.deps.service.submit(
                draft.lead(),
                source="assistant",
                visitor_hash=state["visitor_hash"],
                turnstile_token=state.get("turnstile_token"),
                conversation_id=state["conversation_id"],
                summary=summary,
            )
        except Exception as exc:
            logger.error("lead submit failed", error_type=type(exc).__name__)
            _step("submit", started, outcome="error")
            return {"lead": lead, "answer": FAILED, "intent": OPPORTUNITY}
        _step("submit", started, outcome=result.outcome, refusal=result.refusal)
        if result.refusal == "turnstile":
            return {"lead": lead, "answer": UNVERIFIED, "intent": OPPORTUNITY}
        if result.outcome == "refused":
            return {"lead": None, "answer": CAPPED, "intent": OPPORTUNITY}
        reply = THANKS.format(owner=self.owner)
        if reply_time := await self._reply_time():
            reply += REPLY_TIME.format(reply_time=reply_time.rstrip("."))
        return {"lead": None, "answer": reply, "intent": OPPORTUNITY}

    async def _reply_time(self) -> str | None:
        try:
            return await self.deps.reply_time()
        except Exception as exc:
            logger.warning("reply time unavailable", error_type=type(exc).__name__)
            return None

    async def _summary(self, state: dict[str, Any]) -> str | None:
        """3-5 lines over the public conversation, for the owner. It passes the output guard
        (canaries, prompt leaks, grounding in the transcript) or the email goes without."""
        text = transcript(state.get("history", []), state["text"])
        messages: list[BaseMessage] = [
            SystemMessage(content=prompts.LEAD_SUMMARY_V1),
            HumanMessage(content=f"Transcript:\n{text}"),
        ]
        try:
            with channel_scope("leads"):
                result = await self.deps.orchestrator.chat(Role.GENERATOR, LEAD_TIER, messages)
        except OrchestratorError as exc:
            logger.warning("lead summary failed", error_type=type(exc).__name__)
            return None
        # The address is in the email's fields already; the guard would block it here.
        summary = _EMAIL.sub("[email]", result.text.strip())[:1000]
        verdict = await self.deps.output_guard.check(
            summary, question="Summarise this conversation.", context=text
        )
        return summary if verdict.passed and summary else None
