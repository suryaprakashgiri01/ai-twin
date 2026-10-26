"""Tags for documents ingested without any: one short model call at the document's tier.

The classifier role is used (small and fast), allowed at the document's own tier, so a
confidential document is tagged by a local model. Quarantined and restricted documents are
never tagged: no model reads them. Failure leaves the tags empty; it never fails ingestion.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import structlog
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from llm_orchestrator import Orchestrator, OrchestratorError, Role, Tier

logger = structlog.get_logger(__name__)

MAX_TAGS = 6
TAG_CHARS = 30
SOURCE_CHARS = 3000
_NOT_TAG = re.compile(r"[^a-z0-9+#.]+")

TAG_PROMPT_V1 = """Suggest 3 to 6 short tags for this document from the owner's career vault: \
technologies, skills, domains, companies or the kind of document (for example "python", \
"rag", "system-design", "behavioural", "acme"). Lowercase, one or two words each. Keep \
placeholders such as [PERSON] out of the tags."""


class TagSet(BaseModel):
    tags: list[str] = Field(default_factory=list, max_length=12)


def clean(tags: list[str]) -> list[str]:
    """Lowercase kebab-case, no placeholders, no repeats, at most MAX_TAGS."""
    out: list[str] = []
    for raw in tags:
        if "[" in raw:
            continue
        tag = _NOT_TAG.sub("-", raw.strip().lower()).strip("-.")[:TAG_CHARS]
        if tag and tag not in out:
            out.append(tag)
    return out[:MAX_TAGS]


@dataclass
class Tagger:
    orchestrator: Orchestrator

    async def tags(self, title: str, markdown: str, tier: Tier) -> list[str]:
        if tier >= Tier.RESTRICTED or not markdown.strip():
            return []
        messages = [
            SystemMessage(content=TAG_PROMPT_V1),
            HumanMessage(content=f"Title: {title}\n\n{markdown[:SOURCE_CHARS]}"),
        ]
        try:
            result = await self.orchestrator.structured_call(
                Role.CLASSIFIER, tier, TagSet, messages, privileged=True, max_tokens=120
            )
        except OrchestratorError as exc:
            logger.warning("tags skipped", error_type=type(exc).__name__, tier=tier.label)
            return []
        tags = clean(result.value.tags)
        logger.info("tags written", model=result.model_id, tier=tier.label, count=len(tags))
        return tags
