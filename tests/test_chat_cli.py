from __future__ import annotations

from typing import Any

import httpx
import pytest

from scripts import chat

CID = "a" * 32


def _conversation(cid: str, name: str, updated: str, turns: int) -> dict[str, Any]:
    return {
        "id": cid,
        "name": name,
        "description": f"About {name}",
        "updated_at": updated,
        "message_count": turns,
    }


CONVERSATIONS = [
    _conversation(CID, "interview-prep", "2026-10-18T09:00:00Z", 4),
    _conversation("b" * 32, "interview-prep-2", "2026-10-17T09:00:00Z", 2),
    _conversation("c" * 32, "salary-notes", "2026-10-16T09:00:00Z", 2),
]


def handler(request: httpx.Request) -> httpx.Response:
    assert request.headers["X-API-Key"] == "atw_test"
    path = request.url.path
    if path == "/healthz":
        return httpx.Response(200, json={"status": "ok", "version": "9.9.9"})
    if path == "/admin/v1/whoami":
        return httpx.Response(200, json={"name": "owner", "scopes": ["admin"]})
    if path == "/admin/v1/conversations":
        return httpx.Response(200, json=CONVERSATIONS)
    if path.startswith("/admin/v1/conversations/by-name/"):
        name = path.rsplit("/", 1)[1]
        for c in CONVERSATIONS:
            if c["name"] == name:
                return httpx.Response(200, json=c)
        return httpx.Response(404, json={"error": {"code": "NOT_FOUND", "message": "nope"}})
    if path.startswith("/admin/v1/conversations/"):
        cid = path.rsplit("/", 1)[1]
        for c in CONVERSATIONS:
            if c["id"] == cid:
                return httpx.Response(200, json={**c, "turns": []})
        return httpx.Response(404, json={"error": {"code": "NOT_FOUND", "message": "nope"}})
    if path == "/admin/v1/chat":
        body = (
            'event: meta\ndata: {"conversation_id": "' + CID + '"}\n\n'
            'event: token\ndata: {"text": "Hello"}\n\n'
            "event: done\n"
            'data: {"tier": "private", "model": "ollama-local", "latency_ms": 1200}\n\n'
        )
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})
    return httpx.Response(500)


def make_api(fn: Any = handler) -> chat.Api:
    config = chat.Config(base_url="http://test", key="atw_test")
    return chat.Api(config, transport=httpx.MockTransport(fn))


def test_parse_sse_handles_frames_and_crlf() -> None:
    lines = ["event: meta\r\n", 'data: {"a": 1}\r\n', "\r\n", 'data: {"b": 2}']
    assert list(chat.parse_sse(lines)) == [("meta", {"a": 1}), ("message", {"b": 2})]


@pytest.mark.parametrize(
    ("ref", "expected"),
    [(CID, CID), ("interview-prep", CID), ("salary", "c" * 32)],
    ids=["id", "exact-name", "unique-prefix"],
)
def test_resolve_by_id_name_or_prefix(ref: str, expected: str) -> None:
    assert chat.resolve(make_api(), ref)["id"] == expected


def test_resolve_ambiguous_and_missing() -> None:
    with pytest.raises(chat.CliError, match="matches 2 conversations"):
        chat.resolve(make_api(), "interview")
    with pytest.raises(chat.CliError, match="No conversation named"):
        chat.resolve(make_api(), "zzz")
    with pytest.raises(chat.CliError, match="No conversation with id"):
        chat.resolve(make_api(), "f" * 32)


def test_chat_stream_yields_events() -> None:
    events = list(make_api().chat("hi", None))
    assert [e for e, _ in events] == ["meta", "token", "done"]
    assert "ollama-local" in chat._done_line(events[-1][1])


def test_auth_and_connection_errors_are_one_line() -> None:
    def unauthorized(_: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"code": "AUTH_INVALID", "message": "bad"}})

    with pytest.raises(chat.CliError, match="401"):
        make_api(unauthorized).list(5)

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(chat.CliError, match="not reachable"):
        make_api(down).list(5)


def test_missing_key_is_actionable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(chat.KEY_NAME, raising=False)
    with pytest.raises(chat.CliError, match="AI_TWIN_ADMIN_KEY is not set"):
        chat.load_config(None, env={})
    config = chat.load_config(None, env={chat.KEY_NAME: "atw_x", "PORT": "8010"})
    assert config.base_url == "http://127.0.0.1:8010" and config.key == "atw_x"


def test_render_list_and_empty() -> None:
    table = chat.render_list(CONVERSATIONS)
    assert "interview-prep" in table and CID in table and "2026-10-18 09:00" in table
    assert "No conversations yet" in chat.render_list([])


def test_main_lists_and_reports_errors(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = chat.Config("http://test", "atw_test")
    real_api = chat.Api
    monkeypatch.setattr(chat, "load_config", lambda base_url: config)
    monkeypatch.setattr(
        chat, "Api", lambda cfg: real_api(cfg, transport=httpx.MockTransport(handler))
    )
    assert chat.main(["list"]) == 0
    assert "salary-notes" in capsys.readouterr().out
    assert chat.main(["continue", "interview"]) == 1
    assert "matches 2" in capsys.readouterr().err
    inputs = iter(["hello", "/exit"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(inputs))
    assert chat.main(["continue", "salary-notes"]) == 0
    out = capsys.readouterr().out
    assert "Hello" in out and "kept" in out
    assert "━━ ai-twin · owner chat ━━" in out and 'key "owner"' in out and "API 9.9.9" in out
    assert "Continuing salary-notes (ask)" in out and "Valkey" in out


def test_twin_banner_explains_the_intake_before_the_first_prompt(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = chat.Config("http://test", "atw_test")
    real_api = chat.Api
    monkeypatch.setattr(chat, "load_config", lambda base_url: config)
    monkeypatch.setattr(
        chat, "Api", lambda cfg: real_api(cfg, transport=httpx.MockTransport(handler))
    )
    monkeypatch.setattr("builtins.input", lambda _prompt: "/exit")
    assert chat.main(["twin"]) == 0
    out = capsys.readouterr().out
    assert out.index("intake assistant") < out.index("Commands: /exit")
    assert "save nothing until you say yes" in out and "assessment" in out
