"""answer-studio T3: document cards are written at the document's tier and indexed beside it."""

from __future__ import annotations

from app.kb.ingestion.cards import CARD_ORDINAL, CardWriter, clip
from llm_orchestrator import Tier
from tests.envelope import result
from tests.ingestion import persona
from tests.ingestion.conftest import Api
from tests.ingestion.test_review_publish import call, ingest, internal_id
from tests.llm_orchestrator.fakes import Factory, inventory, orchestrator, spec

CARD = "Kavya built a token-bucket rate limiter in Go; p99 stayed under 12 ms."


def writer(*, local: bool = True) -> tuple[CardWriter, Factory]:
    specs = [spec("cloud", roles=["generator"], max_tier="private")]
    scripts: dict[str, list[object]] = {"cloud": [CARD] * 5}
    if local:
        specs.append(spec("local", roles=["generator"], max_tier="confidential", tags=["local"]))
        scripts["local"] = [CARD] * 5
    factory = Factory(scripts)
    return CardWriter(orchestrator(inventory(*specs), factory, max_attempts=1)), factory


def test_clip_keeps_whole_sentences() -> None:
    text = "One. " * 300
    clipped = clip(text, 100)
    assert len(clipped) <= 100 and clipped.endswith(".")


async def test_confidential_card_uses_only_a_local_model() -> None:
    cards, factory = writer()
    assert await cards.write("Offer", "Offer details", Tier.CONFIDENTIAL) == CARD
    assert factory.calls("local") == 1 and factory.calls("cloud") == 0
    no_local, _ = writer(local=False)
    assert await no_local.write("Offer", "Offer details", Tier.CONFIDENTIAL) is None


async def test_ingestion_indexes_a_card_and_backfill_fills_missing_ones(api: Api) -> None:
    document_id = await ingest(api, persona.note(), "project", "Rate limiter")
    internal = await internal_id(api, document_id)
    assert not [
        p for p in await api.h.index.points_for("kb_private", internal) if p.payload.get("card")
    ]  # the harness pipeline has no card writer yet
    api.h.pipeline.cards, _ = writer()
    response = await call(api, "POST", "/documents/cards", json={})
    assert response.status_code == 200
    assert result(response) == {"checked": 1, "written": 1, "skipped": 0}
    [point] = [
        p for p in await api.h.index.points_for("kb_private", internal) if p.payload.get("card")
    ]
    assert point.payload["ordinal"] == CARD_ORDINAL and point.payload["text"] == CARD
    doc = result(await call(api, "GET", f"/documents/{document_id}"))
    assert doc["card"] == CARD
    again = result(await call(api, "POST", "/documents/cards", json={}))
    assert again == {"checked": 0, "written": 0, "skipped": 0}
    assert any(e["action"] == "document.cards" for e in api.audit.entries)
