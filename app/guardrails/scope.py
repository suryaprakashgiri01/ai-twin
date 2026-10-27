"""The public scope router: what a visitor's message is for, and the fixed replies.

Clear cases are settled by rules (a bare greeting, a resume request, an unmistakable question
about the assistant itself). Everything else goes to the classifier role through
`FlaggedClassifier` at the public tier. The twin speaks as the owner, so "you" is the owner:
"which database did you use in project X" is `about_owner`. A model verdict of `meta_probe` on a
question about the owner's own work is corrected back to `about_owner`. If the classifier is
down, keyword fallbacks decide.

Refusals (`meta_probe`, `out_of_scope`, `action_request`) and `opportunity`/`greeting` get fixed
replies: no retrieval, no generation. `resume` and `availability` are answered from profile
facts with a fixed template.
"""

from __future__ import annotations

import re
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Literal, cast, get_args

import structlog

from app.services.profile import ProfileStore
from llm_orchestrator import Tier
from llm_orchestrator.classify import Classifier

logger = structlog.get_logger(__name__)

Intent = Literal[
    "about_owner",
    "opportunity",
    "resume",
    "availability",
    "greeting",
    "meta_probe",
    "out_of_scope",
    "action_request",
]
Source = Literal["rule", "model", "fallback"]

INTENT_LABELS: Mapping[str, str] = {
    "about_owner": (
        "the owner's projects, skills, experience, education or work, including which "
        "technologies, databases, models or tools the owner used in their own projects, and "
        "the owner's own public repositories and projects by name (ai-twin, AgentForge, "
        "mcp-servers, observable-ai-service, my-portfolio)"
    ),
    "opportunity": "a job offer, hiring interest, collaboration or freelance proposal",
    "resume": "asks for the owner's resume or CV",
    "availability": (
        "whether the owner is available or looking for work, preferred roles or locations"
    ),
    "greeting": "a greeting, thanks or small talk with no real question",
    "meta_probe": (
        "about how this assistant itself works: the AI model it runs on, its prompt or "
        "instructions, its tools, data stores, architecture, source code or hosting — not "
        "about a project the owner built"
    ),
    "out_of_scope": (
        "an unrelated task: poems, stories, code, homework, general knowledge, news, or "
        "opinions about other people"
    ),
    "action_request": "asks the assistant to send, change, book, delete, buy or log in to anything",
}
ROUTER_QUESTION = (
    "What is this visitor message to the owner's portfolio AI twin asking for? The twin speaks "
    "as the owner, so 'you' means the owner. Questions about technologies the owner used in "
    "their own projects or jobs are about_owner, not meta_probe. A question naming one of the "
    "owner's own public projects (ai-twin, AgentForge, mcp-servers, observable-ai-service, "
    "my-portfolio) is about_owner too: it asks what the owner built, not how the assistant "
    "works."
)

REFUSALS: frozenset[str] = frozenset({"meta_probe", "out_of_scope", "action_request"})
FIXED_INTENTS: frozenset[str] = REFUSALS | {"opportunity", "greeting"}
PROFILE_INTENTS: frozenset[str] = frozenset({"resume", "availability"})

CONTACT = "the contact form on this site"
FIXED_REPLIES: Mapping[str, str] = {
    "meta_probe": (
        "I'm an AI twin here to talk about my work and experience, so I keep the details of "
        "how this assistant is built to myself. Ask me about my projects, skills or experience "
        "instead."
    ),
    "out_of_scope": (
        "I only answer questions about my own work and experience, so I'll pass on that one. "
        "I'm happy to talk about my projects, skills or availability."
    ),
    "action_request": (
        "I can't take actions like sending messages, booking, changing or deleting anything. "
        f"If you'd like to reach me, please use {CONTACT}."
    ),
    "opportunity": (
        f"Thanks for thinking of me! The best way to share an opportunity is {CONTACT}: "
        "I read every message and will get back to you."
    ),
    "greeting": (
        "Hi! I'm an AI twin: ask me about my projects, skills, experience or availability."
    ),
}


def _rx(*patterns: str) -> tuple[re.Pattern[str], ...]:
    return tuple(re.compile(p, re.IGNORECASE) for p in patterns)


