"""Memory service: learning (owner words only, mask, filter, dedupe, pending), explicit adds,
accept / edit / archive / delete, audit entries, and no memory text in logs."""

from __future__ import annotations

import json

import pytest
from structlog.testing import capture_logs

from app.exceptions import AppError
from app.services.digest import MemoryCandidate
from app.services.memory import MemoryIn, MemoryPatch, MemoryService, normalise, supported
from llm_orchestrator import Tier
from tests.memory.fakes import FakeAudit, FakeMemoryStore, WordEmbedder

OWNER = (
    "Please answer in concise bullets with sources first. "
    "When I say magnus-eval I mean the Magnus Eval project. "
    "Always show dates like 12 Oct 2026. Always call me on +91 98765 43210 if urgent."
)


def service(**kw: object) -> tuple[MemoryService, FakeMemoryStore, FakeAudit]:
    store, audit = FakeMemoryStore(), FakeAudit()
    embedder = kw.pop("embedder", WordEmbedder())
    return MemoryService(store, audit, embedder=embedder, **kw), store, audit  # type: ignore[arg-type]


def cand(kind: str, text: str, confidence: float = 0.7) -> MemoryCandidate:
    return MemoryCandidate(kind=kind, text=text, confidence=confidence)


async def learn(svc: MemoryService, *candidates: MemoryCandidate, tier: Tier = Tier.PRIVATE) -> int:
    return await svc.learn(candidates, owner_text=OWNER, conversation_id="c1", tier=tier)


async def test_learned_candidates_are_filtered_masked_and_pending() -> None:
    svc, store, audit = service()
    new = await learn(
        svc,
        cand("preference", "Concise bullets with sources first"),
        cand("instruction", "Always show dates like 12 Oct 2026"),
        cand("alias", "magnus-eval = Magnus Eval project"),
        cand("fact", "Works at Globex"),  # unknown kind
        cand("preference", "x " * 150),  # over the cap
        cand("preference", "Prefers long essays with no sources"),  # not the owner's words
        cand("instruction", "Always call me on +91 98765 43210"),
    )
    assert new == 4
    items = await svc.listing("pending")
    assert {m.kind for m in items} == {"preference", "instruction", "alias"}
    assert all(m.status == "pending" and m.tier == "private" for m in items)
    texts = {m.text for m in items}
    assert "Always call me on [PHONE]" in texts  # identifiers masked by the shared recognisers
    assert not any("98765" in t for t in texts)
    assert await svc.active() == []  # nothing learned is active
    assert {e[1] for e in audit.entries} == {"memory.learn"}
    assert all(e[0] == "agent:digest" for e in audit.entries)
    assert len(store.rows) == 4


async def test_only_owner_words_support_a_candidate() -> None:
    assert supported("Concise bullets, sources first", "give me concise bullets and sources first")
    assert not supported("Use tables for comparisons", "what did I learn about raft?")
    assert normalise("  Concise, BULLETS!  ") == "concise bullets"


async def test_duplicates_bump_instead_of_adding() -> None:
    svc, store, audit = service()
    await learn(svc, cand("preference", "Concise bullets with sources first"))
    # Same normalised text, then a near-duplicate by embedding similarity.
    assert await learn(svc, cand("preference", "concise bullets, with sources first!")) == 0
    assert await learn(svc, cand("preference", "Concise bullets with sources first please")) == 0
    (item,) = await svc.listing()
    assert item.uses == 3 and len(store.rows) == 1
    assert [e[1] for e in audit.entries] == ["memory.learn", "memory.bump", "memory.bump"]


async def test_dedupe_falls_back_to_exact_match_without_embeddings() -> None:
    svc, store, _ = service(embedder=WordEmbedder(fail=True))
    await learn(svc, cand("preference", "Concise bullets with sources first"))
    await learn(svc, cand("preference", "CONCISE bullets with sources first."))
    assert len(store.rows) == 1


async def test_confidential_conversation_gives_confidential_memories() -> None:
    svc, _, _ = service()
    await learn(
        svc, cand("preference", "Concise bullets with sources first"), tier=Tier.CONFIDENTIAL
    )
    (item,) = await svc.listing()
    assert item.tier == "confidential"


