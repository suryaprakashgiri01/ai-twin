"""Intake behaviour from owner testing (a publish conversation that kept redrafting itself):
thanks and remarks are not requests, a finished action is not drafted again, a failed one is
retried with its fields, and earlier replies reach the model with their real outcomes."""

from __future__ import annotations

from collections.abc import Callable

from app.agents.intake.confirm import classify_rules
from app.agents.prompts import intake as prompts
from app.exceptions import AppError, ErrorCode
from app.services.admin_ops import PublishResult
from app.services.conversations import Turn
from app.services.intake import history_line
from tests.ingestion import persona
from tests.intake.harness import Twin, extract

MakeTwin = Callable[..., Twin]


async def published_note(twin: Twin) -> str:
    cid = (await twin.say("Save a note about my rate limiter project")).conversation_id
    await twin.say("yes", cid)
    await twin.say("publish it", cid)
    done = await twin.say("yes", cid)
    assert done.result is not None and done.result["action"] == "publish"
    return cid


def scripts(*after: str) -> list[str]:
    return [
        extract("add_note", title="Rate limiter retro", body=persona.note(), category="project"),
        extract("publish", document="it"),
        *after,
    ]


async def test_thanks_after_a_publish_is_not_a_new_draft(make_twin: MakeTwin) -> None:
    # The small model reads "Thanks!" as another publish, as it did in the owner's session.
    twin = make_twin(scripts(extract("publish", document="it", reply="")))
    twin.h.classifier.label = "public"
    cid = await published_note(twin)
    thanks = await twin.say("Thanks!", cid)
    assert thanks.draft is None and thanks.done["status"] == "chat"
    assert [e["action"] for e in twin.audit.entries].count("document.publish") == 1


async def test_publishing_an_already_published_document_says_so(make_twin: MakeTwin) -> None:
    twin = make_twin(scripts(extract("publish", document="it")))
    twin.h.classifier.label = "public"
    cid = await published_note(twin)
    again = await twin.say("please publish it", cid)
    assert again.draft is None and "already published" in again.text
    assert again.done["status"] == "chat"


async def test_remark_on_a_pending_draft_is_read_again(make_twin: MakeTwin) -> None:
    assert classify_rules("I think its already published") == "new"
    twin = make_twin(scripts(extract("publish", document="it"), extract("publish", document="it")))
    twin.h.classifier.label = "public"
    cid = await published_note(twin)
    remark = await twin.say("I think its already published", cid)
    assert remark.draft is None and "already published" in remark.text


async def test_try_again_retries_the_failed_action_with_its_fields(make_twin: MakeTwin) -> None:
    twin = make_twin(scripts(extract(None, reply="Sure.")))
    twin.h.classifier.label = "public"
    cid = (await twin.say("Save a note about my rate limiter project")).conversation_id
    await twin.say("yes", cid)
    await twin.say("publish it", cid)
    real = twin.ops.publish
    calls = 0

    async def flaky(document_id: str, actor: str) -> PublishResult:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise AppError(ErrorCode.DEPENDENCY_UNAVAILABLE, "Qdrant is not reachable")
        return await real(document_id, actor)

    twin.ops.publish = flaky  # type: ignore[method-assign]
    failed = await twin.say("yes", cid)
    assert failed.done["status"] == "error" and "try again" in failed.text
    retry = await twin.say("could you please try again to publish", cid)
    assert retry.draft is not None and retry.draft["action"] == "publish"
    assert retry.draft["payload"]["document_title"] == "Rate limiter retro"
    done = await twin.say("yes", cid)
    assert done.result is not None and "published" in done.text


def test_history_lines_carry_real_outcomes_and_the_prompt_explains_them() -> None:
    done = Turn(seq=2, role="assistant", content="Published “X”.", route="intake:done")
    asked = Turn(seq=1, role="user", content="publish it")
    assert history_line(done)["content"] == "[done] Published “X”."
    assert history_line(asked) == {"role": "user", "content": "publish it"}
    assert "[done] means it already happened" in prompts.EXTRACT_V2
    assert "Never repeat an action" in prompts.EXTRACT_V2


async def test_hr_call_with_contact_and_naukri_invite(make_twin: MakeTwin) -> None:
    twin = make_twin(
        [
            extract(
                "add_record",
                kind="hr call",
                company="Acme Test",
                role="AI Engineer",
                date="yesterday",
                contact="Priya priya@acme.example",
            ),
            extract(
                "add_record", kind="invite", company="Globex Test", role="ML Engineer", date="today"
            ),
        ]
    )
    call = await twin.say(
        "Had an HR call with Acme Test yesterday for AI Engineer, HR was Priya priya@acme.example"
    )
    assert call.draft is not None
    payload = call.draft["payload"]
    assert (payload["kind"], payload["stage"]) == ("hr_call", "HR call")
    assert payload["contact"] == "Priya priya@acme.example"
    saved = await twin.say("yes", call.conversation_id)
    assert "contact: Priya priya@acme.example" in saved.text
    invite = await twin.say("Got an invite on naukri today from Globex Test for ML Engineer")
    assert invite.draft is not None
    assert invite.draft["payload"]["platform"] == "Naukri"
    assert invite.draft["payload"]["stage"] == "invited to apply"


# --- the owner's live session, replayed with a model that misses (null action, no fields) ---


async def test_record_reports_become_drafts_from_the_owners_words(make_twin: MakeTwin) -> None:
    twin = make_twin([extract(None, reply=""), extract(None, reply="")])
    call = await twin.say(
        "Had an HR call with Initech Test yesterday for a Platform Engineer role, "
        "the HR was Meera meera@initech.example"
    )
    assert call.draft is not None and call.draft["action"] == "add_record"
    payload = call.draft["payload"]
    assert payload["kind"] == "hr_call" and payload["company"] == "Initech Test"
    assert payload["role"] == "Platform Engineer" and payload["stage"] == "HR call"
    assert payload["contact"] == "Meera meera@initech.example"
    assert payload["date"] == "2026-10-18"  # yesterday in the harness clock
    invite = await twin.say(
        "Got an invite on naukri today from Globex Test for an ML Engineer role"
    )
    payload = invite.draft["payload"] if invite.draft else {}
    assert (payload.get("kind"), payload.get("platform")) == ("invite", "Naukri")
    assert (payload.get("company"), payload.get("role")) == ("Globex Test", "ML Engineer")


async def test_a_bare_yes_with_nothing_pending_is_not_a_draft(make_twin: MakeTwin) -> None:
    twin = make_twin([extract("add_note", title="yes", body="yes", category="experience")])
    reply = await twin.say("yes")
    assert reply.draft is None and "Nothing is waiting for a yes" in reply.text


async def test_remember_keeps_the_owners_words_and_reads_aliases(make_twin: MakeTwin) -> None:
    paraphrase = "Remember that I mean the Quorvex project when you say qrx"
    twin = make_twin([extract("remember", text=paraphrase, kind="alias")])
    reply = await twin.say("remember that when I say qrx I mean the Quorvex project")
    assert reply.draft is not None
    assert reply.draft["payload"] == {"text": "Qrx = the Quorvex project", "kind": "alias"}


async def test_publish_by_title_finds_a_published_document(make_twin: MakeTwin) -> None:
    twin = make_twin(scripts(extract("publish")))
    twin.h.classifier.label = "public"
    cid = await published_note(twin)
    again = await twin.say("please publish the Rate limiter retro note", cid)
    assert again.draft is None and "already published" in again.text
