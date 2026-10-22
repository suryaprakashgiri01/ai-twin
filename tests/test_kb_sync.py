"""answer-studio T6: the KB loader plans, ingests idempotently and replaces changed entries."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from scripts import kb_sync


def ok(result: Any) -> httpx.Response:
    return httpx.Response(200, json={"status": "success", "message": "OK", "result": result})


class FakeApi:
    def __init__(self, quarantine: bool = False) -> None:
        self.calls: list[tuple[str, str, Any]] = []
        self.quarantine = quarantine
        self.n = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/admin/v1")
        body: Any = None
        if request.headers.get("content-type", "").startswith("application/json"):
            body = json.loads(request.content or b"null")
        self.calls.append((request.method, path, body))
        if path.startswith("/ingest/"):
            self.n += 1
            source = {"sourceId": f"s{self.n}", "status": "queued", "duplicate": False}
            return ok(source if path == "/ingest/text" else {"sources": [source]})
        if path.startswith("/sources/"):
            status = "quarantined" if self.quarantine else "done"
            return ok({"status": status, "documentIds": [f"d-{path.rsplit('/', 1)[1]}"]})
        if path.startswith("/documents/") and request.method == "GET":
            return ok({"reviewStatus": "quarantined" if self.quarantine else "pending"})
        return ok({})


def setup(tmp_path: Path, manifest: str) -> list[kb_sync.Entry]:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "notes.md").write_text("# Notes\nRate limiter work.")
    (tmp_path / "docs" / "resume.pdf").write_bytes(b"%PDF-1.4 fake")
    return kb_sync.parse_entries(yaml.safe_load(manifest))


MANIFEST = """
entries:
  - {key: resume, file: docs/resume.pdf, category: resume}
  - {key: notes, text: docs/notes.md, category: work-note, release: local}
  - {key: site, url: "https://example.vercel.app/", category: profile-link}
  - {key: repos, repos: [owner/one, owner/two], tags: [github]}