async def test_auto_accept_activates_only_confident_candidates() -> None:
    svc, _, _ = service(auto_accept=True)
    new = await learn(
        svc,
        cand("preference", "Concise bullets with sources first", confidence=0.85),
        cand("instruction", "Always show dates like 12 Oct 2026", confidence=0.79),
    )
    assert new == 1
    assert [m.kind for m in await svc.active()] == ["preference"]
    assert [m.kind for m in await svc.listing("pending")] == ["instruction"]


async def test_explicit_add_is_active_and_a_duplicate_activates_the_pending_one() -> None:
    svc, _, audit = service()
    item, created = await svc.add(
        MemoryIn(text="Use British spelling", kind="instruction"), "key:owner"
    )
    assert created and item.status == "active" and item.confidence == 1.0
    await learn(svc, cand("preference", "Concise bullets with sources first"))
    (pending,) = await svc.listing("pending")
    again, created = await svc.add(MemoryIn(text="concise bullets with sources first"), "key:owner")
    assert not created and again.id == pending.id and again.status == "active"
    assert audit.entries[0][:2] == ("key:owner", "memory.add")


async def test_accept_edit_archive_delete_are_audited() -> None:
    svc, store, audit = service()
    await learn(svc, cand("preference", "Concise bullets with sources first"))
    (item,) = await svc.listing()
    assert (await svc.accept(item.id, "key:owner")).status == "active"
    edited = await svc.update(
        item.id, MemoryPatch(text="Short bullets, sources first"), "key:owner"
    )
    assert edited.text == "Short bullets, sources first"
    assert store.rows[item.id]["norm"] == "short bullets sources first"
    assert (await svc.archive(item.id, "key:owner")).status == "archived"
    assert await svc.listing() == []  # archived items are out of the live list
    await svc.delete(item.id, "key:owner")
    with pytest.raises(AppError):
        await svc.get(item.id)
    actions = [e[1] for e in audit.entries]
    assert actions == [
        "memory.learn",
        "memory.accept",
        "memory.edit",
        "memory.archive",
        "memory.delete",
    ]
    assert all("text" not in json.dumps(e[3]) or "fields" in e[3] for e in audit.entries)


async def test_edit_into_an_existing_memory_conflicts() -> None:
    svc, _, _ = service()
    first, _ = await svc.add(MemoryIn(text="Use British spelling"), "key:owner")
    second, _ = await svc.add(MemoryIn(text="Use tables"), "key:owner")
    with pytest.raises(AppError) as err:
        await svc.update(second.id, MemoryPatch(text="use british spelling"), "key:owner")
    assert err.value.code.value == "CONFLICT"
    assert first.id != second.id


async def test_no_memory_text_in_logs() -> None:
    svc, _, _ = service()
    with capture_logs() as logs:
        await learn(svc, cand("preference", "Concise bullets with sources first"))
        await svc.add(MemoryIn(text="Use British spelling"), "key:owner")
    text = json.dumps(logs, default=str)
    assert "Concise" not in text and "British" not in text
    assert any(e["event"] == "memories learned" for e in logs)


async def test_pending_count_is_per_conversation() -> None:
    svc, _, _ = service()
    await learn(svc, cand("preference", "Concise bullets with sources first"))
    await svc.learn(
        [cand("alias", "magnus-eval = Magnus Eval project")],
        owner_text=OWNER,
        conversation_id="c2",
        tier=Tier.PRIVATE,
    )
    assert await svc.pending_count("c1") == 1 and await svc.pending_count("c3") == 0


def test_facts_and_loose_aliases_are_not_preference_shaped() -> None:
    from app.services.memory import preference_shaped

    assert not preference_shaped("instruction", "learned about Raft leader election")
    assert not preference_shaped("preference", "The owner asked about notice period")
    assert not preference_shaped("alias", "magnus-eval Magnus Eval project")
    assert preference_shaped("alias", "rl-svc means the rate limiter service")
    assert not preference_shaped("alias", "it = the Lumenrail observability platform note")
    assert preference_shaped("instruction", "Always show dates as 12 Oct 2026")
    assert preference_shaped("preference", "Concise bullets with sources first")