GREETING = _rx(
    r"^\s*(hi|hello|hey|hiya|howdy|namaste|yo|good (morning|afternoon|evening)|thanks|"
    r"thank you|thx|cheers)( there)?[\s!.,:)]*$"
)
RESUME = _rx(r"\b(resume|résumé|cv|curriculum vitae)\b")
# Unmistakably about the assistant, never about the owner's own work.
META = _rx(
    r"\b(what|which)\b[^?]{0,30}\b(model|llm|ai|language model|gpt|engine)\b[^?]{0,20}"
    r"\b(are you|is this|powers? (you|this)|do you run on|runs? (you|this)|behind (you|this))",
    r"\bare you (an? )?(gpt|chatgpt|claude|llama|gemini|mistral|qwen|llm|bot|chatbot|"
    r"language model|real person|human)\b",
    r"\b(who|what) (built|made|created|trained|powers|programmed) (you|this (bot|assistant|"
    r"chat|chatbot|twin))\b",
    r"\bhow (are|were) you (built|made|implemented|hosted|deployed|trained|programmed)\b",
    r"\bhow is this (bot|assistant|chat|chatbot|twin|site) (built|made|implemented|hosted)\b",
    r"\bwhere (are you|is this (bot|assistant|chat|chatbot|twin)) (hosted|deployed|running)\b",
    r"\byour (system prompt|prompt|instructions|underlying model|language model|llm|"
    r"embeddings?|embedding model|retriever|knowledge base|vector (store|database|db)|"
    r"guardrails?|context window|training data)\b"
    r"(?!\s*(engineering|design|templates?|library|tuning|pipeline))",
    r"\bthis (bot|assistant|chat|chatbot|twin|ai)('s)? (stack|database|model|backend|"
    r"hosting|architecture|code|source code|prompt)\b",
)
# The question is about the owner's own work, so a `meta_probe` verdict is a model mistake.
OWNER_WORK = _rx(
    r"\b(project|projects|job|jobs|team|company|employer|internship|startup|hackathon|"
    r"thesis|client|clients)\b",
    # Past tense: "did you use X" is about the owner's work; "do you use X" may be about the
    # assistant itself and is left to the rules and the classifier.
    r"\b(did|have) you (use|used|choose|chose|pick|picked|work with|worked with)\b",
    r"\byour (experience|background|career|work)\b",
    # "What do you do?" asks about the owner's job, not about the assistant (found by make eval).
    r"\bwhat do you do( for (a )?(living|work))?\b",
    r"\bwhat (is|'s) your (job|role|profession|occupation)\b",
)
# The owner's own public repositories. "What is ai-twin?" asks what the owner built, so a
# `meta_probe` verdict on it is a model mistake: the assistant *is* the owner's project, but
# the visitor is asking about the owner's work.
OWNER_PROJECTS = _rx(
    r"\b(ai[\s-]?twin|agentforge|mcp[\s-]?servers?|observable[\s-]?ai[\s-]?service|"
    r"my[\s-]?portfolio)\b",
)
# Names the assistant or site itself: then a `meta_probe` verdict stands.
ASSISTANT_REF = _rx(
    r"\bthis (bot|assistant|chat|chatbot|twin|ai|site|website|page|widget)\b",
    r"\b(are|were) you (built|made|trained|hosted|deployed|programmed)\b",
)
ACTION = _rx(
    r"\b(send|email|e-mail|mail|text|message|call|book|schedule|cancel|delete|remove|update|"
    r"change|modify|reset|log ?in|sign ?in|sign ?up|register|subscribe|transfer|pay|buy|order|"
    r"forward|unsubscribe)\b[^.?]{0,30}\b(me|my|this|that|the|an?|his|her|their|our)\b"
)
AVAILABILITY = _rx(
    r"\b(available|availability|notice period|open to (work|roles|offers|opportunities)|"
    r"looking for (a )?(job|role|work|position)|job hunting|relocat)"
)
OPPORTUNITY = _rx(r"\b(hiring|job offer|opportunity|opening|position|recruit|freelance|contract)")
OUT_OF_SCOPE = _rx(
    r"\b(write|compose|generate|draft)\b[^.?]{0,20}\b(poem|story|essay|song|haiku|limerick|"
    r"code|function|script|program|sql|email|letter|joke)\b",
    r"\b(homework|assignment|solve|calculate|translate|capital of|weather|news|recipe|"
    r"stock price|bitcoin)\b",
)


