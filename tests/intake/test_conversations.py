"""owner-intake: scripted multi-turn conversations with the mock local model (AC 2-10)."""

from __future__ import annotations

from collections.abc import Callable, MutableMapping
from pathlib import Path
from typing import Any

import pytest

from app.exceptions import AppError, ErrorCode
from app.services.profile import RecordIn
from tests.agent.harness import persona_records
from tests.ingestion import persona
from tests.intake.harness import KEY, Twin, extract, route

MakeTwin = Callable[..., Twin]


async def test_missing_fields_then_draft_then_yes_writes_once(make_twin: MakeTwin) -> None:
    twin = make_twin(
        [
            extract("add_record", kind="interview", company="Acme Widgets"),
            extract("add_record", role="Backend Engineer", stage="round 2", date="yesterday"),
        ]
    )
    first = await twin.say("Add an interview with Acme Widgets")
    cid = first.conversation_id
    assert first.done["status"] == "asked"
    assert all(f"- {f}:" in first.text for f in ("role", "stage", "date"))
    assert "company" not in first.text.split("need:")[1]
    assert first.draft is None and twin.records.rows == {}

    second = await twin.say("Backend engineer, round 2, yesterday", cid)
    assert second.draft is not None and second.draft["kind"] == "draft"
    assert "Sat 17 Oct 2026" in second.draft["summary"]
    assert second.draft["payload"]["date"] == "2026-10-17"
    assert twin.records.rows == {}

    third = await twin.say("yes", cid)
    assert third.done["status"] == "done"
    assert len(twin.records.rows) == 1
    record = next(iter(twin.records.rows.values()))
    assert (record.company, record.stage) == ("Acme Widgets", "round 2")
    entries = [e for e in twin.audit.entries if e["action"] == "record.create"]
    assert [e["actor"] for e in entries] == [f"agent:intake via key:{KEY}"]
    assert "Anything else from this round?" in third.text

    again = await twin.say("yes", cid)
    assert len(twin.records.rows) == 1
    assert again.result is None


ACME = "Interview with Acme Widgets for Backend Engineer, round 2, on 2026-10-16"


def interview(**overrides: Any) -> str:
    fields = {
        "kind": "interview",
        "company": "Acme Widgets",
        "role": "Backend Engineer",
        "stage": "round 2",
        "date": "2026-10-16",
    }
    return extract("add_record", **(fields | overrides))


async def test_no_discards_the_draft(make_twin: MakeTwin) -> None:
    twin = make_twin([interview()])
    draft = await twin.say(ACME)
    assert draft.draft is not None
    calls = twin.model_calls
    gone = await twin.say("no", draft.conversation_id)
    assert gone.done["status"] == "cancelled" and "nothing was saved" in gone.text
    assert twin.model_calls == calls  # the rules read "no"
    assert twin.records.rows == {}
    assert (await twin.service.state(draft.conversation_id)).pending is None


async def test_edit_updates_the_draft_and_asks_again(make_twin: MakeTwin) -> None:
    twin = make_twin([interview(), extract("add_record", stage="round 3")])
    cid = (await twin.say(ACME)).conversation_id
    edited = await twin.say("Change the stage to round 3", cid)
    assert edited.draft is not None
    assert edited.draft["payload"]["stage"] == "round 3"
    assert edited.draft["payload"]["company"] == "Acme Widgets"
    assert twin.records.rows == {}
    await twin.say("yes", cid)
    assert [r.stage for r in twin.records.rows.values()] == ["round 3"]


async def test_unclear_reply_goes_to_the_model_then_reasks(make_twin: MakeTwin) -> None:
    twin = make_twin([interview(), '{"decision": "garbage"}', '{"decision": "garbage"}'])
    cid = (await twin.say(ACME)).conversation_id
    unclear = await twin.say("hmm, what was that again", cid)
    assert unclear.draft is not None and unclear.draft["reask"] is True
    assert twin.records.rows == {}


async def test_model_reading_yes_from_free_text(make_twin: MakeTwin) -> None:
    twin = make_twin([interview(), '{"decision": "yes"}'])
    cid = (await twin.say(ACME)).conversation_id
    await twin.say("that all looks right to me, thank you", cid)
    assert len(twin.records.rows) == 1


