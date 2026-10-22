"""Turn ranked hits into the context a generator sees: budgeted, numbered and tier-tracked.

Citations are numbered per document in order of first appearance, so two chunks of one note
share a number. The context's tier is the highest tier among the chunks actually included;
chunks above `ceiling` are withheld and counted, never silently dropped.
"""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import BaseModel, Field, field_serializer, field_validator

from app.api.envelope import ApiModel
from app.kb.retrieval.retriever import Hit
from llm_orchestrator import Tier

# A rough, model-independent token estimate: ~4 characters per token for English prose.
CHARS_PER_TOKEN = 4


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // CHARS_PER_TOKEN)


class Source(ApiModel):
    """One cited document, as listed in the `sources` event."""

    index: int
    document_id: str
    title: str
    category: str
    tier: Tier

    @field_serializer("tier")
    def _tier_label(self, tier: Tier) -> str:
        return tier.label

    @field_validator("tier", mode="before")
    @classmethod
    def _parse_tier(cls, value: object) -> Tier:
        # Serialised sources (the answer cache) carry the label, not the number.
        return Tier.parse(value)


class ContextChunk(BaseModel):
    citation: int
    hit: Hit


class BuiltContext(BaseModel):
    chunks: list[ContextChunk] = Field(default_factory=list)
    sources: list[Source] = Field(default_factory=list)
    # Highest tier among included chunks; None when nothing was included.
    tier: Tier | None = None
    # Documents left out because their tier is above the ceiling.
    withheld: int = 0
    tokens: int = 0

    @property
    def empty(self) -> bool:
        return not self.chunks

    def render(self) -> str:
        """`[n] title (category)` headers followed by chunk text, in rank order; cards say so.

        The category tells the generator an employer project (`company-projects`) from a
        personal repository (`project`)."""
        return "\n\n".join(
            f"[{c.citation}] {c.hit.title} ({c.hit.category or 'uncategorised'}"
            f"{', document summary' if c.hit.card else ''})\n{c.hit.text}"
            for c in self.chunks
        )


def build_context(hits: Sequence[Hit], *, budget_tokens: int, ceiling: Tier) -> BuiltContext:
    """Add hits best first until the budget is spent; withhold any above `ceiling`."""
    built = BuiltContext()
    numbers: dict[str, int] = {}
    withheld: set[str] = set()
    for hit in hits:
        if hit.tier > ceiling:
            withheld.add(hit.document_id)
            continue
        cost = estimate_tokens(hit.text)
        if built.chunks and built.tokens + cost > budget_tokens:
            continue
        if hit.document_id not in numbers:
            numbers[hit.document_id] = len(numbers) + 1
            built.sources.append(
                Source(
                    index=numbers[hit.document_id],
                    document_id=hit.document_id,
                    title=hit.title,
                    category=hit.category,
                    tier=hit.tier,
                )
            )
        built.chunks.append(ContextChunk(citation=numbers[hit.document_id], hit=hit))
        built.tokens += cost
        built.tier = hit.tier if built.tier is None else max(built.tier, hit.tier)
    built.withheld = len(withheld)
    return built
