"""Owner-agent T6: chat and conversation routes, SSE order, JSON form, history reload (AC7-AC9)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from app.auth.keys import issue_key
from app.core.app_factory import create_app
from app.core.config import Settings
from app.services.conversations import Turn
from app.services.owner_chat import ConversationMeta, ConversationSummary, OwnerChatService
from llm_orchestrator import Tier
from tests.agent.harness import PRIV_TEXT, Agent, hit, make_agent, route_json
from tests.conftest import FakeCheck
from tests.fakes import (
    MemoryApiKeyStore,
    MemoryAuditLog,
    MemoryConversationStore,
    MemoryOverrideStore,
)

Q = "what did I learn at the meetup?"


class MemoryIndex:
    def __init__(self, store: MemoryConversationStore) -> None:
        self.store = store
        self.titles: dict[str, tuple[str | None, str | None]] = {}

    def _meta(self, conversation_id: str) -> ConversationMeta:
        meta = self.store.conversations[conversation_id]
        turns = self.store.turns.get(conversation_id, {})
        stamps = [t.created_at for t in turns.values()]
        name, description = self.titles.get(conversation_id, (None, None))
        return ConversationMeta(
            id=conversation_id,
            name=name,
            description=description,
            channel=meta["channel"],
            created_at=min(stamps),
            updated_at=max(stamps),
            message_count=max(turns, default=0),
        )

    async def get(self, conversation_id: str) -> ConversationMeta | None:
        if conversation_id not in self.store.conversations:
            return None
        return self._meta(conversation_id)

    async def list_owner(self, limit: int) -> list[ConversationSummary]:
        metas = [
            self._meta(c) for c, m in self.store.conversations.items() if m["channel"] == "owner"
        ]
        metas.sort(key=lambda m: m.updated_at, reverse=True)
        return [ConversationSummary(**m.model_dump(exclude={"channel"})) for m in metas[:limit]]

    async def turns(self, conversation_id: str, limit: int) -> list[Turn]:
        return [t for _, t in sorted(self.store.turns.get(conversation_id, {}).items())][:limit]

    async def by_name(self, name: str) -> ConversationMeta | None:
        for conversation_id, (title, _) in self.titles.items():
            if title == name and conversation_id in self.store.conversations:
                return self._meta(conversation_id)
        return None

    async def names_like(self, base: str) -> set[str]:
        names = {title for title, _ in self.titles.values() if title}
        return {n for n in names if n == base or n.startswith(f"{base}-")}

    async def set_title(
        self, conversation_id: str, name: str | None, description: str | None
    ) -> None:
        old_name, old_description = self.titles.get(conversation_id, (None, None))
        self.titles[conversation_id] = (name or old_name, description or old_description)


@dataclass
class ChatApi:
    client: httpx.AsyncClient
    headers: dict[str, str]
    agent: Agent

    async def sse(self, body: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        response = await self.client.post("/admin/v1/chat", json=body, headers=self.headers)
        assert response.status_code == 200, response.text
        assert response.headers["content-type"].startswith("text/event-stream")
        await self.agent.conversations.drain()
        events = []
        for frame in response.text.strip().split("\n\n"):
            name, data = frame.split("\n", 1)
            events.append((name.removeprefix("event: "), json.loads(data.removeprefix("data: "))))
        return events


async def chat_api(agent: Agent) -> AsyncIterator[ChatApi]:
    app = create_app(
        Settings(_env_file=None, app_env="test", log_format="json"),
        health_checks=[FakeCheck("mongodb")],
    )
    keys = MemoryApiKeyStore()
    app.state.resources = SimpleNamespace(
        api_keys=keys, audit=MemoryAuditLog(), overrides=MemoryOverrideStore()
    )
    app.state.owner_chat = OwnerChatService(
        agent.graph, agent.conversations, MemoryIndex(agent.store), frozenset({"cloud", "local"})
    )
    key = (await issue_key(keys, "owner", ["admin"]))[1]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield ChatApi(client, {"X-API-Key": key}, agent)


@pytest.fixture
async def api() -> AsyncIterator[ChatApi]:
    agent = make_agent(
        router=[route_json("kb_search", Q)],
        cloud=["Raft [1]."],
        hits=[hit("priv", Tier.PRIVATE, PRIV_TEXT), hit("conf", Tier.CONFIDENTIAL, "conf")],
    )
    async for value in chat_api(agent):
        yield value


async def test_sse_order_meta_first_done_last(api: ChatApi) -> None:
    events = await api.sse({"message": Q})
    names = [n for n, _ in events]
    assert names[0] == "meta" and names[-1] == "done"
    assert names == ["meta", "token", "token", "sources", "done"]  # answer + withheld note
    meta, done = events[0][1], events[-1][1]
    assert meta["route"] == "kb_search" and meta["max_tier"] == "confidential"
    sources = dict(events)["sources"]
    assert sources["citations"] == ["priv"]
    assert sources["sources"][0]["tier"] == "private"
    assert done["citations"] == ["priv"] and done["tier"] == "private"
    assert done["withheld"] == 1 and done["model"] == "cloud" and done["seq"] == 2
    assert done["conversation_id"] == meta["conversation_id"]


async def test_stream_false_returns_the_same_fields(api: ChatApi) -> None:
    response = await api.client.post(
        "/admin/v1/chat?stream=false", json={"message": Q}, headers=api.headers
    )
    assert response.status_code == 200
    body = response.json()
    assert body["answer"].startswith("Raft [1].")
    assert body["citations"] == ["priv"] and body["sources"][0]["document_id"] == "priv"
    assert {
        "route",
        "query",
        "tier",
        "model",
        "withheld",
        "incomplete",
        "latency_ms",
        "tokens",
        "seq",
    } <= set(body)
    assert body["incomplete"] is False and body["error"] is None


async def test_follow_up_and_conversation_routes(api: ChatApi) -> None:
    first = await api.sse({"message": Q, "max_tier": "private"})
    conversation_id = first[0][1]["conversation_id"]
    again = await api.sse(
        {"message": "thanks", "conversation_id": conversation_id, "max_tier": "private"}
    )
    assert again[-1][1]["seq"] == 4
    listed = await api.client.get("/admin/v1/conversations", headers=api.headers)
    assert [c["id"] for c in listed.json()] == [conversation_id]
    detail = await api.client.get(f"/admin/v1/conversations/{conversation_id}", headers=api.headers)
    turns = detail.json()["turns"]
    assert [t["role"] for t in turns] == ["user", "assistant", "user", "assistant"]
    assert turns[1]["citations"] == ["priv"] and turns[1]["route"] == "kb_search"


async def test_history_reloads_after_a_redis_flush(api: ChatApi) -> None:
    first = await api.sse({"message": Q})
    conversation_id = first[0][1]["conversation_id"]
    await api.agent.redis.flushall()
    await api.sse({"message": "and the one before that?", "conversation_id": conversation_id})
    router_prompt = "\n".join(str(m.content) for m in api.agent.factory.models["router"].seen[-1])
    assert Q in router_prompt and "Raft [1]." in router_prompt
    assert [t.seq for t in await api.agent.history(conversation_id)] == [1, 2, 3, 4]


async def test_errors_before_streaming_use_the_envelope(api: ChatApi) -> None:
    unknown = await api.client.post(
        "/admin/v1/chat", json={"message": Q, "conversation_id": "a" * 32}, headers=api.headers
    )
    assert unknown.status_code == 404 and unknown.json()["error"]["code"] == "NOT_FOUND"
    bad_model = await api.client.post(
        "/admin/v1/chat", json={"message": Q, "model": "nope"}, headers=api.headers
    )
    assert bad_model.status_code == 422
    empty = await api.client.post("/admin/v1/chat", json={"message": ""}, headers=api.headers)
    assert empty.status_code == 422
    assert (await api.client.post("/admin/v1/chat", json={"message": Q})).status_code == 401
    missing = await api.client.get(f"/admin/v1/conversations/{'b' * 32}", headers=api.headers)
    assert missing.status_code == 404


async def test_mid_stream_failure_ends_with_error_event() -> None:
    agent = make_agent(
        router=[route_json("kb_search", Q)],
        cloud=["unused"],
        hits=[hit("priv", Tier.PRIVATE, PRIV_TEXT)],
        streams={"cloud": [["Raft ", ConnectionError("dropped")]]},
    )
    async for api in chat_api(agent):
        events = await api.sse({"message": Q})
        assert [n for n, _ in events] == ["meta", "token", "error"]
        error = events[-1][1]
        assert error["code"] == "STREAM_INTERRUPTED" and error["incomplete"] is True
        stored = await agent.history(error["conversation_id"])
        assert stored[-1].incomplete is True and stored[-1].content == "Raft "
        body = (
            await api.client.post(
                "/admin/v1/chat?stream=false", json={"message": Q}, headers=api.headers
            )
        ).json()
        assert body["incomplete"] is True and body["error"]["code"] == "STREAM_INTERRUPTED"


async def test_chat_without_the_service_is_unavailable() -> None:
    app = create_app(
        Settings(_env_file=None, app_env="test", log_format="json"),
        health_checks=[FakeCheck("mongodb")],
    )
    keys = MemoryApiKeyStore()
    app.state.resources = SimpleNamespace(
        api_keys=keys, audit=MemoryAuditLog(), overrides=MemoryOverrideStore()
    )
    key = (await issue_key(keys, "owner", ["admin"]))[1]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/admin/v1/chat", json={"message": Q}, headers={"X-API-Key": key}
        )
    assert response.status_code == 503


async def test_new_conversations_get_unique_names(api: ChatApi) -> None:
    first = (await api.sse({"message": Q}))[0][1]["conversation_id"]
    second = (await api.sse({"message": Q}))[0][1]["conversation_id"]
    listed = {
        c["id"]: c
        for c in (await api.client.get("/admin/v1/conversations", headers=api.headers)).json()
    }
    base = listed[first]["name"]
    assert base and listed[second]["name"] == f"{base}-2"
    assert listed[first]["description"] == Q[:100]
    found = await api.client.get(f"/admin/v1/conversations/by-name/{base}", headers=api.headers)
    assert found.status_code == 200 and found.json()["id"] == first
    missing = await api.client.get("/admin/v1/conversations/by-name/nope", headers=api.headers)
    assert missing.status_code == 404


async def test_rename_validates_and_refuses_taken_names(api: ChatApi) -> None:
    first = (await api.sse({"message": Q}))[0][1]["conversation_id"]
    second = (await api.sse({"message": "another question entirely"}))[0][1]["conversation_id"]
    url = f"/admin/v1/conversations/{second}"
    renamed = await api.client.patch(
        url, json={"name": "raft-notes", "description": "Raft study"}, headers=api.headers
    )
    assert renamed.status_code == 200
    assert renamed.json()["name"] == "raft-notes"
    assert renamed.json()["description"] == "Raft study"
    first_name = (
        await api.client.get(f"/admin/v1/conversations/{first}", headers=api.headers)
    ).json()["name"]
    taken = await api.client.patch(url, json={"name": first_name}, headers=api.headers)
    assert taken.status_code == 409 and taken.json()["error"]["code"] == "CONFLICT"
    invalid = await api.client.patch(url, json={"name": "Not A Slug"}, headers=api.headers)
    assert invalid.status_code == 422
