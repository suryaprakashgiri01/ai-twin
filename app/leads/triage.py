"""Who a lead is from (a `choice` classification at tier public) and how it fits the owner.

Triage goes through `FlaggedClassifier`: Jev when FF_JEV_ENABLED is on, the LLM classifier
otherwise. Visitor text is public-tier, so either may see it. A classifier failure files the lead
as `other` rather than losing it.

The fit note is deterministic: the owner's public `preferred_roles`, `locations` and published
skill tags that the opportunity text mentions. It reads "matches ..." in the email and does not
depend on a model's mood.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

import structlog

from app.db.documents import KbDocument
from app.leads.models import LeadIn
from app.services.profile import ProfileStore
from llm_orchestrator import Tier
from llm_orchestrator.classify import Classifier

logger = structlog.get_logger(__name__)

TRIAGE_LABELS: Mapping[str, str] = {
    "recruiter": "an agency or in-house recruiter or talent partner reaching out about a job",
    "hiring_manager": "a manager or founder hiring directly for their own team",
    "client": "someone who wants to pay for freelance, contract or consulting work",
    "collaboration": "an unpaid collaboration, open-source, speaking, writing or research idea",
    "spam": "spam, marketing, SEO or link selling, scams, or nonsense with no real opportunity",
    "other": "anything else",
}
TRIAGE_QUESTION = "Who sent this message to the owner's portfolio, and what do they want?"
SKILL_LIMIT = 200
# Words that say nothing about fit on their own ("Backend Engineer" matches on "backend").
GENERIC = frozenset(
    {
        "engineer",
        "engineering",
        "developer",
        "development",
        "senior",
        "junior",
        "staff",
        "principal",
        "lead",
        "role",
        "roles",
        "and",
        "the",
        "of",
        "for",
        "in",
    }
)
_WORD = re.compile(r"[a-z0-9+#.]+")


@dataclass(frozen=True)
class Triage:
    label: str
    confidence: float | None = None
    model: str | None = None


def triage_text(lead: LeadIn) -> str:
    parts = [f"Role: {lead.role}", f"Company: {lead.company}"]
    if lead.employment_type:
        parts.append(f"Type: {lead.employment_type}")
    parts.append(f"Message: {lead.message}")
    return "\n".join(parts)


async def triage(classifier: Classifier, lead: LeadIn) -> Triage:
    try:
        result = await classifier.classify(
            triage_text(lead), question=TRIAGE_QUESTION, labels=TRIAGE_LABELS, tier=Tier.PUBLIC
        )
    except Exception as exc:
        logger.warning("lead triage failed", error_type=type(exc).__name__)
        return Triage("other")
    label = result.label if result.label in TRIAGE_LABELS else "other"
    return Triage(label, round(result.confidence, 3), result.model)


# --- fit note --------------------------------------------------------------------------------


@dataclass(frozen=True)
class FitFacts:
    preferred_roles: Sequence[str] = ()
    locations: Sequence[str] = ()
    skills: Sequence[str] = field(default_factory=tuple)


class FitFactsSource(Protocol):
    async def facts(self) -> FitFacts: ...


class PublicFitFacts:
    """Profile roles and locations, plus the tags of published documents."""

    def __init__(self, profile: ProfileStore) -> None:
        self._profile = profile

    async def facts(self) -> FitFacts:
        profile = await self._profile.get()
        try:
            skills = await _published_tags()
        except Exception as exc:
            logger.warning("published tags unavailable", error_type=type(exc).__name__)
            skills = []
        return FitFacts(profile.preferred_roles, profile.locations, skills)


async def _published_tags() -> list[str]:
    query: dict[str, Any] = {"published": True, "deleted_at": None}
    tags = await KbDocument.get_pymongo_collection().distinct("tags", query)
    return sorted({str(t) for t in tags if t})[:SKILL_LIMIT]


def _words(text: str) -> set[str]:
    return set(_WORD.findall(text.casefold()))


def _mentioned(phrase: str, words: set[str]) -> bool:
    wanted = _words(phrase) - GENERIC
    return bool(wanted) and wanted <= words


def fit_note(lead: LeadIn, facts: FitFacts) -> str:
    words = _words(" ".join([lead.role, lead.message, *lead.details().values()]))
    groups = [
        ("role", [r for r in facts.preferred_roles if _mentioned(r, words)]),
        ("location", [loc for loc in facts.locations if _mentioned(loc, words)]),
        ("skills", [s for s in facts.skills if _mentioned(s, words)]),
    ]
    found = [f"{name}: {', '.join(items)}" for name, items in groups if items]
    if not found:
        return "No overlap with preferred roles, locations or published skills."
    return "Matches " + "; ".join(found) + "."
