"""owner-memory AC10: the digest call is one Langfuse trace with owner content masked."""

from __future__ import annotations

from app.services.digest import ConversationDigester
from tests.agent.test_digest import Learner, MemoryIndex, digest_json, long_chat
from tests.llm_orchestrator.fakes import Factory, inventory, orchestrator, spec
from tests.observability.fakes import CapturedTraces, attrs_of

SUMMARY = "Owner asked about raft leader election twice."
MEMORY = "Concise bullets with sources first"


async def test_digest_trace_is_masked(captured: CapturedTraces) -> None:
    index = MemoryIndex()
    index.add("c1", long_chat(3))
    factory = Factory({"cloud": [digest_json(SUMMARY, [{"kind": "preference", "text": MEMORY}])]})
    orch = orchestrator(inventory(spec("cloud", max_tier="private")), factory)
    digester = ConversationDigester(index, orch, learner=Learner())
    await digester.refresh("c1")

    [root] = captured.roots()
    attrs = attrs_of(root)
    assert attrs["langfuse.trace.name"] == "conversation-digest"
    assert "channel:owner" in set(attrs["langfuse.trace.tags"])
    assert "[owner content masked: " in attrs["langfuse.trace.output"]
    sent = captured.text()
    for secret in (SUMMARY, MEMORY, "owner question 1", "raft"):
        assert secret not in sent
