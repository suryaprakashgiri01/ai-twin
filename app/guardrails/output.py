"""The public output guard: every generated answer is checked whole, before any of it is sent.

1. Canary scan: a marker-shaped token whose SHA-256 is a known canary (critical).
2. Identifier scan: the rule recognisers (PAN, Aadhaar, UAN, IFSC, account, passport, DOB, roll
   number, Indian mobile) plus e-mail. Published contact details are allowlisted.
3. Disclosure scan: self-referential statements about the assistant's model, provider, prompt
   or stack, and word n-gram overlap with the system prompt (prompt leak).
4. Grounding: the judge role (public tier; Jev when flagged) says whether the context supports
   every claim.

1-3 fail closed: if a check cannot run, the answer is blocked. Grounding fails open: a judge
outage sends the answer and records `grounding_fail_open`; a `false` verdict blocks. Verdicts
carry rule names only, never the matched text.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol

import structlog
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel

from app.agents.prompts.public import GROUNDING_V1
from app.guardrails.canary_hashes import CanaryHashSource, leaked
from app.guardrails.identifiers import OUTPUT_RULES, rule_spans
from llm_orchestrator import Orchestrator, Role, Tier
from llm_orchestrator.classify import Classifier

logger = structlog.get_logger(__name__)

Check = Literal["canary", "identifier", "disclosure", "prompt_leak", "grounding"]
Severity = Literal["low", "medium", "high", "critical"]
Grounding = Literal["supported", "unsupported", "unavailable", "skipped"]

CHECKS: tuple[Check, ...] = ("canary", "identifier", "disclosure", "prompt_leak")

# Word n-grams shared with the system prompt; this many distinct ones is a leak.
LEAK_NGRAM = 6
LEAK_MIN_SHARED = 3


@dataclass(frozen=True)
class OutputVerdict:
    passed: bool
    check: Check | None = None
    rule: str | None = None
    severity: Severity | None = None
    grounding: Grounding = "skipped"
    notes: tuple[str, ...] = ()

    def summary(self) -> dict[str, object]:
        return {
            "passed": self.passed,
            "check": self.check,
            "rule": self.rule,
            "grounding": self.grounding,
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class Blocked:
    rule: str
    severity: Severity


# --- disclosure ----------------------------------------------------------------------------

_PROVIDERS = (
    r"openai|anthropic|groq|cerebras|gemini|google deepmind|meta ai|mistral|ollama|openrouter|"
    r"gpt-?\d[\w.-]*|gpt|chatgpt|llama[- ]?\d[\w.-]*|llama|claude|qwen|gemma|prompt guard"
)
DISCLOSURE: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (name, re.compile(pattern, re.IGNORECASE))
    for name, pattern in (
        (
            "self_construction",
            r"\b(i am|i'm|i was|this (assistant|chat|chatbot|bot|twin) (is|was))\s+"
            r"([\w-]+\s+){0,3}(powered by|running on|runs on|built (on|with|using)|based on|"
            r"trained by|developed by|created by|made by|hosted on|deployed on|served by)\b",
        ),
        ("ai_model_claim", r"\bas an? (ai|artificial intelligence) (language )?model\b"),
        ("ai_model_claim", r"\b(i am|i'm) an? (large )?language model\b"),
        (
            "own_prompt",
            r"\bmy (system prompt|prompt|instructions|guidelines|system message|underlying "
            r"model|language model|knowledge cutoff|context window)\b"
            r"(?!\s*(engineering|design|templates?|library|tuning))",
        ),
        (
            "own_prompt",
            r"\b(i was|i've been|i have been|i am|i'm) (prompted|programmed|configured) to\b",
        ),
        (
            "provider_claim",
            rf"\b({_PROVIDERS})\b[^.\n]{{0,30}}\b(powers|runs|hosts|serves|generated|generates|"
            r"wrote|writes)\s+(me|this (answer|reply|response|assistant|chat))\b",
        ),
        ("provider_claim", rf"\b(i am|i'm|i run on)\s+(an? )?({_PROVIDERS})\b"),
    )
)


def disclosure_rule(text: str) -> str | None:
    for name, pattern in DISCLOSURE:
        if pattern.search(text):
            return name
    return None


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", text.lower())


def ngrams(text: str, n: int = LEAK_NGRAM) -> set[tuple[str, ...]]:
    words = _words(text)
    return {tuple(words[i : i + n]) for i in range(len(words) - n + 1)}


def prompt_leak(answer: str, prompt_ngrams: set[tuple[str, ...]]) -> bool:
    return len(ngrams(answer) & prompt_ngrams) >= LEAK_MIN_SHARED


# --- identifiers -----------------------------------------------------------------------------


def _key(value: str) -> str:
    return re.sub(r"[\s().+-]", "", value).lower()


def identifier_entities(text: str, allowlist: Iterable[str]) -> list[str]:
    """Entity names of identifiers in `text`, other than the published contact details."""
    allowed = {_key(a) for a in allowlist if a.strip()}
    found = {
        entity
        for start, end, entity, _score in rule_spans(text, OUTPUT_RULES)
        if _key(text[start:end]) not in allowed
    }
    return sorted(found)


# --- grounding -------------------------------------------------------------------------------


class GroundingJudge(Protocol):
    async def supported(self, *, question: str, context: str, answer: str) -> bool: ...


class GroundingVerdict(BaseModel):
    supported: bool


def _grounding_body(question: str, context: str, answer: str) -> str:
    return f"Context:\n{context}\n\nQuestion: {question}\n\nAnswer:\n{answer}"


class OrchestratorJudge:
    """The judge role at the public tier: every input here is public text."""

    def __init__(self, orchestrator: Orchestrator) -> None:
        self._orchestrator = orchestrator

    async def supported(self, *, question: str, context: str, answer: str) -> bool:
        result = await self._orchestrator.structured_call(
            Role.JUDGE,
            Tier.PUBLIC,
            GroundingVerdict,
            [
                SystemMessage(content=GROUNDING_V1),
                HumanMessage(content=_grounding_body(question, context, answer)),
            ],
        )
        return result.value.supported


GROUNDING_LABELS = {
    "supported": "every claim in the answer is supported by the context",
    "unsupported": "the answer makes a claim the context does not support",
}


class ClassifierJudge:
    """Grounding as a two-label classification (Jev when `FF_JEV_ENABLED` is on)."""

    def __init__(self, classifier: Classifier) -> None:
        self._classifier = classifier

    async def supported(self, *, question: str, context: str, answer: str) -> bool:
        result = await self._classifier.classify(
            _grounding_body(question, context, answer),
            question="Is every factual claim in the answer supported by the context?",
            labels=GROUNDING_LABELS,
            tier=Tier.PUBLIC,
        )
        return result.label == "supported"


@dataclass
class SwitchedJudge:
    """`flagged` while `use_flagged()` is true (Jev on and configured), `default` otherwise."""

    default: GroundingJudge
    flagged: GroundingJudge
    use_flagged: Callable[[], bool]

    async def supported(self, *, question: str, context: str, answer: str) -> bool:
        judge = self.flagged if self.use_flagged() else self.default
        return await judge.supported(question=question, context=context, answer=answer)


# --- the guard -------------------------------------------------------------------------------


@dataclass
class OutputGuard:
    canaries: CanaryHashSource
    system_prompts: Sequence[str]
    allowlist: Sequence[str] = ()
    judge: GroundingJudge | None = None
    _prompt_ngrams: set[tuple[str, ...]] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._prompt_ngrams = set().union(*(ngrams(p) for p in self.system_prompts))

    async def check(self, answer: str, *, question: str, context: str) -> OutputVerdict:
        started = time.perf_counter()
        verdict = await self._check(answer, question=question, context=context)
        logger.info(
            "output guard",
            passed=verdict.passed,
            check=verdict.check,
            rule=verdict.rule,
            severity=verdict.severity,
            grounding=verdict.grounding,
            notes=list(verdict.notes),
            chars=len(answer),
            latency_ms=round((time.perf_counter() - started) * 1000, 1),
        )
        return verdict

    async def _check(self, answer: str, *, question: str, context: str) -> OutputVerdict:
        for check in CHECKS:
            try:
                blocked = await self._run(check, answer)
            except Exception as exc:
                logger.error("output check failed", check=check, error_type=type(exc).__name__)
                blocked = Blocked(f"{check}_check_failed", "high")
            if blocked is not None:
                return OutputVerdict(False, check, blocked.rule, blocked.severity)
        return await self._grounding(answer, question=question, context=context)

    async def _run(self, check: Check, answer: str) -> Blocked | None:
        if check == "canary":
            hit = leaked(answer, await self.canaries.hashes())
            return Blocked("canary", "critical") if hit else None
        if check == "identifier":
            entities = identifier_entities(answer, self.allowlist)
            return Blocked(f"identifier:{entities[0]}", "high") if entities else None
        if check == "disclosure":
            rule = disclosure_rule(answer)
            return Blocked(f"disclosure:{rule}", "high") if rule else None
        return Blocked("prompt_leak", "high") if prompt_leak(answer, self._prompt_ngrams) else None

    async def _grounding(self, answer: str, *, question: str, context: str) -> OutputVerdict:
        if self.judge is None or not context.strip():
            return OutputVerdict(True, grounding="skipped")
        try:
            supported = await self.judge.supported(
                question=question, context=context, answer=answer
            )
        except Exception as exc:
            logger.warning("grounding judge unavailable", error_type=type(exc).__name__)
            return OutputVerdict(True, grounding="unavailable", notes=("grounding_fail_open",))
        if not supported:
            return OutputVerdict(
                False, "grounding", "grounding_unsupported", "medium", grounding="unsupported"
            )
        return OutputVerdict(True, grounding="supported")