async def test_delete_record_needs_the_company_retyped(make_twin: MakeTwin) -> None:
    twin = make_twin([interview(), extract("delete_record", record="acme")])
    cid = (await twin.say(ACME)).conversation_id
    await twin.say("yes", cid)
    assert len(twin.records.rows) == 1
    asked = await twin.say("Delete the Acme interview", cid)
    assert asked.draft is not None and asked.draft["kind"] == "draft"
    retype = await twin.say("yes", cid)
    assert retype.draft is not None and retype.draft["kind"] == "retype"
    assert "Acme Widgets" in retype.draft["prompt"]
    assert len(twin.records.rows) == 1
    wrong = await twin.say("Acme", cid)
    assert wrong.done["status"] == "cancelled" and "nothing was deleted" in wrong.text
    assert len(twin.records.rows) == 1

    await twin.say("Delete the Acme interview", cid)
    await twin.say("yes", cid)
    done = await twin.say("acme widgets", cid)
    assert done.done["status"] == "done"
    assert twin.records.rows == {}
    assert [e["actor"] for e in twin.audit.entries if e["action"] == "record.delete"] == [
        f"agent:intake via key:{KEY}"
    ]


async def test_delete_document_needs_the_title_retyped(make_twin: MakeTwin) -> None:
    twin = make_twin(
        [
            extract("add_note", title="Raft notes", body="Leader election recap", category="blog"),
            extract("delete_document", document="Raft notes"),
        ]
    )
    cid = (await twin.say("Note: leader election recap")).conversation_id
    await twin.say("yes", cid)
    doc_id = next(iter(twin.h.store.documents))
    await twin.say("delete the raft notes document", cid)
    await twin.say("yes", cid)
    await twin.say("Raft", cid)
    assert twin.h.store.documents[doc_id].deleted_at is None
    await twin.say("delete the raft notes document", cid)
    await twin.say("yes", cid)
    await twin.say("“Raft notes”", cid)
    assert twin.h.store.documents[doc_id].deleted_at is not None


# --- inbox (AC 5) --------------------------------------------------------------------------------


async def test_ingest_file_from_the_inbox(make_twin: MakeTwin) -> None:
    twin = make_twin([extract("ingest_file", filename="resume.pdf", category="resume")])
    (twin.inbox / "resume.pdf").write_bytes(persona.pdf_bytes("Kavya backend engineer resume"))
    draft = await twin.say("Ingest resume.pdf from the inbox as my resume")
    assert draft.draft is not None and draft.draft["payload"]["filename"] == "resume.pdf"
    done = await twin.say("yes", draft.conversation_id)
    assert done.result is not None
    source = done.result["sources"][0]
    assert source["status"] == "done" and len(source["documents"]) == 1
    assert source["source_id"] in done.text and "proposed tier" in done.text


