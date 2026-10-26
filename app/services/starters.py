"""Conversation starters the owner saves in the console, per agent (`chat_starters`)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Protocol

from beanie import PydanticObjectId
from pydantic import ConfigDict, Field

from app.api.envelope import ApiModel
from app.db.documents import ChatStarter

StarterAgent = Literal["super", "ask", "prep", "intake", "public"]
STARTERS_MAX = 50


class StarterIn(ApiModel):
    model_config = ConfigDict(extra="forbid")

    agent: StarterAgent
    text: str = Field(min_length=1, max_length=1000)


class Starter(StarterIn):
    id: str
    created_at: datetime


class StarterStore(Protocol):
    async def list(self, agent: StarterAgent | None = None) -> list[Starter]: ...

    async def add(self, body: StarterIn) -> Starter: ...

    async def delete(self, starter_id: str) -> bool: ...


def _out(doc: ChatStarter) -> Starter:
    return Starter(id=str(doc.id), agent=doc.agent, text=doc.text, created_at=doc.created_at)


class MongoStarterStore:
    async def list(self, agent: StarterAgent | None = None) -> list[Starter]:
        query = ChatStarter.find(ChatStarter.agent == agent) if agent else ChatStarter.find_all()
        docs = await query.sort("+created_at").limit(STARTERS_MAX * 5).to_list()
        return [_out(d) for d in docs]

    async def add(self, body: StarterIn) -> Starter:
        doc = ChatStarter(agent=body.agent, text=body.text.strip())
        await doc.insert()
        return _out(doc)

    async def delete(self, starter_id: str) -> bool:
        try:
            doc = await ChatStarter.get(PydanticObjectId(starter_id))
        except Exception:  # not an ObjectId: nothing to delete
            return False
        if doc is None:
            return False
        await doc.delete()
        return True
