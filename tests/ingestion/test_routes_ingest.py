"""Ingestion T8: admin ingest routes and source status."""

from __future__ import annotations

from typing import Any

import pytest

from app.kb.ingestion.fetch import url as url_module
from tests.envelope import result
from tests.ingestion import persona
from tests.ingestion.conftest import REPO, Api
from tests.ingestion.test_fetch import linkedin_zip

TEXT = {"title": "Interview", "body": persona.note(), "category": "interview", "tags": ["backend"]}


async def status_of(api: Api, source_id: str) -> dict[str, Any]:
    response = await api.client.get(f"/admin/v1/sources/{source_id}", headers=api.headers)
    assert response.status_code == 200
    body: dict[str, Any] = result(response)
    return body


async def test_text_ingest_is_async_and_pollable(api: Api) -> None:
    response = await api.client.post("/admin/v1/ingest/text", json=TEXT, headers=api.headers)
    assert response.status_code == 202
    body = result(response)
    assert body["status"] == "queued" and body["duplicate"] is False
    await api.wait()
    status = await status_of(api, body["sourceId"])
    assert status["status"] == "done"
    assert len(status["documentIds"]) == 1
    assert api.audit.entries[0]["action"] == "ingest.submit"
    assert "body" not in str(api.audit.entries)


async def test_same_text_twice_is_a_no_op(api: Api) -> None:
    first = result(await api.client.post("/admin/v1/ingest/text", json=TEXT, headers=api.headers))
    await api.wait()
    again = await api.client.post("/admin/v1/ingest/text", json=TEXT, headers=api.headers)
    assert again.status_code == 202
    assert result(again)["sourceId"] == first["sourceId"]
    assert result(again)["duplicate"] is True
    assert result(again)["status"] == "done"
    assert len(api.h.store.documents) == 1


async def test_text_validation(api: Api) -> None:
    bad = {**TEXT, "category": "gossip"}
    response = await api.client.post("/admin/v1/ingest/text", json=bad, headers=api.headers)
    assert response.status_code == 422
    unauth = await api.client.post("/admin/v1/ingest/text", json=TEXT)
    assert unauth.status_code == 401


async def test_file_batch(api: Api) -> None:
    files = [
        ("files", ("cv.pdf", persona.pdf_bytes("Kavya backend engineer"), "application/pdf")),
        ("files", ("notes.md", persona.note().encode(), "text/markdown")),
        ("files", ("shot.png", persona.png_bytes(), "image/png")),
    ]
    data = {"category": "project", "tags": "a, b"}
    response = await api.client.post(
        "/admin/v1/ingest/files", files=files, data=data, headers=api.headers
    )
    assert response.status_code == 202, response.text
    sources = result(response)["sources"]
    assert [s["filename"] for s in sources] == ["cv.pdf", "notes.md", "shot.png"]
    await api.wait()
    for source in sources:
        assert (await status_of(api, source["sourceId"]))["status"] == "done"
    assert {d.tags[0] for d in api.h.store.documents.values()} == {"a"}


async def test_mislabelled_file_names_the_detected_type(api: Api) -> None:
    files = [("files", ("resume.pdf", persona.png_bytes(), "application/pdf"))]
    response = await api.client.post(
        "/admin/v1/ingest/files", files=files, data={"category": "resume"}, headers=api.headers
    )
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "INVALID_INPUT"
    assert error["details"]["detected"] == "png"
    assert "png" in response.json()["message"]
    assert api.h.store.sources == {}


async def test_unsupported_file_is_refused(api: Api) -> None:
    files = [("files", ("a.gif", b"GIF89a....", "image/gif"))]
    response = await api.client.post(
        "/admin/v1/ingest/files", files=files, data={"category": "project"}, headers=api.headers
    )
    assert response.status_code == 422
    assert response.json()["error"]["details"]["detected"] == "gif"


async def test_oversize_file_is_413(api: Api) -> None:
    big = b"a" * (1024 * 1024 + 1)
    files = [("files", ("big.txt", big, "text/plain"))]
    response = await api.client.post(
        "/admin/v1/ingest/files", files=files, data={"category": "project"}, headers=api.headers
    )
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "PAYLOAD_TOO_LARGE"


async def test_too_many_files_and_bad_form(api: Api) -> None:
    files = [("files", (f"{i}.txt", b"x", "text/plain")) for i in range(4)]
    response = await api.client.post(
        "/admin/v1/ingest/files", files=files, data={"category": "project"}, headers=api.headers
    )
    assert response.status_code == 422
    bad = await api.client.post(
        "/admin/v1/ingest/files",
        files=[("files", ("a.txt", b"x", "text/plain"))],
        data={"category": "nope"},
        headers=api.headers,
    )
    assert bad.status_code == 422


async def test_urls(api: Api, monkeypatch: pytest.MonkeyPatch) -> None:
    async def resolver(host: str, port: int) -> list[str]:
        known = {"talks.example": ["93.184.215.14"], "intranet.example": ["10.1.1.1"]}
        return known.get(host, [host])

    monkeypatch.setattr("app.api.routes.admin_ingest.system_resolver", resolver)
    ok = await api.client.post(
        "/admin/v1/ingest/urls",
        json={"urls": ["https://talks.example/notes"], "category": "blog"},
        headers=api.headers,
    )
    assert ok.status_code == 202
    [source] = result(ok)["sources"]
    await api.wait()
    assert (await status_of(api, source["sourceId"]))["status"] == "done"

    for bad in ("ftp://talks.example/x", "http://intranet.example/", "http://127.0.0.1/"):
        response = await api.client.post(
            "/admin/v1/ingest/urls", json={"urls": [bad], "category": "blog"}, headers=api.headers
        )
        assert response.status_code == 422, bad
        assert response.json()["error"]["details"]["reason"] == "FETCH_BLOCKED"
    assert url_module.MAX_REDIRECTS == 5


async def test_github_allowlist(api: Api) -> None:
    ok = await api.client.post(
        "/admin/v1/ingest/github", json={"repos": [REPO]}, headers=api.headers
    )
    assert ok.status_code == 202
    refused = await api.client.post(
        "/admin/v1/ingest/github", json={"repos": [REPO, "someone/else"]}, headers=api.headers
    )
    assert refused.status_code == 422
    assert refused.json()["error"]["details"]["reason"] == "NOT_ALLOWED"
    await api.wait()
    assert len(api.h.store.sources) == 1


async def test_linkedin_export(api: Api) -> None:
    response = await api.client.post(
        "/admin/v1/ingest/linkedin-export",
        files={"file": ("export.zip", linkedin_zip(), "application/zip")},
        headers=api.headers,
    )
    assert response.status_code == 202
    await api.wait()
    status = await status_of(api, result(response)["sourceId"])
    assert status["status"] == "done"
    assert len(status["documentIds"]) == 7
    not_zip = await api.client.post(
        "/admin/v1/ingest/linkedin-export",
        files={"file": ("export.zip", b"plain text", "application/zip")},
        headers=api.headers,
    )
    assert not_zip.status_code == 422


async def test_unknown_source_and_stopped_ingestion(api: Api) -> None:
    for value in ("not-an-id", "6ad3501e8d52090966a06598"):
        response = await api.client.get(f"/admin/v1/sources/{value}", headers=api.headers)
        assert response.status_code == 404
    api.app.state.ingestion = None
    response = await api.client.post("/admin/v1/ingest/text", json=TEXT, headers=api.headers)
    assert response.status_code == 503
