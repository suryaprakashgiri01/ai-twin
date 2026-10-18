"""owner-intake T5: POST /admin/v1/assistant (SSE), GET /assistant/{id}, and the twin CLI."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from app.auth.keys import issue_key
from app.core.app_factory import create_app
from app.core.config import Settings
from scripts import chat
from tests.conftest import FakeCheck
from tests.fakes import MemoryApiKeyStore, MemoryOverrideStore
from tests.intake.harness import Twin, extract

MakeTwin = Callable[..., Twin]
INTERVIEW = extract(
    "add_record",
    kind="interview",
    company="Acme Widgets",
    role="Backend Engineer",
    stage="round 2",
    date="yesterday",
)


async def _client(twin: Twin) -> tuple[FastAPI, httpx.AsyncClient, dict[str, str]]:
    app = create_app(
        Settings(_env_file=None, app_env="test", log_format="json"),
        health_checks=[FakeCheck("mongodb")],
    )
    keys = MemoryApiKeyStore()
    app.state.resources = SimpleNamespace(
        api_keys=keys, audit=twin.audit, overrides=MemoryOverrideStore()
    )
    app.state.intake = twin.service
    key = (await issue_key(keys, "owner-cli", ["admin"]))[1]
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    return app, client, {"X-API-Key": key}


@pytest.fixture
async def api(make_twin: MakeTwin) -> AsyncIterator[tuple[Twin, httpx.AsyncClient, dict[str, str]]]:
    twin = make_twin([INTERVIEW])
    _, client, headers = await _client(twin)
    async with client:
        yield twin, client, headers


def events(text: str) -> list[tuple[str, dict[str, Any]]]:
    return list(chat.parse_sse(text.splitlines(keepends=True)))


async def test_sse_draft_then_yes(api: tuple[Twin, httpx.AsyncClient, dict[str, str]]) -> None:
    twin, client, headers = api
    first = await client.post(
        "/admin/v1/assistant",
        json={"message": "Interview with Acme Widgets, Backend Engineer, round 2, yesterday"},
        headers=headers,
    )
    assert first.status_code == 200
    assert first.headers["content-type"].startswith("text/event-stream")
    got = events(first.text)
    names = [e for e, _ in got]
    assert names[0] == "meta" and names[-1] == "done" and "draft" in names
    cid = got[0][1]["conversation_id"]
    assert twin.index.meta[cid]["mode"] == "intake"
    assert twin.index.meta[cid]["name"]

    state = await client.get(f"/admin/v1/assistant/{cid}", headers=headers)
    assert state.json()["pending"]["action"] == "add_record"
    assert state.json()["mode"] == "intake"

    second = await client.post(
        "/admin/v1/assistant", json={"conversation_id": cid, "message": "yes"}, headers=headers
    )
    names = [e for e, _ in events(second.text)]
    assert "result" in names and names[-1] == "done"
    assert len(twin.records.rows) == 1
    assert twin.audit.entries[0]["actor"] == "agent:intake via key:owner-cli"


async def test_requires_admin_key(api: tuple[Twin, httpx.AsyncClient, dict[str, str]]) -> None:
    _, client, _ = api
    response = await client.post("/admin/v1/assistant", json={"message": "hi"})
    assert response.status_code == 401


async def test_unknown_conversation_is_404(
    api: tuple[Twin, httpx.AsyncClient, dict[str, str]],
) -> None:
    _, client, headers = api
    body = {"conversation_id": "f" * 32, "message": "yes"}
    response = await client.post("/admin/v1/assistant", json=body, headers=headers)
    assert response.status_code == 404


async def test_ask_conversation_is_refused(
    api: tuple[Twin, httpx.AsyncClient, dict[str, str]],
) -> None:
    twin, client, headers = api
    await twin.index.set_mode("e" * 32, "ask")
    body = {"conversation_id": "e" * 32, "message": "yes"}
    response = await client.post("/admin/v1/assistant", json=body, headers=headers)
    assert response.status_code == 409


async def test_cloud_only_is_503_with_the_ollama_hint(make_twin: MakeTwin) -> None:
    twin = make_twin([INTERVIEW], cloud_only=True)
    _, client, headers = await _client(twin)
    async with client:
        response = await client.post(
            "/admin/v1/assistant", json={"message": "Interview with Acme"}, headers=headers
        )
    assert response.status_code == 503
    assert "make ollama ARGS=start" in response.json()["error"]["message"]
    assert twin.factory.models["cloud"].calls == 0


# --- CLI -------------------------------------------------------------------------------------


CID = "a" * 32
PENDING = {
    "kind": "draft",
    "action": "add_record",
    "summary": "Add a record",
    "payload": {"company": "Acme Widgets", "date": "2026-10-17"},
    "prompt": "Add a record\n  company: Acme Widgets\n\nSave this? (yes / no / an edit)",
    "reask": False,
}


def cli_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/healthz":
        return httpx.Response(200, json={"status": "ok", "version": "0.0.0"})
    if path == "/admin/v1/whoami":
        return httpx.Response(200, json={"name": "owner", "scopes": ["admin"]})
    if path == "/admin/v1/conversations":
        rows = [
            {
                "id": CID,
                "name": "acme-interview",
                "mode": "intake",
                "updated_at": "2026-10-18",
                "message_count": 2,
                "description": "Interview with Acme",
            },
            {"id": "b" * 32, "name": "salary", "updated_at": "2026-10-17", "message_count": 2},
        ]
        return httpx.Response(200, json=rows)
    if path == f"/admin/v1/conversations/{CID}":
        turns = [{"role": "user", "content": "Interview with Acme"}]
        return httpx.Response(
            200,
            json={
                "id": CID,
                "name": "acme-interview",
                "mode": "intake",
                "message_count": 2,
                "turns": turns,
            },
        )
    if path == f"/admin/v1/assistant/{CID}":
        return httpx.Response(
            200, json={"conversation_id": CID, "mode": "intake", "pending": PENDING}
        )
    if path == "/admin/v1/assistant":
        body = (
            f'event: meta\ndata: {{"conversation_id": "{CID}"}}\n\n'
            'event: token\ndata: {"text": "Saved interview Acme Widgets"}\n\n'
            'event: done\ndata: {"status": "done", "action": "add_record", "pending": false}\n\n'
        )
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})
    return httpx.Response(500)


def cli() -> chat.Api:
    return chat.Api(
        chat.Config(base_url="http://test", key="k"), transport=httpx.MockTransport(cli_handler)
    )


def test_list_shows_mode() -> None:
    api = cli()
    out = chat.render_list(api.list(20))
    header, first, second = out.splitlines()
    assert "MODE" in header
    assert " intake " in first and " ask " in second


def test_continue_intake_shows_the_pending_draft(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    replies = iter(["yes", "/exit"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(replies))
    api = cli()
    args = chat.build_parser().parse_args(["continue", CID])
    assert chat.execute(args, api) == 0
    out = capsys.readouterr().out
    assert "Waiting for your answer" in out and "Save this?" in out
    assert "Saved interview Acme Widgets" in out


def test_twin_starts_an_intake_conversation(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    replies = iter(["Interview with Acme", "/exit"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(replies))
    paths: list[str] = []

    def spy(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return cli_handler(request)

    api = chat.Api(chat.Config(base_url="http://test", key="k"), transport=httpx.MockTransport(spy))
    assert chat.execute(chat.build_parser().parse_args(["twin"]), api) == 0
    assert "/admin/v1/assistant" in paths and "/admin/v1/chat" not in paths
    assert "Saved interview" in capsys.readouterr().out