@pytest.mark.parametrize("name", ["../.env", "/etc/passwd", "sub/resume.pdf", "C:\\x.pdf"])
async def test_ingest_file_refuses_paths_without_reading(
    make_twin: MakeTwin, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    reads: list[Path] = []
    original = Path.read_bytes

    def spy(self: Path) -> bytes:
        reads.append(self)
        return original(self)

    monkeypatch.setattr(Path, "read_bytes", spy)
    twin = make_twin([extract("ingest_file", filename=name, category="resume")])
    reply = await twin.say(f"ingest {name}")
    assert reply.done["status"] == "asked" and reply.draft is None
    assert "not a path" in reply.text
    assert reads == [] and twin.h.store.sources == {}


async def test_ingest_file_refuses_a_symlink_out_of_the_inbox(
    make_twin: MakeTwin, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = tmp_path / "outside.env"
    secret.write_text("SECRET=1")
    twin = make_twin([extract("ingest_file", filename="link.pdf", category="resume")])
    (twin.inbox / "link.pdf").symlink_to(secret)
    reads: list[Path] = []

    def spy(self: Path) -> bytes:
        reads.append(self)
        return b""

    monkeypatch.setattr(Path, "read_bytes", spy)
    reply = await twin.say("ingest link.pdf")
    assert reply.draft is None and "link" in reply.text
    assert reads == [] and twin.h.store.sources == {}


async def test_ingest_file_missing_from_the_inbox(make_twin: MakeTwin) -> None:
    twin = make_twin([extract("ingest_file", filename="nope.pdf", category="resume")])
    reply = await twin.say("ingest nope.pdf")
    assert reply.draft is None and "No file named 'nope.pdf'" in reply.text


# --- ingest report and suggestion (AC 6) ---------------------------------------------------------


async def test_public_note_suggests_publish_and_waits_for_a_new_yes(make_twin: MakeTwin) -> None:
    twin = make_twin(
        [
            extract("add_note", title="Lumenrail talk", body=persona.note(), category="blog"),
            extract("add_record", kind="interview", company="Initech"),
        ]
    )
    twin.h.classifier.label = "public"
    cid = (await twin.say("Save a note about my Lumenrail talk")).conversation_id
    done = await twin.say("yes", cid)
    assert done.result is not None
    source = done.result["sources"][0]
    doc = source["documents"][0]
    assert (source["status"], doc["tier"], doc["review_status"]) == ("done", "public", "pending")
    assert source["source_id"] in done.text and "proposed tier public" in done.text
    assert done.draft is not None and done.draft["kind"] == "suggestion"
    assert done.draft["action"] == "approve_publish"
    stored = twin.h.store.documents
    assert all(d.review_status == "pending" and not d.published for d in stored.values())

    # Something else instead of yes: the suggestion is dropped, nothing is published.
    other = await twin.say("add an interview with Initech", cid)
    assert other.done["status"] == "asked"
    assert all(not d.published for d in stored.values())
    assert (await twin.service.state(cid)).pending is None


async def test_yes_to_the_suggestion_approves_and_publishes(make_twin: MakeTwin) -> None:
    twin = make_twin(
        [extract("add_note", title="Lumenrail talk", body=persona.note(), category="blog")]
    )
    twin.h.classifier.label = "public"
    cid = (await twin.say("Save a note about my Lumenrail talk")).conversation_id
    await twin.say("yes", cid)
    published = await twin.say("yes", cid)
    assert published.result is not None and published.result["action"] == "approve_publish"
    doc = next(iter(twin.h.store.documents.values()))
    assert doc.review_status == "approved" and doc.published
    actions = [e["action"] for e in twin.audit.entries]
    assert actions == ["ingest.submit", "document.approve", "document.publish"]


async def test_restricted_document_is_reported_as_file_only(make_twin: MakeTwin) -> None:
    twin = make_twin(
        [extract("add_note", title="Marks", body="Semester marks summary", category="marksheet")]
    )
    cid = (await twin.say("save my semester marks")).conversation_id
    done = await twin.say("yes", cid)
    assert done.result is not None
    assert done.result["sources"][0]["documents"][0]["tier"] == "restricted"
    assert "kept as a file only" in done.text
    assert done.draft is None


# --- restart (AC 7) ------------------------------------------------------------------------------


async def test_pending_draft_survives_restart_and_redis_flush(make_twin: MakeTwin) -> None:
    twin = make_twin([interview()])
    cid = (await twin.say(ACME)).conversation_id
    twin.restart()
    state = await twin.service.state(cid)
    assert state.pending is not None and "Acme Widgets" in state.pending.summary
    assert await twin.redis.keys("*") == []
    done = await twin.say("yes", cid)
    assert done.done["resumed"] is True and done.done["status"] == "done"
    twin.restart()
    again = await twin.say("yes", cid)
    assert again.result is None
    assert len(twin.records.rows) == 1
    turns = await twin.history(cid)
    assert [t.role for t in turns[:4]] == ["user", "assistant", "user", "assistant"]


# --- local only (AC 8) ---------------------------------------------------------------------------


async def test_refuses_without_a_local_model_and_calls_none(make_twin: MakeTwin) -> None:
    twin = make_twin([interview()], cloud_only=True)
    with pytest.raises(AppError) as caught:
        await twin.say(ACME)
    assert caught.value.code == ErrorCode.MODEL_UNAVAILABLE
    assert "make ollama ARGS=start" in caught.value.message
    assert twin.factory.models["cloud"].calls == 0
    assert twin.factory.models["local"].calls == 0


# --- answer (AC 9) -------------------------------------------------------------------------------


async def test_question_is_answered_by_the_owner_graph(make_twin: MakeTwin) -> None:
    october = [
        RecordIn.model_validate(
            {
                "kind": "interview",
                "company": c,
                "role": "Backend Engineer",
                "stage": "screen",
                "date": f"2026-10-{d}T10:00:00",
            }
        )
        for c, d in (("Acme Widgets", "06"), ("Globex Labs", "13"))
    ]
    twin = make_twin(
        [extract("answer", question="How many interviews in October?")],
        router=[
            route(
                "How many interviews in October?",
                {"kind": "interview", "date_from": "2026-10-01", "date_to": "2026-10-31"},
            )
        ],
        owner_rows=[*persona_records(), *october],
    )
    reply = await twin.say("How many interviews in October?")
    assert reply.done["status"] == "answer"
    assert "You have 2 interviews" in reply.text
    assert twin.owner_factory.models["owner-cloud"].calls == 0
    assert twin.owner_factory.models["owner-router"].calls == 1
    turns = await twin.history(reply.conversation_id)
    assert [t.role for t in turns] == ["user", "assistant"]
    assert turns[1].max_tier == "confidential"


# --- logs (AC 11) --------------------------------------------------------------------------------


async def test_no_conversation_text_or_payload_in_logs(
    make_twin: MakeTwin,
    intake_logs: list[MutableMapping[str, Any]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret = "ZEPHYR-QUILL"
    twin = make_twin(
        [
            extract("add_record", kind="interview", company=f"{secret} Corp"),
            extract("add_record", role=f"{secret} role", stage="round 1", date="yesterday"),
            '{"decision": "yes"}',
        ]
    )
    cid = (await twin.say(f"Interview at {secret} Corp")).conversation_id
    await twin.say(f"{secret} role, round 1, yesterday", cid)
    await twin.say(f"{secret}: sounds fine by me", cid)
    assert len(twin.records.rows) == 1
    assert intake_logs, "expected intake log lines"
    assert secret not in repr(intake_logs)
    assert secret not in capsys.readouterr().out


# --- regressions from the live run with llama3.2:3b -------------------------------------------


async def test_invented_values_are_dropped_and_asked_for(make_twin: MakeTwin) -> None:
    twin = make_twin(
        [
            interview(
                company="Quorvex Labs",
                role="Software Engineer",
                stage="screening",
                date="2026-10-12",
                notes="Impressive team and challenging questions.",
            )
        ]
    )
    reply = await twin.say("Add an interview with Quorvex Labs")
    assert reply.draft is None and reply.done["status"] == "asked"
    assert all(f"- {f}:" in reply.text for f in ("role", "stage", "date"))


async def test_model_no_without_a_no_word_is_an_edit(make_twin: MakeTwin) -> None:
    twin = make_twin(
        [
            extract("add_record", kind="interview", company="Quorvex Labs"),
            extract("add_record", role="Platform Engineer", stage="round 2", date="yesterday"),
            interview(company="Quorvex Labs", role="Platform Engineer", date="yesterday"),
            '{"decision": "no"}',
            extract("add_record", stage="round 3"),
        ]
    )
    cid = (await twin.say("Add an interview with Quorvex Labs")).conversation_id
    draft = await twin.say("It was for the Platform Engineer role, round 2, yesterday", cid)
    assert draft.draft is not None
    edited = await twin.say("hmm it was round 3 actually", cid)
    assert edited.done["status"] != "cancelled"


async def test_reference_copied_from_an_earlier_reply_resolves(make_twin: MakeTwin) -> None:
    twin = make_twin([interview(), extract("delete_record", record="placeholder")])
    cid = (await twin.say(ACME)).conversation_id
    await twin.say("yes", cid)
    record = next(iter(twin.records.rows.values()))
    line = f"Acme Widgets · Backend Engineer · round 2 · Fri 16 Oct 2026 ({record.id})"
    twin.factory.models["local"].script[1] = extract("delete_record", record=line)
    draft = await twin.say("Delete the Acme Widgets interview record", cid)
    assert draft.draft is not None and record.id in draft.draft["summary"]
    long_name = extract("delete_record", record="the Acme Widgets interview record")
    twin.factory.models["local"].script[1] = long_name
    await twin.say("no", cid)
    again = await twin.say("Delete the Acme Widgets interview record", cid)
    assert again.draft is not None and record.id in again.draft["summary"]


async def test_an_id_the_owner_typed_wins(make_twin: MakeTwin) -> None:
    twin = make_twin([interview(), extract("delete_record", record="Acme Widgets")])
    cid = (await twin.say(ACME)).conversation_id
    await twin.say("yes", cid)
    first = next(iter(twin.records.rows.values()))
    twin.factory.models["local"].script[0] = interview(stage="round 3")
    twin.factory.models["local"].calls = 0
    await twin.say(ACME.replace("round 2", "round 3"), cid)
    await twin.say("yes", cid)
    twin.factory.models["local"].calls = 1
    draft = await twin.say(f"Delete record {first.id}", cid)
    assert draft.draft is not None and first.id in draft.draft["summary"]


def test_missing_kind_lists_the_allowed_values() -> None:
    from app.agents.graphs.intake import _missing_text
    from app.agents.intake.actions import ACTIONS

    text = _missing_text(ACTIONS["add_record"], ["kind", "date"], {})
    assert "- kind: application, assessment, interview, offer, rejection or withdrawal" in text
    assert "owner" not in text and "your words" in text
