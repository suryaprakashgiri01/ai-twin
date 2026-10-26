"""Ingestion T9: review queue, approve, publish/unpublish, relabel, release, delete, audit."""

from __future__ import annotations

from typing import Any

from app.services.answer_cache import public_version
from tests.envelope import result
from tests.ingestion import persona
from tests.ingestion.conftest import Api

BASE = "/admin/v1"
# One paragraph: one chunk (plus a canary for private and confidential tiers).
BODY = "Built a cache layer that cut p99 latency in half for the search service."


async def ingest(api: Api, body: str, category: str, title: str = "Doc") -> str:
    response = await api.client.post(
        f"{BASE}/ingest/text",
        json={"title": title, "body": body, "category": category},
        headers=api.headers,
    )
    assert response.status_code == 202
    await api.wait()
    source_id = result(response)["sourceId"]
    status = result(await api.client.get(f"{BASE}/sources/{source_id}", headers=api.headers))
    [document_id] = status["documentIds"]
    return str(document_id)


async def call(api: Api, method: str, path: str, **kwargs: Any) -> Any:
    return await api.client.request(method, f"{BASE}{path}", headers=api.headers, **kwargs)


async def internal_id(api: Api, document_id: str) -> str:
    """The ObjectId vectors are keyed on, for the document the API calls `document_id`."""
    doc = await api.h.store.document_by_uid(document_id)
    return str(doc.id) if doc else document_id


async def counts(api: Api, document_id: str) -> dict[str, int]:
    internal = await internal_id(api, document_id)
    return {c: await api.h.index.count(c, internal) for c in api.h.index.collections}


