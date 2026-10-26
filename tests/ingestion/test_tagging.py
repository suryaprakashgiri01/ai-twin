"""answer-studio T11: documents ingested without tags get a few from a short model call."""

from __future__ import annotations

import json

from app.kb.ingestion.tagging import Tagger, clean
from tests.envelope import result
from tests.ingestion import persona
from tests.ingestion.conftest import Api
from tests.ingestion.test_review_publish import call, ingest
from tests.llm_orchestrator.fakes import Factory, inventory, orchestrator, spec


def test_clean_normalises_and_drops_placeholders() -> None:
    raw = ["Python", "System Design", "[PERSON]", "python", "RAG!", "a" * 50, "x", "y", "z"]
    assert clean(raw) == ["python", "system-design", "rag", "a" * 30, "x", "y"]


async def test_untagged_documents_are_tagged_and_owner_tags_kept(api: Api) -> None:
    reply = json.dumps({"tags": ["Go", "Rate Limiting", "[PERSON]"]})
    factory = Factory({"tagger": [reply] * 5})
    api.h.pipeline.tagger = Tagger(
        orchestrator(
            inventory(spec("tagger", roles=["classifier"], max_tier="confidential")), factory
        )
    )
    untagged = await ingest(api, persona.note(), "project", "Rate limiter")
    assert result(await call(api, "GET", f"/documents/{untagged}"))["tags"] == [
        "go",
        "rate-limiting",
    ]
    response = await api.client.post(
        "/admin/v1/ingest/text",
        json={"title": "Raft", "body": "Notes on raft.", "category": "learning", "tags": ["raft"]},
        headers=api.headers,
    )
    assert response.status_code == 202
    await api.wait()
    tagged = [d for d in api.h.store.documents.values() if d.title == "Raft"]
    assert tagged[0].tags == ["raft"]
    assert factory.calls("tagger") == 1


async def test_new_categories_are_accepted_with_their_tiers(api: Api) -> None:
    tiers = {}
    for category in ("company-projects", "question-bank", "mixed", "other"):
        # Distinct bodies: the same text twice is one source.
        body = f"{persona.note()}\n\nFiled as {category}."
        document_id = await ingest(api, body, category, f"Doc {category}")
        tiers[category] = result(await call(api, "GET", f"/documents/{document_id}"))["tier"]
    assert tiers == {
        "company-projects": "confidential",
        "question-bank": "private",
        "mixed": "private",
        "other": "private",
    }