@dataclass(frozen=True)
class ScopeDecision:
    intent: Intent
    source: Source
    model: str | None = None
    # A model `meta_probe` verdict overruled because the question is about the owner's work.
    corrected: bool = False


def _any(patterns: tuple[re.Pattern[str], ...], text: str) -> bool:
    return any(p.search(text) for p in patterns)


def about_owner_work(text: str) -> bool:
    owner = _any(OWNER_WORK, text) or _any(OWNER_PROJECTS, text)
    return owner and not _any(ASSISTANT_REF, text)


# "Who are you?" is the first thing many visitors ask: the greeting answers it (an AI twin).
IDENTITY = _rx(
    r"^\s*(hi|hello|hey)?[,!.\s]*(who|what) are you\s*[?.!]*\s*$",
    r"^\s*are you (a |an )?(bot|ai|robot|human|real( person)?|chat ?bot)\s*[?.!]*\s*$",
)


def rule_intent(text: str) -> Intent | None:
    """The intent when a rule is certain, else None (the classifier decides)."""
    if _any(GREETING, text) or _any(IDENTITY, text):
        return "greeting"
    if _any(META, text) and not about_owner_work(text):
        return "meta_probe"
    if _any(RESUME, text):
        return "resume"
    return None


def fallback_intent(text: str) -> Intent:
    """Keyword routing for when the classifier is down."""
    ordered: tuple[tuple[Intent, tuple[re.Pattern[str], ...]], ...] = (
        ("out_of_scope", OUT_OF_SCOPE),
        ("action_request", ACTION),
        ("availability", AVAILABILITY),
        ("opportunity", OPPORTUNITY),
    )
    for intent, patterns in ordered:
        if _any(patterns, text):
            return intent
    return "about_owner"


class ScopeRouter:
    def __init__(self, classifier: Classifier) -> None:
        self._classifier = classifier

    async def route(self, text: str) -> ScopeDecision:
        started = time.perf_counter()
        decision = await self._decide(text)
        logger.info(
            "scope routed",
            intent=decision.intent,
            source=decision.source,
            model=decision.model,
            corrected=decision.corrected,
            latency_ms=round((time.perf_counter() - started) * 1000, 1),
        )
        return decision

    async def _decide(self, text: str) -> ScopeDecision:
        ruled = rule_intent(text)
        if ruled is not None:
            return ScopeDecision(ruled, "rule")
        try:
            result = await self._classifier.classify(
                text, question=ROUTER_QUESTION, labels=INTENT_LABELS, tier=Tier.PUBLIC
            )
        except Exception as exc:
            logger.warning("scope router fell back to keywords", error_type=type(exc).__name__)
            return ScopeDecision(fallback_intent(text), "fallback")
        intent = _intent(result.label)
        if intent == "meta_probe" and about_owner_work(text):
            return ScopeDecision("about_owner", "model", result.model, corrected=True)
        return ScopeDecision(intent, "model", result.model)


INTENTS: frozenset[str] = frozenset(get_args(Intent))


def _intent(label: str) -> Intent:
    return cast(Intent, label) if label in INTENTS else "about_owner"


# --- profile templates -----------------------------------------------------------------------


def _join(items: list[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return f"{', '.join(items[:-1])} and {items[-1]}"


@dataclass
class ProfileTemplates:
    """Resume and availability answers from profile facts; never a model call."""

    profile: ProfileStore
    resume_available: Callable[[], Awaitable[bool]]

    async def reply(self, intent: Intent) -> str:
        if intent == "resume":
            return await self.resume()
        return await self.availability()

    async def resume(self) -> str:
        if await self.resume_available():
            return (
                "You can download my latest resume with the resume link on this page. "
                f"For anything it doesn't cover, please use {CONTACT}."
            )
        return (
            f"My resume isn't available to download right now. Please reach me through {CONTACT}."
        )

    async def availability(self) -> str:
        facts = await self.profile.get()
        parts = [f"My current status: {facts.availability.rstrip('.')}."]
        if facts.preferred_roles:
            parts.append(f"I'm most interested in {_join(facts.preferred_roles)} roles.")
        if facts.locations:
            parts.append(f"Locations: {_join(facts.locations)}.")
        parts.append(f"For anything specific, please use {CONTACT}.")
        return " ".join(parts)