def review_items(response: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = result(response)["listOfItems"]
    return found


def actions(api: Api) -> list[str]:
    return [e["action"] for e in api.audit.entries if e["action"].startswith("document.")]


async def test_review_lists_pending_and_quarantined_newest_first(api: Api) -> None:
    pending = await ingest(api, persona.note(), "experience", "First")
    quarantined = await ingest(api, "Fixed FAKEPROJ-12 today.", "work-note", "Second")
    queue = review_items(await call(api, "GET", "/review"))
    assert [i["id"] for i in queue] == [quarantined, pending]
    first = queue[1]
    assert first["label"]["reason"] and first["label"]["tier"] == "private"
    assert queue[0]["quarantineRules"] == ["ticket-keys"]
    assert len(review_items(await call(api, "GET", "/review?pageSize=1"))) == 1


async def test_publish_requires_public_and_approved(api: Api) -> None:
    private = await ingest(api, persona.note(), "experience")
    refused = await call(api, "POST", f"/documents/{private}/publish")
    assert refused.status_code == 409
    assert "public" in refused.json()["message"]
    await call(api, "POST", f"/documents/{private}/approve")
    assert (await call(api, "POST", f"/documents/{private}/publish")).status_code == 409

    api.h.classifier.label = "public"
    public = await ingest(api, "My blog post on caching.", "blog")
    unapproved = await call(api, "POST", f"/documents/{public}/publish")
    assert unapproved.status_code == 409
    assert "Approve" in unapproved.json()["message"]
    assert await api.h.index.count("kb_public") == 0


async def test_publish_copies_non_canary_chunks_and_unpublish_delete_remove_them(
    api: Api,
) -> None:
    document_id = await ingest(api, BODY, "experience")
    assert (await counts(api, document_id))["kb_private"] == 2  # one chunk + one canary
    patched = await call(api, "PATCH", f"/documents/{document_id}", json={"tier": "public"})
    assert patched.status_code == 200
    assert result(patched)["label"]["by"] == "owner"
    assert api.h.store.canaries == {}  # public documents carry no canary
    assert (await call(api, "POST", f"/documents/{document_id}/approve")).status_code == 200

    published = await call(api, "POST", f"/documents/{document_id}/publish")
    assert published.status_code == 200
    assert result(published)["chunks"] == 1
    public_points = await api.h.index.points_for("kb_public", await internal_id(api, document_id))
    assert [p.payload["published"] for p in public_points] == [True]
    assert not any(p.payload["canary"] for p in public_points)
    assert (await counts(api, document_id))["kb_private"] == 1  # private copy stays
    assert all(c.published for c in api.h.store.chunks)

    unpublished = await call(api, "POST", f"/documents/{document_id}/unpublish")
    assert result(unpublished)["published"] is False
    assert await api.h.index.count("kb_public") == 0

    await call(api, "POST", f"/documents/{document_id}/publish")
    assert await api.h.index.count("kb_public") == 1
    deleted = await call(api, "DELETE", f"/documents/{document_id}")
    assert result(deleted) == {"id": document_id}
    assert await counts(api, document_id) == {"kb_public": 0, "kb_private": 0, "kb_confidential": 0}
    assert (await call(api, "GET", f"/documents/{document_id}")).status_code == 404
    assert review_items(await call(api, "GET", "/review")) == []
    assert actions(api) == [
        "document.update",
        "document.approve",
        "document.publish",
        "document.unpublish",
        "document.publish",
        "document.delete",
    ]


async def test_relabel_moves_vectors_and_resets_approval(api: Api) -> None:
    document_id = await ingest(api, BODY, "experience")
    await call(api, "POST", f"/documents/{document_id}/approve")
    moved = await call(
        api, "PATCH", f"/documents/{document_id}", json={"tier": "confidential", "title": "New"}
    )
    body = result(moved)
    assert body["tier"] == "confidential" and body["reviewStatus"] == "pending"
    assert body["title"] == "New"
    assert await counts(api, document_id) == {"kb_public": 0, "kb_private": 0, "kb_confidential": 2}
    points = await api.h.index.points_for("kb_confidential", await internal_id(api, document_id))
    assert {p.payload["tier"] for p in points} == {"confidential"}
    assert {c.tier for c in api.h.store.chunks} == {"confidential"}

    # public -> private gets a fresh canary
    await call(api, "PATCH", f"/documents/{document_id}", json={"tier": "public"})
    assert (await counts(api, document_id))["kb_private"] == 1
    await call(api, "PATCH", f"/documents/{document_id}", json={"tier": "private"})
    assert (await counts(api, document_id))["kb_private"] == 2
    assert len(api.h.store.canaries) == 1


async def test_relabel_to_restricted_and_back(api: Api) -> None:
    document_id = await ingest(api, BODY, "experience")
    restricted = await call(api, "PATCH", f"/documents/{document_id}", json={"tier": "restricted"})
    assert result(restricted)["text"] is None
    assert await counts(api, document_id) == {"kb_public": 0, "kb_private": 0, "kb_confidential": 0}
    assert api.h.store.chunks == []
    back = await call(api, "PATCH", f"/documents/{document_id}", json={"tier": "private"})
    assert back.status_code == 409


async def test_published_document_relabelled_private_is_withdrawn(api: Api) -> None:
    api.h.classifier.label = "public"
    document_id = await ingest(api, "Talk abstract about queues.", "blog")
    await call(api, "POST", f"/documents/{document_id}/approve")
    await call(api, "POST", f"/documents/{document_id}/publish")
    assert await api.h.index.count("kb_public") == 1
    await call(api, "PATCH", f"/documents/{document_id}", json={"tier": "private"})
    assert await api.h.index.count("kb_public") == 0


async def test_release_quarantined_document(api: Api) -> None:
    document_id = await ingest(api, "Fixed FAKEPROJ-12 today.", "experience")
    assert (await call(api, "POST", f"/documents/{document_id}/approve")).status_code == 409
    body = {"localOnly": False}
    released = await call(api, "POST", f"/documents/{document_id}/release", json=body)
    assert released.status_code == 200
    assert result(released)["document"]["reviewStatus"] == "pending"
    assert result(released)["document"]["localOnly"] is False
    assert result(released)["chunks"] == 2
    assert (await counts(api, document_id))["kb_private"] == 2
    again = await call(api, "POST", f"/documents/{document_id}/release")
    assert again.status_code == 409
    assert any(
        e["action"] == "document.release" and e["rules"] == ["ticket-keys"]
        for e in api.audit.entries
    )


async def test_local_only_release_is_confidential_and_never_published(api: Api) -> None:
    document_id = await ingest(api, "Fixed FAKEPROJ-12 today.", "blog")
    released = await call(api, "POST", f"/documents/{document_id}/release")
    assert result(released)["document"]["localOnly"] is True
    assert result(released)["document"]["tier"] == "confidential"
    assert (await counts(api, document_id))["kb_confidential"] >= 1
    assert (await call(api, "POST", f"/documents/{document_id}/approve")).status_code == 200
    public = await call(api, "PATCH", f"/documents/{document_id}", json={"tier": "public"})
    assert public.status_code == 409
    refused = await call(api, "POST", f"/documents/{document_id}/publish")
    assert refused.status_code == 409 and "local use only" in refused.json()["message"]
    assert any(e.get("local_only") is True for e in api.audit.entries)


async def test_patch_validation_and_unknown_documents(api: Api) -> None:
    document_id = await ingest(api, persona.note(), "experience")
    assert (await call(api, "PATCH", f"/documents/{document_id}", json={})).status_code == 422
    bad = await call(api, "PATCH", f"/documents/{document_id}", json={"tier": "secret"})
    assert bad.status_code == 422
    for path in ("/documents/nope", "/documents/6ad3501e8d52090966a06598"):
        assert (await call(api, "GET", path)).status_code == 404
    full = result(await call(api, "GET", f"/documents/{document_id}"))
    assert full["text"].startswith("# Interview")
    assert full["redactionReport"] == {}
    api.app.state.ingestion = None
    assert (await call(api, "GET", f"/documents/{document_id}")).status_code == 503


async def test_publish_unpublish_and_delete_bump_the_public_version(api: Api) -> None:
    redis = api.app.state.resources.redis
    assert await public_version(redis) == 0
    document_id = await ingest(api, BODY, "project")
    await call(api, "PATCH", f"/documents/{document_id}", json={"tier": "public"})
    await call(api, "POST", f"/documents/{document_id}/approve")
    assert await public_version(redis) == 0  # nothing public changed yet
    assert (await call(api, "POST", f"/documents/{document_id}/publish")).status_code == 200
    assert await public_version(redis) == 1
    await call(api, "PATCH", f"/documents/{document_id}", json={"title": "Renamed"})
    assert await public_version(redis) == 2
    await call(api, "POST", f"/documents/{document_id}/unpublish")
    assert await public_version(redis) == 3
    await call(api, "POST", f"/documents/{document_id}/publish")
    await call(api, "DELETE", f"/documents/{document_id}")
    assert await public_version(redis) == 5
