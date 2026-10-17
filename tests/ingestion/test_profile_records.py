"""Ingestion T10: profile and records routes."""

from __future__ import annotations

from tests.ingestion import persona
from tests.ingestion.conftest import Api

BASE = "/admin/v1"


async def test_profile_round_trip_with_resume(api: Api) -> None:
    initial = await api.client.get(f"{BASE}/profile", headers=api.headers)
    assert initial.json()["availability"] == "not looking"

    files = [
        ("files", ("resume.pdf", persona.pdf_bytes(f"{persona.NAME} resume"), "application/pdf"))
    ]
    submitted = await api.client.post(
        f"{BASE}/ingest/files", files=files, data={"category": "resume"}, headers=api.headers
    )
    await api.wait()
    source_id = submitted.json()["sources"][0]["source_id"]
    status = await api.client.get(f"{BASE}/sources/{source_id}", headers=api.headers)
    [document_id] = status.json()["document_ids"]

    body = {
        "availability": "open to offers",
        "notice_period": "60 days",
        "preferred_roles": ["backend engineer"],
        "locations": ["Remote"],
        "resume_document_id": document_id,
    }
    put = await api.client.put(f"{BASE}/profile", json=body, headers=api.headers)
    assert put.status_code == 200
    profile = put.json()
    assert profile["resume_document_id"] == document_id
    assert profile["resume_blob"]["content_type"] == "application/pdf"
    assert api.audit.entries[-1]["action"] == "profile.put"

    bad = await api.client.put(
        f"{BASE}/profile",
        json={**body, "resume_document_id": "6ad3501e8d52090966a06598"},
        headers=api.headers,
    )
    assert bad.status_code == 422
    no_extra = await api.client.put(
        f"{BASE}/profile", json={**body, "surprise": 1}, headers=api.headers
    )
    assert no_extra.status_code == 422


async def test_records_crud(api: Api) -> None:
    record = {
        "kind": "application",
        "company": persona.COMPANY,
        "role": "Backend Engineer",
        "stage": "applied",
        "date": "2026-09-01T00:00:00Z",
    }
    created = await api.client.post(f"{BASE}/records", json=record, headers=api.headers)
    assert created.status_code == 201
    record_id = created.json()["id"]
    interview = {**record, "kind": "interview", "stage": "round 1", "date": "2026-09-10T00:00:00Z"}
    await api.client.post(f"{BASE}/records", json=interview, headers=api.headers)

    listed = (await api.client.get(f"{BASE}/records", headers=api.headers)).json()
    assert [r["kind"] for r in listed] == ["interview", "application"]
    only = await api.client.get(f"{BASE}/records?kind=application", headers=api.headers)
    assert [r["id"] for r in only.json()] == [record_id]

    patched = await api.client.patch(
        f"{BASE}/records/{record_id}", json={"stage": "offer"}, headers=api.headers
    )
    assert patched.json()["stage"] == "offer"
    empty = await api.client.patch(f"{BASE}/records/{record_id}", json={}, headers=api.headers)
    assert empty.status_code == 422

    deleted = await api.client.delete(f"{BASE}/records/{record_id}", headers=api.headers)
    assert deleted.status_code == 204
    missing = "6ad3501e8d52090966a06598"
    assert (
        await api.client.delete(f"{BASE}/records/{missing}", headers=api.headers)
    ).status_code == 404
    assert (
        await api.client.patch(
            f"{BASE}/records/{missing}", json={"stage": "x"}, headers=api.headers
        )
    ).status_code == 404
    assert [e["action"] for e in api.audit.entries if e["action"].startswith("record.")] == [
        "record.create",
        "record.create",
        "record.update",
        "record.delete",
    ]