"""


@pytest.fixture(autouse=True)
def kb_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(kb_sync, "KB_DIR", tmp_path)
    monkeypatch.setattr(kb_sync, "STATE", tmp_path / "state.json")
    return tmp_path


def test_manifest_validation() -> None:
    with pytest.raises(kb_sync.SyncError, match="entries"):
        kb_sync.parse_entries({})
    with pytest.raises(kb_sync.SyncError, match="one of"):
        kb_sync.parse_entries({"entries": [{"key": "x", "file": "a", "url": "b"}]})
    with pytest.raises(kb_sync.SyncError, match="category"):
        kb_sync.parse_entries({"entries": [{"key": "x", "file": "a"}]})


def test_apply_is_idempotent_and_replaces_changed_entries(tmp_path: Path) -> None:
    entries = setup(tmp_path, MANIFEST)
    api = FakeApi(quarantine=True)
    client = httpx.Client(base_url="http://t/admin/v1", transport=httpx.MockTransport(api))
    state: dict[str, kb_sync.EntryState] = {}
    counts = kb_sync.apply(client, entries, state, timeout=1)
    assert counts == {"new": 4, "changed": 0, "unchanged": 0}
    assert state["repos"].sources == ["s4"]
    releases = [c for c in api.calls if c[1].endswith("/release")]
    assert releases == [("POST", "/documents/d-s2/release", {"localOnly": True})]
    assert {c[1] for c in api.calls if c[0] == "POST" and c[1].startswith("/ingest/")} == {
        "/ingest/files",
        "/ingest/text",
        "/ingest/urls",
        "/ingest/github",
    }
    api.calls.clear()
    reloaded = kb_sync.load_state()
    assert kb_sync.apply(client, entries, reloaded, timeout=1)["unchanged"] == 4
    assert api.calls == []
    (tmp_path / "docs" / "notes.md").write_text("# Notes\nNew text.")
    assert [s for _, s in kb_sync.plan(entries, reloaded)] == [
        "unchanged",
        "changed",
        "unchanged",
        "unchanged",
    ]
    kb_sync.apply(client, entries, reloaded, timeout=1)
    assert ("DELETE", "/documents/d-s2", None) in api.calls
    assert reloaded["notes"].documents == ["d-s5"]


def test_url_and_repo_changes_refresh_instead_of_deleting(tmp_path: Path) -> None:
    entries = setup(tmp_path, MANIFEST)
    api = FakeApi()
    client = httpx.Client(base_url="http://t/admin/v1", transport=httpx.MockTransport(api))
    state: dict[str, kb_sync.EntryState] = {}
    kb_sync.apply(client, entries, state, timeout=1)
    state["repos"].checksum = "stale"
    api.calls.clear()
    kb_sync.apply(client, entries, state, timeout=1)
    [(_method, path, body)] = [c for c in api.calls if c[1].startswith("/ingest/")]
    assert path == "/ingest/github" and body["refresh"] is True
    assert not [c for c in api.calls if c[0] == "DELETE"]


def test_dry_run_changes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    setup(tmp_path, MANIFEST)
    (tmp_path / "manifest.yaml").write_text(MANIFEST)
    monkeypatch.setattr(kb_sync, "MANIFEST", tmp_path / "manifest.yaml")
    monkeypatch.setattr(kb_sync, "admin_client", lambda *_: pytest.fail("no API in a dry run"))
    assert kb_sync.main([]) == 0
    assert kb_sync.main(["--reset"]) == 0
    assert not (tmp_path / "state.json").exists()


def _git(repo: Path, *args: str, email: str | None = None) -> None:
    import subprocess

    who = ["-c", f"user.email={email}", "-c", "user.name=x"] if email else []
    subprocess.run(["git", "-C", str(repo), *who, *args], check=True, capture_output=True)


def _repo(path: Path) -> Path:
    path.mkdir()
    _git(path, "init", "-q")
    (path / "README.md").write_text("# Rate limiter\nToken bucket service.")
    (path / "docs").mkdir()
    (path / "docs" / "design.md").write_text("# Design\nRedis-backed buckets.")
    (path / "src").mkdir()
    (path / "src" / "app.py").write_text("print('hi')\n")
    _git(path, "add", ".")
    _git(path, "commit", "-qm", "feat: token bucket", email="kavya@work.test")
    (path / "src" / "app.py").write_text("# chore\n")
    _git(path, "commit", "-qam", "chore", email="other@work.test")
    return path


def test_local_repo_digest_is_built_ingested_and_tracks_new_commits(tmp_path: Path) -> None:
    from scripts import repo_digest

    repo = _repo(tmp_path / "rate-limiter")
    text = repo_digest.digest(repo, "kavya@work.test")
    assert text.startswith("# rate-limiter (repository I worked on)")
    assert "Python (1)" in text and "Commits: 1" in text and "- feat: token bucket" in text
    assert "- chore" not in text  # someone else's commit
    assert "Token bucket service." in text and "### docs/design.md" in text
    manifest = (
        "entries:\n"
        f"  - {{key: work, local_repo: '{repo}', author: kavya@work.test, "
        "category: company-projects, release: local}\n"
    )
    entries = kb_sync.parse_entries(yaml.safe_load(manifest))
    api = FakeApi()
    client = httpx.Client(base_url="http://t/admin/v1", transport=httpx.MockTransport(api))
    state: dict[str, kb_sync.EntryState] = {}
    kb_sync.apply(client, entries, state, timeout=1)
    [(_, path, body)] = [c for c in api.calls if c[1].startswith("/ingest/")]
    assert path == "/ingest/text" and body["title"] == "Rate Limiter"
    assert body["category"] == "company-projects" and "feat: token bucket" in body["body"]
    assert (tmp_path / "docs" / "generated" / "work.md").exists()
    assert [s for _, s in kb_sync.plan(entries, state)] == ["unchanged"]
    (repo / "src" / "app.py").write_text("# more\n")
    _git(repo, "commit", "-qam", "feat: burst limits", email="kavya@work.test")
    assert [s for _, s in kb_sync.plan(entries, state)] == ["changed"]
    with pytest.raises(kb_sync.SyncError, match="author"):
        kb_sync.parse_entries(
            {"entries": [{"key": "x", "local_repo": str(repo), "category": "other"}]}
        )
