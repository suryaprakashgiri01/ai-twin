"""Owner-console T2: channel listing, visitor scoping and soft delete of conversations."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr

from app.auth.keys import KeyRecord, issue_key
from app.core.app_factory import create_app
from app.core.config import Settings
from app.services.conversations import Turn, hash_visitor
from app.services.owner_chat import OwnerChatService
from llm_orchestrator import Tier
from tests.agent.harness import PRIV_TEXT, Agent, hit, make_agent, route_json
from tests.agent.test_chat_api import ChatApi, MemoryIndex
from tests.conftest import FakeCheck
from tests.envelope import items, result
from tests.fakes import MemoryApiKeyStore, MemoryAuditLog, MemoryOverrideStore

Q = "what did I learn at the meetup?"
SALT = "salt"


@dataclass
class Scope:
    owner: ChatApi
    audit: MemoryAuditLog
    visitor: dict[str, str]
    visitor_record: KeyRecord
    other: dict[str, str]


def seed_public(agent: Agent, conversation_id: str, record: KeyRecord, text: str) -> None:
    """A visitor conversation as the public channel stores it, keyed by the key's hash."""
    agent.store.conversations[conversation_id] = {
        "channel": "public",
        "visitor_hash": hash_visitor(f"key:{record.id}", SALT),
    }
    agent.store.turns[conversation_id] = {
        1: Turn(seq=1, role="user", content=text),
        2: Turn(seq=2, role="assistant", content="An answer."),
    }


@pytest.fixture
async def scope() -> AsyncIterator[Scope]:
    agent = make_agent(
        router=[route_json("kb_search", Q)],
        cloud=["Raft [1]."],
        hits=[hit("priv", Tier.PRIVATE, PRIV_TEXT)],
    )
    app = create_app(
        Settings(
            _env_file=None,
            app_env="test",
            log_format="json",
            visitor_hash_salt=SecretStr(SALT),
        ),
        health_checks=[FakeCheck("mongodb")],
    )
    keys, audit = MemoryApiKeyStore(), MemoryAuditLog()
    app.state.resources = SimpleNamespace(
        api_keys=keys, audit=audit, overrides=MemoryOverrideStore()
    )
    service = OwnerChatService(
        agent.graph, agent.conversations, MemoryIndex(agent.store), frozenset({"cloud", "local"})
    )
    app.state.owner_chat = service
    app.state.conversation_directory = service.directory
    owner_key = (await issue_key(keys, "owner", ["admin"]))[1]
    visitor_record, visitor_key = await issue_key(keys, "site", ["public:read"])
    other_record, other_key = await issue_key(keys, "other", ["public:read"])
    seed_public(agent, "a" * 32, visitor_record, "what does Alex build?")
    seed_public(agent, "b" * 32, other_record, "is Alex hiring?")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield Scope(
            ChatApi(client, {"X-API-Key": owner_key}, agent),
            audit,
            {"X-API-Key": visitor_key},
            visitor_record,
            {"X-API-Key": other_key},
        )


async def test_owner_lists_by_channel_with_tags(scope: Scope) -> None:
    owner = scope.owner
    mine = (await owner.sse({"message": Q}))[0][1]["conversationId"]

    async def listed(channel: str) -> dict[str, str]:
        response = await owner.client.get(
            f"/admin/v1/conversations?channel={channel}", headers=owner.headers
        )
        return {c["id"]: c["channel"] for c in items(response)}

    assert await listed("owner") == {mine: "owner"}
    assert await listed("public") == {"a" * 32: "public", "b" * 32: "public"}
    assert set((await listed("all")).values()) == {"owner", "public"}
    # The owner reads a visitor conversation, but it is read-only.
    url = f"/admin/v1/conversations/{'a' * 32}"
    detail = await owner.client.get(url, headers=owner.headers)
    assert detail.status_code == 200 and result(detail)["channel"] == "public"
    assert "visitorHash" not in result(detail)
    refused = await owner.client.delete(url, headers=owner.headers)
    assert refused.status_code == 409
    renamed = await owner.client.patch(url, json={"name": "nope"}, headers=owner.headers)
    assert renamed.status_code == 409


async def test_a_public_key_sees_only_its_own(scope: Scope) -> None:
    client = scope.owner.client
    listed = await client.get("/v1/conversations", headers=scope.visitor)
    assert [c["id"] for c in items(listed)] == ["a" * 32]
    own = await client.get(f"/v1/conversations/{'a' * 32}", headers=scope.visitor)
    assert own.status_code == 200 and len(result(own)["turns"]) == 2
    theirs = await client.get(f"/v1/conversations/{'b' * 32}", headers=scope.visitor)
    assert theirs.status_code == 404
    owner_one = (await scope.owner.sse({"message": Q}))[0][1]["conversationId"]
    hidden = await client.get(f"/v1/conversations/{owner_one}", headers=scope.visitor)
    assert hidden.status_code == 404
    renamed = await client.patch(
        f"/v1/conversations/{'a' * 32}", json={"name": "my-question"}, headers=scope.visitor
    )
    assert renamed.status_code == 200 and result(renamed)["name"] == "my-question"
    own_delete = await client.delete(f"/v1/conversations/{'b' * 32}", headers=scope.other)
    assert own_delete.status_code == 200  # the other key's own conversation
    assert items(await client.get("/v1/conversations", headers=scope.other)) == []
    audited = [e for e in scope.audit.entries if e["action"] == "conversation.delete"]
    assert audited == [
        {
            "actor": "key:other",
            "action": "conversation.delete",
            "target": "b" * 32,
            "channel": "public",
        }
    ]
    assert (await client.get("/v1/conversations")).status_code == 401


async def test_soft_delete_hides_blocks_continuing_and_audits(scope: Scope) -> None:
    owner = scope.owner
    conversation_id = (await owner.sse({"message": Q}))[0][1]["conversationId"]
    url = f"/admin/v1/conversations/{conversation_id}"
    deleted = await owner.client.delete(url, headers=owner.headers)
    assert deleted.status_code == 200 and result(deleted)["id"] == conversation_id
    assert (await owner.client.get(url, headers=owner.headers)).status_code == 404
    listed = await owner.client.get("/admin/v1/conversations?channel=all", headers=owner.headers)
    assert conversation_id not in {c["id"] for c in items(listed)}
    again = await owner.client.post(
        "/admin/v1/chat",
        json={"message": "and then?", "conversationId": conversation_id},
        headers=owner.headers,
    )
    assert again.status_code == 404
    assert (await owner.client.delete(url, headers=owner.headers)).status_code == 404
    entries = [e for e in scope.audit.entries if e["action"] == "conversation.delete"]
    assert [e["target"] for e in entries] == [conversation_id]
    # The stored turns stay; only the flag hides them.
    assert owner.agent.store.turns[conversation_id]
