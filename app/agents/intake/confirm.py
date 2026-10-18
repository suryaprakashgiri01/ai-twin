"""Reading the owner's reply to a draft: yes, no, an edit, or something new.

Rules first, so the common answers never need a model. Anything the rules can't place goes to
the local model (one structured call); if that fails too the draft stays pending and the owner
is asked again. A delete also needs the target's title or company retyped exactly.
"""

from __future__ import annotations

import re
from typing import Literal

import structlog
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel

from app.agents.prompts import intake as prompts
from llm_orchestrator import Orchestrator, OrchestratorError, Role, Tier

logger = structlog.get_logger(__name__)

Decision = Literal["yes", "no", "edit", "new"]

YES_WORDS = frozenset(
    {
        "yes",
        "y",
        "yep",
        "yeah",
        "yup",
        "sure",
        "ok",
        "okay",
        "confirm",
        "confirmed",
        "correct",
        "approve",
        "approved",
        "go",
        "proceed",
        "save",
        "do",
        "please",
        "right",
    }
)
YES_PHRASES = frozenset(
    {
        "go ahead",
        "do it",
        "save it",
        "looks good",
        "sounds good",
        "that's right",
        "thats right",
        "all good",
        "ship it",
        "yes please",
        "please do",
        "publish it",
        "add it",
        "delete it",
    }
)
NO_WORDS = frozenset({"no", "n", "nope", "nah", "cancel", "discard", "stop", "abort", "drop"})
NO_PHRASES = frozenset({"never mind", "nevermind", "forget it", "don't", "dont", "not now"})
EDIT_VERBS = frozenset(
    {
        "change",
        "set",
        "make",
        "update",
        "use",
        "rename",
        "replace",
        "correct",
        "fix",
        "actually",
        "instead",
        "edit",
        "add",
        "remove",
        "put",
    }
)
EDIT_MARKERS = ("should be", "instead", " not ", " but ", "change", "wrong")
SHORT = 4

_PUNCT = re.compile(r"[^\w\s']+")
# Quotes and end punctuation, curly ones included.
_QUOTES = "\"'`\u201c\u201d\u2018\u2019"
_TRIM = re.compile(rf"^[\s{_QUOTES}.,!?]+|[\s{_QUOTES}.,!?]+$")


class ConfirmReply(BaseModel):
    decision: Decision


def _words(text: str) -> list[str]:
    return _PUNCT.sub(" ", text.casefold()).split()


def classify_rules(text: str) -> Decision | None:
    words = _words(text)
    if not words:
        return None
    joined = " ".join(words)
    padded = f" {joined} "
    if joined in YES_PHRASES:
        return "yes"
    if joined in NO_PHRASES:
        return "no"
    edit_marked = any(marker in padded for marker in EDIT_MARKERS)
    if words[0] in EDIT_VERBS or (edit_marked and len(words) > 1):
        return "edit"
    if words[0] in YES_WORDS and len(words) <= SHORT:
        return "yes"
    if words[0] in NO_WORDS and len(words) <= SHORT:
        return "no"
    return None


async def classify_reply(
    text: str, summary: str, orchestrator: Orchestrator, tier: Tier
) -> Decision | None:
    """Rules, then the local model. None means ask the owner again."""
    decided = classify_rules(text)
    if decided is not None:
        return decided
    messages = [
        SystemMessage(content=prompts.CONFIRM_V1),
        HumanMessage(content=f"Draft:\n{summary}\n\nOwner's reply: {text}"),
    ]
    try:
        result = await orchestrator.structured_call(
            Role.ROUTER, tier, ConfirmReply, messages, privileged=True
        )
    except OrchestratorError as exc:
        logger.warning("intake confirm fell back to asking", error_type=type(exc).__name__)
        return None
    return result.value.decision


def retype_matches(expected: str, typed: str) -> bool:
    """The second confirmation: the same text, ignoring case, spacing and quotes."""

    def norm(value: str) -> str:
        return " ".join(_TRIM.sub("", value).casefold().split())

    return bool(norm(expected)) and norm(expected) == norm(typed)
