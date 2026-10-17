"""dev-tooling T3: the seed script creates and removes only the fake persona, over the API."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from scripts import seed


class FakeApi:
    """Just enough of /admin/v1 for the seed script."""

    def __init__(self) -> None:
        self.records: dict[str, dict[str, Any]] = {"real": {"id": "real", "notes": None}}
        self.deleted: list[str] = []
        self.calls: list[str] = []
        self.profile: dict[str, Any] | None = None
        self.polls = 0

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/admin/v1")
        self.calls.append(f"{request.method} {path}")
        body: dict[str, Any] = json.loads(request.content) if request.content else {}
        if request.method == "POST" and path == "/ingest/text":
            assert body["tags"] == [seed.MARKER] and "Kavya Testpal" in body["body"]
            return httpx.Response(202, json={"source_id": f"src{len(self.calls)}"})
        if request.method == "GET" and path.startswith("/sources/"):
            self.polls += 1
            done = self.polls % 2 == 0  # each job reports "embedding" once, then "done"
            sid = path.rsplit("/", 1)[1]
            status = "done" if done else "embedding"
            return httpx.Response(200, json={"status": status, "document_ids": [f"doc-{sid}"]})
        if request.method == "POST" and path == "/records":
            rid = f"rec{len(self.records)}"
            self.records[rid] = {"id": rid, **body}
            return httpx.Response(201, json=self.records[rid])
        if request.method == "GET" and path == "/records":
            return httpx.Response(200, json=list(self.records.values()))
        if request.method == "PUT" and path == "/profile":
            self.profile = body
            return httpx.Response(200, json=body)
        if request.method == "DELETE":
            self.deleted.append(path)
            self.records.pop(path.rsplit("/", 1)[1], None)
            return httpx.Response(204)
        return httpx.Response(404)


@pytest.fixture
def api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[FakeApi, httpx.Client]:
    monkeypatch.setattr(seed, "MANIFEST", tmp_path / "vault" / "seed.json")
    fake = FakeApi()
    client = httpx.Client(
        base_url="http://api/admin/v1", transport=httpx.MockTransport(fake.handle)
    )
    return fake, client


def test_dry_run_calls_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(seed, "admin_client", lambda url: pytest.fail("no API in a dry run"))
    assert seed.main([]) == 0
    assert seed.main(["--remove"]) == 0


def test_seed_then_remove_touches_only_seeded_data(api: tuple[FakeApi, httpx.Client]) -> None:
    fake, client = api
    manifest = seed.seed(client, with_profile=False, interval=0)
    assert len(manifest.sources) == len(seed.NOTES)
    assert len(manifest.documents) == len(seed.NOTES)
    assert len(manifest.records) == len(seed.RECORDS)
    assert seed.Manifest.load() == manifest and fake.profile is None
    assert seed.execute(seed.parse([]), client) == 1  # a second seed is refused

    documents, records = seed.remove(client, seed.Manifest.load())
    assert (documents, records) == (len(seed.NOTES), len(seed.RECORDS))
    assert "real" in fake.records  # an unmarked record is never touched
    assert not seed.MANIFEST.exists()


def test_records_are_found_by_marker_without_a_manifest(api: tuple[FakeApi, httpx.Client]) -> None:
    fake, client = api
    seed.seed(client, with_profile=True, interval=0)
    assert fake.profile == seed.PROFILE
    seed.MANIFEST.unlink()
    _, records = seed.remove(client, None)
    assert records == len(seed.RECORDS) and list(fake.records) == ["real"]
