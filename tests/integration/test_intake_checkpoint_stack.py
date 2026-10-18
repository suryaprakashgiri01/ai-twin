"""owner-intake T2 on the stack: the MongoDB checkpointer keeps a pending interrupt across a
restart, resumes it once, prunes finished threads and keeps waiting ones; migration 0004."""

from __future__ import annotations

from datetime import timedelta
from typing import Any, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from app.agents.checkpoint import MongoCheckpointSaver
from app.core.resources import Resources
from app.db.documents import CheckpointEntry, utcnow
from app.migrations import m0004_conversation_mode
from tests.integration.conftest import TEST_DATABASE


class State(TypedDict, total=False):
    draft: dict[str, Any]
    answer: str
    executed: int


async def confirm(state: State) -> dict[str, Any]:
    reply = interrupt({"summary": state["draft"]["company"]})
    return {"answer": str(reply)}


async def execute(state: State) -> dict[str, Any]:
    return {"executed": state.get("executed", 0) + (1 if state["answer"] == "yes" else 0)}


def graph(saver: MongoCheckpointSaver) -> Any:
    builder = StateGraph(State)
    builder.add_node("confirm", confirm)
    builder.add_node("execute", execute)
    builder.add_edge(START, "confirm")
    builder.add_edge("confirm", "execute")
    builder.add_edge("execute", END)
    return builder.compile(checkpointer=saver)


def cfg(thread: str) -> RunnableConfig:
    return {"configurable": {"thread_id": thread}}


async def test_interrupt_survives_restart_and_resumes_once(resources: Resources) -> None:
    first = graph(MongoCheckpointSaver())
    out = await first.ainvoke({"draft": {"company": "Acme Widgets"}}, cfg("t1"))
    assert out["__interrupt__"][0].value == {"summary": "Acme Widgets"}
    # A new saver and graph: nothing in memory carries over.
    saver = MongoCheckpointSaver()
    assert await saver.waiting("t1")
    second = graph(saver)
    snapshot = await second.aget_state(cfg("t1"))
    assert snapshot.next == ("confirm",)
    done = await second.ainvoke(Command(resume="yes"), cfg("t1"))
    assert done["executed"] == 1
    assert not await saver.waiting("t1")
    assert (await second.aget_state(cfg("t1"))).next == ()
    history = [c async for c in saver.alist(cfg("t1"))]
    assert len(history) >= 3
    assert (
        history[0].config["configurable"]["checkpoint_id"]
        > history[-1].config["configurable"]["checkpoint_id"]
    )


async def test_prune_drops_old_finished_threads_only(resources: Resources) -> None:
    saver = MongoCheckpointSaver()
    g = graph(saver)
    await g.ainvoke({"draft": {"company": "Globex"}}, cfg("finished"))
    await g.ainvoke(Command(resume="no"), cfg("finished"))
    await g.ainvoke({"draft": {"company": "Initech"}}, cfg("waiting"))
    await g.ainvoke({"draft": {"company": "Umbrella"}}, cfg("recent"))
    await g.ainvoke(Command(resume="no"), cfg("recent"))
    old = utcnow() - timedelta(days=31)
    coll = CheckpointEntry.get_pymongo_collection()
    await coll.update_many(
        {"thread_id": {"$in": ["finished", "waiting"]}}, {"$set": {"updated_at": old}}
    )
    deleted = await saver.prune_finished(utcnow() - timedelta(days=30))
    assert deleted == 1
    threads = set(await coll.distinct("thread_id"))
    assert threads == {"waiting", "recent"}
    assert await saver.waiting("waiting")


async def test_migration_sets_ask_mode(resources: Resources) -> None:
    db = resources.mongo[TEST_DATABASE]
    await db["conversations"].insert_many(
        [
            {"_id": "c-old", "channel": "owner"},
            {"_id": "c-intake", "channel": "owner", "mode": "intake"},
        ]
    )
    await m0004_conversation_mode.up(db)
    await m0004_conversation_mode.up(db)
    modes = {d["_id"]: d["mode"] async for d in db["conversations"].find({}, {"mode": 1})}
    assert modes == {"c-old": "ask", "c-intake": "intake"}
