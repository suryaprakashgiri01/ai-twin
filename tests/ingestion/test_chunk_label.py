"""Ingestion T6: Markdown chunking and labelling (rules, confidential classifier, max-merge)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest
from pydantic import SecretStr

from app.core.config import DEFAULT_MODEL_INVENTORY
from app.kb.ingestion.chunking import chunk_markdown, count_tokens
from app.kb.ingestion.labelling import Labeller, rule_label
from app.kb.ingestion.models import IngestItem
from llm_orchestrator import (
    AllModelsFailedError,
    Inventory,
    Orchestrator,
    OrchestratorSettings,
    ProviderSecrets,
    Role,
    Tier,
)
from llm_orchestrator.classify.base import Classification
from tests.ingestion import persona

# --- chunking ---------------------------------------------------------------------------------


def long_doc() -> str:
    para = " ".join(f"word{i}" for i in range(150))
    return "\n".join(
        [
            "Intro line before any heading.",
            "# Projects",
            "## Rate limiter",
            para,
            "",
            para,
            "",
            para,
            "## Search",
            "Short section.",
            "# Notes",
            "### Deep heading",
            "Deep text.",
        ]
    )


def test_chunks_keep_heading_context_and_respect_target() -> None:
    chunks = chunk_markdown(long_doc(), title="Portfolio", target=200, overlap=30)
    assert [c.ordinal for c in chunks] == list(range(len(chunks)))
    assert chunks[0].text.startswith("Portfolio\n\nIntro line")
    rate = [c for c in chunks if c.heading == "Portfolio > Projects > Rate limiter"]
    assert len(rate) >= 2
    assert all(count_tokens(c.text) <= 200 + 30 for c in chunks)
    assert any(c.heading == "Portfolio > Projects > Search" for c in chunks)
    assert chunks[-1].heading == "Portfolio > Notes > Deep heading"


def test_consecutive_chunks_overlap() -> None:
    chunks = chunk_markdown(long_doc(), title="P", target=120, overlap=20)
    rate = [c.text for c in chunks if c.heading.endswith("Rate limiter")]
    first_tail = rate[0].split()[-5:]
    assert " ".join(first_tail) in rate[1]


def test_title_heading_not_repeated_and_empty_text() -> None:
    chunks = chunk_markdown("# Notes\n\nhello", title="Notes")
    assert chunks[0].text == "Notes\n\nhello"
    assert chunk_markdown("   \n\n", title="x") == []
    with pytest.raises(ValueError):
        chunk_markdown("x", title="t", target=50, overlap=50)


# --- labelling --------------------------------------------------------------------------------


def item(category: str = "experience", **extra: Any) -> IngestItem:
    values: dict[str, Any] = {
        "source_id": "s1",
        "source_kind": "text",
        "title": "Note",
        "category": category,
        "markdown": persona.note(),
        "redacted": True,
        **extra,
    }
    return IngestItem(**values)


class SpyClassifier:
    def __init__(self, label: str = "private", *, fail: bool = False) -> None:
        self.label = label
        self.fail = fail
        self.calls: list[dict[str, Any]] = []

    async def classify(
        self, text: str, *, question: str, labels: Mapping[str, str], tier: Tier
    ) -> Classification:
        self.calls.append({"text": text, "tier": tier, "labels": dict(labels)})
        if self.fail:
            raise AllModelsFailedError(Role.CLASSIFIER, tier, [])
        return Classification(
            label=self.label, probabilities={self.label: 1.0}, confidence=0.8, model="mock"
        )


def orchestrator(**secrets: Any) -> Orchestrator:
    inventory = Inventory.from_yaml(DEFAULT_MODEL_INVENTORY)
    return Orchestrator.from_config(
        inventory, OrchestratorSettings(secrets=ProviderSecrets(**secrets))
    )


LOCAL = {"mock_enabled": True}
CLOUD_ONLY = {"groq_api_key": SecretStr("test-not-real"), "gemini_api_key": SecretStr("t")}


async def test_classifier_runs_at_confidential_tier() -> None:
    spy = SpyClassifier("private")
    labelled = await Labeller(orchestrator(**LOCAL), spy).label(item())
    assert [c["tier"] for c in spy.calls] == [Tier.CONFIDENTIAL]
    assert set(spy.calls[0]["labels"]) == {"public", "private", "confidential"}
    assert labelled.label is not None
    assert labelled.label.by == "rules+classifier:mock"


async def test_cloud_only_means_no_classifier_call() -> None:
    spy = SpyClassifier("public")
    labelled = await Labeller(orchestrator(**CLOUD_ONLY), spy).label(item())
    assert spy.calls == []
    assert labelled.label is not None
    assert labelled.label.by == "rules"
    assert labelled.label.tier == "private"


@pytest.mark.parametrize(
    ("category", "model_says", "final"),
    [
        ("experience", "public", "private"),  # a model can never lower the tier
        ("experience", "confidential", "confidential"),  # but it may raise it
        ("compensation", "public", "confidential"),
        ("blog", "public", "public"),
        ("blog", "private", "private"),
    ],
)
async def test_final_tier_is_the_maximum(category: str, model_says: str, final: str) -> None:
    labelled = await Labeller(orchestrator(**LOCAL), SpyClassifier(model_says)).label(
        item(category)
    )
    assert labelled.label is not None
    assert labelled.label.tier == final
    assert Tier.parse(labelled.label.tier) >= Tier.parse(labelled.rule_tier)


@pytest.mark.parametrize("category", ["id-document", "marksheet"])
async def test_restricted_text_never_reaches_a_model(category: str) -> None:
    spy = SpyClassifier("public")
    labelled = await Labeller(orchestrator(**LOCAL), spy).label(item(category))
    assert spy.calls == []
    assert labelled.label is not None
    assert labelled.label.tier == "restricted"


async def test_classifier_failure_keeps_the_rule_label() -> None:
    labelled = await Labeller(orchestrator(**LOCAL), SpyClassifier(fail=True)).label(item())
    assert labelled.label is not None
    assert labelled.label.by == "rules"


async def test_labelling_refuses_unredacted_text() -> None:
    with pytest.raises(RuntimeError):
        await Labeller(orchestrator(**LOCAL), SpyClassifier()).label(item(redacted=False))


def test_rule_floors_and_hints() -> None:
    assert rule_label(item("blog"))[0] == Tier.PUBLIC
    assert rule_label(item("blog", redaction_report={"PAN": 1}))[0] == Tier.PRIVATE
    assert rule_label(item("blog", tier_hint="confidential"))[0] == Tier.CONFIDENTIAL
    assert rule_label(item("compensation", tier_hint="public"))[0] == Tier.CONFIDENTIAL
    assert rule_label(item("project", source_kind="github"))[0] == Tier.PUBLIC
    assert rule_label(item("anything-else"))[0] == Tier.PRIVATE
    assert "suggest public" in rule_label(item("resume"))[1]


@pytest.mark.parametrize(
    ("category", "tier"),
    [
        ("offer", Tier.CONFIDENTIAL),
        ("assessment", Tier.PRIVATE),
        ("job-description", Tier.PRIVATE),
        ("achievement", Tier.PRIVATE),
        ("testimonial", Tier.PRIVATE),
        ("learning", Tier.PRIVATE),
    ],
)
def test_new_categories_have_rules(category: str, tier: Tier) -> None:
    got, reason = rule_label(item(category))
    assert got is tier
    assert "default" not in reason
