"""owner-intake T3: dates, missing fields, inbox resolution, reply rules and suggestions."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from app.agents.intake.actions import ACTIONS, json_safe
from app.agents.intake.confirm import classify_rules, retype_matches
from app.agents.intake.drafts import InboxError, check, merge, normalise, parse_day, resolve_inbox
from app.agents.intake.suggest import suggest

TZ = ZoneInfo("Asia/Kolkata")
# 01:30 on Sunday 18 Oct in India, still Saturday 17 Oct in UTC.
NOW = datetime(2026, 10, 17, 20, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("yesterday", date(2026, 10, 17)),
        ("today", date(2026, 10, 18)),
        ("last Friday", date(2026, 10, 16)),
        ("friday", date(2026, 10, 16)),
        ("last sunday", date(2026, 10, 11)),
        ("12 Oct", date(2026, 10, 12)),
        ("12/10", date(2026, 10, 12)),
        ("3/10/2026", date(2026, 10, 3)),
        ("2026-10-03", date(2026, 10, 3)),
        ("3 days ago", date(2026, 10, 15)),
    ],
)
def test_dates_resolve_in_the_owner_zone_day_first(text: str, expected: date) -> None:
    assert parse_day(text, TZ, NOW) == expected


def test_yesterday_depends_on_the_zone() -> None:
    assert parse_day("yesterday", ZoneInfo("UTC"), NOW) == date(2026, 10, 16)
    assert parse_day("yesterday", TZ, NOW) == date(2026, 10, 17)


def test_unreadable_date_is_invalid_not_guessed() -> None:
    payload, invalid = normalise(ACTIONS["add_record"], {"date": "the blue moon"}, TZ, NOW)
    assert "date" in invalid and "date" not in payload


def test_missing_fields_are_computed_by_code() -> None:
    spec = ACTIONS["add_record"]
    payload, _ = normalise(spec, merge({}, {"kind": "Interview", "company": "Acme"}), TZ, NOW)
    result = check(spec, payload)
    assert result.missing == ["role", "stage", "date"] and not result.complete


def test_invalid_values_are_dropped_and_asked_again() -> None:
    spec = ACTIONS["add_record"]
    raw = {"kind": "meeting", "company": "Acme", "role": "SRE", "stage": "1", "date": "today"}
    payload, _ = normalise(spec, raw, TZ, NOW)
    result = check(spec, payload)
    assert set(result.invalid) == {"kind"} and "kind" not in result.payload


def test_merge_never_erases_with_empty_values() -> None:
    assert merge({"company": "Acme"}, {"company": "", "role": None, "stage": "2"}) == {
        "company": "Acme",
        "stage": "2",
    }


def test_unknown_fields_are_ignored() -> None:
    payload, _ = normalise(ACTIONS["add_record"], {"salary": "lots", "company": "A"}, TZ, NOW)
    assert payload == {"company": "A"}


def test_summary_and_json_show_the_resolved_date() -> None:
    spec = ACTIONS["add_record"]
    payload, _ = normalise(spec, {"company": "Acme", "date": "yesterday"}, TZ, NOW)
    assert "Sat 17 Oct 2026" in spec.summary(payload)
    assert json_safe(payload)["date"] == "2026-10-17"


def test_inbox_accepts_a_file_by_name(tmp_path: Path) -> None:
    (tmp_path / "resume.pdf").write_bytes(b"%PDF")
    assert resolve_inbox(tmp_path, "resume.pdf") == (tmp_path / "resume.pdf").resolve()


@pytest.mark.parametrize(
    "name", ["../.env", "/etc/passwd", "a/b.pdf", "a\\b.pdf", "..", ".", "", "C:x.pdf"]
)
def test_inbox_refuses_paths(tmp_path: Path, name: str) -> None:
    with pytest.raises(InboxError):
        resolve_inbox(tmp_path, name)


def test_inbox_refuses_symlinks_and_folders(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (tmp_path / "secret").write_text("x")
    (inbox / "link.pdf").symlink_to(tmp_path / "secret")
    (inbox / "folder").mkdir()
    for name in ("link.pdf", "folder", "missing.pdf"):
        with pytest.raises(InboxError):
            resolve_inbox(inbox, name)


@pytest.mark.parametrize(
    ("reply", "decision"),
    [
        ("yes", "yes"),
        ("Yes, go ahead!", "yes"),
        ("ok", "yes"),
        ("delete it", "yes"),
        ("no", "no"),
        ("nope, cancel", "no"),
        ("never mind", "no"),
        ("change the stage to round 3", "edit"),
        ("yes but the date should be Friday", "edit"),
        ("actually it was Globex", "edit"),
        ("that all looks right to me", None),
        ("what?", None),
    ],
)
def test_reply_rules(reply: str, decision: str | None) -> None:
    assert classify_rules(reply) == decision


def test_retype_must_match_exactly_apart_from_case_and_quotes() -> None:
    assert retype_matches("Acme Widgets", "  “acme   widgets”. ")
    assert not retype_matches("Acme Widgets", "Acme")
    assert not retype_matches("", "")


def test_suggestions_are_deterministic() -> None:
    doc = {
        "id": "d1",
        "title": "Talk",
        "tier": "public",
        "review_status": "pending",
        "published": False,
        "quarantine_rules": [],
    }
    outcome = {"action": "add_note", "sources": [{"documents": [doc]}]}
    tip = suggest(outcome)
    assert tip is not None and tip.action == "approve_publish"
    assert tip.payload == {"document": "d1", "document_id": "d1", "document_title": "Talk"}
    private = {**doc, "tier": "private"}
    assert suggest({"action": "add_note", "sources": [{"documents": [private]}]}) is None
    record = suggest({"action": "add_record"})
    assert record is not None and record.action is None
    assert suggest({"action": "publish"}) is None


def test_grounding_keeps_the_owners_words_only() -> None:
    from app.agents.intake.drafts import ground, grounded

    message = "It was for the Platform Engineer role, round 2, yesterday"
    assert grounded("Platform Engineer", message)
    assert grounded("round 2", message)
    assert not grounded("screening", message)
    assert not grounded("Software Engineer", message)
    spec = ACTIONS["add_record"]
    kept, dropped = ground(
        spec,
        {
            "kind": "interview",
            "role": "Platform Engineer",
            "stage": "screening",
            "date": "2026-10-12",
            "notes": "Great team",
        },
        message,
        {},
        TZ,
        NOW,
    )
    assert kept["kind"] == "interview" and kept["role"] == "Platform Engineer"
    assert kept["date"] == datetime(2026, 10, 17)  # the phrase in the message, not the guess
    assert sorted(dropped) == ["notes", "stage"]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("had my second round yesterday, system design", date(2026, 10, 17)),
        ("last Friday screen with Globex", date(2026, 10, 16)),
        ("interview on 12/10 for SRE", date(2026, 10, 12)),
        ("Add an interview with Quorvex Labs", None),
    ],
)
def test_find_day_in_free_text(text: str, expected: date | None) -> None:
    from app.agents.intake.drafts import find_day

    assert find_day(text, TZ, NOW) == expected


def test_model_yes_needs_a_yes_word() -> None:
    from app.agents.intake.confirm import checked

    assert checked("yes", "that all looks right to me") == "yes"
    assert checked("yes", "it was for the platform role") is None
    assert checked("no", "it was for the platform role") == "edit"
    assert checked("no", "please don't save that") == "no"


def test_draft_schema_is_lenient_with_small_models() -> None:
    from app.agents.intake.drafts import IntakeDraft

    odd = IntakeDraft.model_validate({"action": "Delete", "payload": None, "reply": None})
    assert (odd.action, odd.payload, odd.reply) == (None, {}, "")
    ok = IntakeDraft.model_validate({"action": " Add_Record ", "payload": {"company": "A"}})
    assert ok.action == "add_record"
    assert len(IntakeDraft(reply="x" * 5000).reply) <= 5000
    assert "add_record" in str(IntakeDraft.model_json_schema())


async def test_extraction_is_retried_once(make_twin: Any) -> None:
    from tests.intake.harness import extract

    twin = make_twin(["{", "{", extract("add_record", company="Acme Widgets")])
    reply = await twin.say("Interview with Acme Widgets")
    assert reply.done["status"] == "asked" and twin.model_calls == 3
