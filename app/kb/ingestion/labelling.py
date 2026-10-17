"""Propose a tier for a document: rules first, then a local classifier that may only raise it.

1. Category and source rules (e.g. `compensation` -> confidential, `marksheet` -> restricted).
2. Redaction results: identifiers found -> at least private.
3. The `classifier` role at tier=confidential, so only local models (Ollama/mock) are eligible:
   unlabelled text is treated as the most sensitive tier a model may see. Without an eligible
   model, or if it fails, the rule label stands with `by: rules`.

The final tier is the maximum of the rule tier and the classifier tier. A restricted rule tier
stops here: no model ever sees that text.
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog

from app.db.documents import TierLabel
from app.kb.ingestion.models import IngestItem
from llm_orchestrator import NoEligibleModelError, Orchestrator, OrchestratorError, Role, Tier
from llm_orchestrator.classify import Classifier

logger = structlog.get_logger(__name__)

CLASSIFIER_TIER = Tier.CONFIDENTIAL
EXCERPT_CHARS = 4000

CATEGORIES: tuple[str, ...] = (
    "resume",
    "project",
    "experience",
    "education",
    "certification",
    "skill",
    "interview",
    "application",
    "work-note",
    "compensation",
    "marksheet",
    "id-document",
    "profile-link",
    "blog",
    "persona",
)


@dataclass(frozen=True)
class CategoryRule:
    tier: Tier
    reason: str


CATEGORY_RULES: dict[str, CategoryRule] = {
    "compensation": CategoryRule(Tier.CONFIDENTIAL, "compensation is confidential"),
    "work-note": CategoryRule(Tier.CONFIDENTIAL, "work notes may touch employer matters"),
    "id-document": CategoryRule(Tier.RESTRICTED, "identity documents are blob-only"),
    "marksheet": CategoryRule(Tier.RESTRICTED, "marksheets are blob-only"),
    "resume": CategoryRule(Tier.PRIVATE, "resume; suggest public after review"),
    "certification": CategoryRule(Tier.PRIVATE, "certification name; suggest public after review"),
    "blog": CategoryRule(Tier.PUBLIC, "published writing"),
    "profile-link": CategoryRule(Tier.PUBLIC, "a public profile"),
}
SOURCE_RULES: dict[str, CategoryRule] = {
    "github": CategoryRule(Tier.PUBLIC, "an allowlisted public repository"),
}
DEFAULT_RULE = CategoryRule(Tier.PRIVATE, "default: private until reviewed")

LABELS: dict[str, str] = {
    "public": "fine to show anyone: published work, public profiles, general skills",
    "private": "personal or career details the owner shares selectively",
    "confidential": "salary, health, finances, third parties, or employer-internal matters",
}
QUESTION = "How sensitive is this text about the owner's career and life?"


def rule_label(item: IngestItem) -> tuple[Tier, str]:
    """The tier the rules give, and why. Pure: no model involved."""
    rule = SOURCE_RULES.get(item.source_kind) or CATEGORY_RULES.get(item.category, DEFAULT_RULE)
    tier, reasons = rule.tier, [rule.reason]
    if item.tier_hint is not None and Tier.parse(item.tier_hint) > tier:
        tier, reasons = Tier.parse(item.tier_hint), [*reasons, "owner hint"]
    if item.redaction_report and tier < Tier.PRIVATE:
        tier, reasons = Tier.PRIVATE, [*reasons, "identifiers were redacted"]
    return tier, "; ".join(reasons)


@dataclass
class Labeller:
    orchestrator: Orchestrator
    classifier: Classifier

    async def _classifier_available(self) -> bool:
        try:
            await self.orchestrator.select(Role.CLASSIFIER, CLASSIFIER_TIER)
        except NoEligibleModelError:
            return False
        return True

    async def label(self, item: IngestItem) -> IngestItem:
        if not item.redacted:
            raise RuntimeError("labelling needs redacted text")
        rule_tier, reason = rule_label(item)
        label = TierLabel(tier=rule_tier.label, reason=reason, by="rules", confidence=1.0)
        if rule_tier < Tier.RESTRICTED and await self._classifier_available():
            label = await self._with_classifier(item, rule_tier, reason) or label
        return item.model_copy(
            update={"rule_tier": rule_tier.label, "rule_reason": reason, "label": label}
        )

    async def _with_classifier(
        self, item: IngestItem, rule_tier: Tier, reason: str
    ) -> TierLabel | None:
        excerpt = f"{item.title}\n\n{item.markdown[:EXCERPT_CHARS]}"
        try:
            result = await self.classifier.classify(
                excerpt, question=QUESTION, labels=LABELS, tier=CLASSIFIER_TIER
            )
        except OrchestratorError as exc:
            logger.warning("classifier unavailable, rule label kept", error_type=type(exc).__name__)
            return None
        model_tier = Tier.parse(result.label)
        final = max(rule_tier, model_tier)
        raised = "; classifier raised it" if final > rule_tier else ""
        return TierLabel(
            tier=final.label,
            reason=f"{reason}; classifier said {model_tier.label}{raised}",
            by=f"rules+classifier:{result.model}",
            confidence=result.confidence,
        )
