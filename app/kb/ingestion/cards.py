"""Document cards: one short summary per document, indexed beside its chunks.

Overview questions ("summarise all my projects") need one line per document rather than six
chunks from whichever document ranks best. A card is written once at ingestion by a generator
allowed at the document's own tier (a local model for confidential text), from the redacted
text only. It is stored on the document and indexed as an extra point with `card: true`.
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog
from langchain_core.messages import HumanMessage, SystemMessage

from llm_orchestrator import Orchestrator, OrchestratorError, Role, Tier

logger = structlog.get_logger(__name__)

CARD_ORDINAL = 1_000_000
CARD_MAX_CHARS = 800
SOURCE_CHARS = 12_000
# The offline stand-in answers every prompt the same way; a card from it would mislead.
NO_CARD_MODELS = frozenset({"mock"})

CARD_PROMPT_V1 = """You write a short card about one document from the owner's personal \
knowledge vault. In at most 120 words, state what it is about, the owner's role, the \
technologies, and any outcomes or numbers it gives. Plain sentences, no headings, no lists, \
no preamble. Use only the document. Keep placeholders such as [PERSON] as they are."""


def card_text(title: str, summary: str) -> str:
    """The text that is embedded and shown to the generator for a card point."""
    return f"Document summary: {title}\n{summary}"


def clip(text: str, limit: int = CARD_MAX_CHARS) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text.rfind(". ", 0, limit)
    return text[: cut + 1] if cut > limit // 2 else text[:limit].rstrip() + "…"


@dataclass
class CardWriter:
    orchestrator: Orchestrator

    async def write(self, title: str, markdown: str, tier: Tier) -> str | None:
        """A card for the document, or None when no real model may read it at `tier`."""
        if not markdown.strip():
            return None
        messages = [
            SystemMessage(content=CARD_PROMPT_V1),
            HumanMessage(content=f"Title: {title}\n\n{markdown[:SOURCE_CHARS]}"),
        ]
        try:
            result = await self.orchestrator.chat(
                Role.GENERATOR, tier, messages, privileged=True, max_tokens=300
            )
        except OrchestratorError as exc:
            logger.warning("card skipped", error_type=type(exc).__name__, tier=tier.label)
            return None
        if result.model_id in NO_CARD_MODELS or not result.text.strip():
            return None
        logger.info("card written", model=result.model_id, tier=tier.label)
        return clip(result.text)
