"""intake-record-fields: labels, JSON paste, role/location fallbacks, tips, schemas."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from app.agents.graphs.intake import _location, _record_defaults, _role, _work_mode
from app.agents.intake.labels import (
    find_json_object,
    merge_labelled,
    parse_labels,
    validate_paste,
)
from app.agents.intake.suggest import suggest
from app.agents.prompts import intake as prompts
from app.core.config import Settings
from app.services.profile import RecordIn

TZ = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 10, 17, 20, 0, tzinfo=UTC)
RECORD_FIELDS = frozenset(
    {
        "kind",
        "company",
        "role",
        "stage",
        "date",
        "notes",
        "platform",
        "contact",
        "location",
        "work_mode",
        "extras",
    }
)

# Windows-log style messages (fake companies only).
WINDOWS_ROLE = (
    "I applied to the Senior AI Engineer \u2013 Inference & Agent Systems role at Arcana today"
)
WINDOWS_LABELS = (
    "Role: Backend Engineer\nlocation: Hyderabad, Telangana\nwork mode: remote\n"
    "in notes please mention referral from Ada"
)
WINDOWS_NOTES = "applied to Initech for a Data Engineer today. Job ID: 778812"


def test_record_in_keeps_location_work_mode_and_extras() -> None:
    record = RecordIn(
        kind="application",
        company="Arcana",
        role="Senior AI Engineer",
        stage="applied",
        date="2026-10-18",
        location="Hyderabad, Telangana",
        work_mode="remote",
        extras={"referral": "Ada", "salary range": "40-50 LPA"},
    )
    assert record.location == "Hyderabad, Telangana"
    assert record.work_mode == "remote"
    assert record.extras["referral"] == "Ada"


def test_extras_cap_and_length() -> None:
    with pytest.raises(ValidationError):
        RecordIn(
            kind="application",
            company="Arcana",
            role="AI",
            stage="applied",
            date="2026-10-18",
            extras={f"k{i}": "v" for i in range(11)},
        )
    with pytest.raises(ValidationError):
        RecordIn(
            kind="application",
            company="Arcana",
            role="AI",
            stage="applied",
            date="2026-10-18",
            extras={"k": "x" * 201},
        )


def test_parse_labels_known_and_extras() -> None:
    got = parse_labels(WINDOWS_LABELS, RECORD_FIELDS)
    assert got["role"] == "Backend Engineer"
    assert got["location"] == "Hyderabad, Telangana"
    assert got["work_mode"] == "remote"
    assert "referral from Ada" in got["notes"]
    extras = parse_labels("salary range: 40-50 LPA", RECORD_FIELDS)
    assert extras["extras"] == {"salary range": "40-50 LPA"}
    job = parse_labels("Job ID: 778812", RECORD_FIELDS)
    assert job["notes"] == "Job ID: 778812"


def test_labelled_values_win_in_merge() -> None:
    base = {"role": "Invented Title", "company": "Arcana"}
    labelled = parse_labels("Role: Backend Engineer", RECORD_FIELDS)
    merged = merge_labelled(base, labelled)
    assert merged["role"] == "Backend Engineer"


def test_json_paste_valid_and_invalid() -> None:
    raw = find_json_object(
        '{"kind":"application","company":"Initech","role":"DE","stage":"applied",'
        '"date":"2026-10-18","location":"Pune","workMode":"hybrid"}'
    )
    assert raw is not None
    kept, errors = validate_paste(raw, RecordIn, partial=False)
    assert not errors
    assert kept["work_mode"] == "hybrid"
    assert kept["location"] == "Pune"
    bad, bad_errors = validate_paste(
        {"kind": "application", "company": "X", "role": "Y", "stage": "z", "date": "not-a-day"},
        RecordIn,
        partial=False,
    )
    assert bad == {} and "date" in bad_errors


def test_windows_role_and_location_fallbacks() -> None:
    role = _role(WINDOWS_ROLE, "Arcana")
    assert role is not None
    assert "Inference" in role and "Agent Systems" in role
    assert _role(WINDOWS_ROLE, "Arcana") == role
    defaults = _record_defaults({}, WINDOWS_ROLE, TZ, NOW, adding=True)
    assert defaults.get("company") == "Arcana"
    assert defaults.get("role") == role
    assert defaults.get("kind") == "application"
    assert defaults.get("stage") == "applied"
    assert _location("for location Gurugram, Haryana, onsite") == "Gurugram, Haryana"
    assert _work_mode("hybrid role in Pune") == "hybrid"
    assert _work_mode("online interview") == "remote"


def test_job_id_lands_in_notes_not_contact() -> None:
    defaults = _record_defaults({}, WINDOWS_NOTES, TZ, NOW, adding=True)
    assert "778812" in str(defaults.get("notes") or "")
    assert defaults.get("contact") is None or "778812" not in str(defaults.get("contact"))


@pytest.mark.parametrize(
    ("kind", "needle"),
    [
        ("application", "application"),
        ("interview", "round"),
        ("invite", "invite"),
        ("hr_call", "call"),
    ],
)
def test_suggestions_follow_record_kind(kind: str, needle: str) -> None:
    tip = suggest({"action": "add_record", "record": {"kind": kind}})
    assert tip is not None and tip.action is None
    assert needle in tip.text.casefold()


def test_intake_v4_embeds_pydantic_schemas() -> None:
    assert prompts.PROMPT_VERSION == "intake-v4"
    draft = prompts.draft_schema_json()
    record = prompts.record_schema_json()
    assert "IntakeDraft" in draft or '"action"' in draft
    assert "work_mode" in record and "extras" in record and "location" in record
    rendered = prompts.EXTRACT_V2.format(
        today="2026-10-20 (Monday)",
        catalog="add_record",
        draft_schema=draft,
        record_schema=record,
        draft="",
        recent="",
    )
    assert "Schema Adherence" in rendered
    assert "work_mode" in rendered


def test_local_title_wait_setting() -> None:
    settings = Settings()
    assert settings.title_refresh_wait_seconds == 8.0
    assert settings.title_refresh_wait_local_seconds == 20.0
