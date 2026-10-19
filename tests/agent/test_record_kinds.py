"""Job-search record kinds beyond application/interview: routing, normalisation, validation."""

from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from app.agents.routing import canonical_kind, heuristic_route
from app.services.profile import RecordIn

TODAY = date(2026, 10, 18)


@pytest.mark.parametrize(
    ("question", "kind"),
    [
        ("how many take-home assignments did I get this month?", "assessment"),
        ("how many online tests in October?", "assessment"),
        ("how many offers did I get this year?", "offer"),
        ("how many times was I rejected in September?", "rejection"),
        ("list my withdrawals this month", "withdrawal"),
        ("how many interviews this month?", "interview"),
        ("which companies did I apply to in October?", "application"),
        ("how many HR calls did I have this month?", "hr_call"),
        ("list my recruiter screens this week", "hr_call"),
        ("how many invites did I get from Naukri this month?", "invite"),
        ("how many times was I invited to apply in October?", "invite"),
    ],
)
def test_records_questions_route_to_the_right_kind(question: str, kind: str) -> None:
    decision = heuristic_route(question, None, TODAY)
    assert decision.route == "records"
    assert decision.records is not None and decision.records.kind == kind


@pytest.mark.parametrize(
    ("loose", "kind"),
    [
        ("take-home", "assessment"),
        ("Coding test", "assessment"),
        ("HackerRank", "assessment"),
        ("Interview", "interview"),
        ("got rejected", "rejection"),
        ("withdrew", "withdrawal"),
        ("offer", "offer"),
        ("applied", "application"),
        ("HR call", "hr_call"),
        ("hr_call", "hr_call"),
        ("invite", "invite"),
        ("invited", "invite"),
    ],
)
def test_intake_kind_wording_is_normalised(loose: str, kind: str) -> None:
    assert canonical_kind(loose) == kind


def test_unknown_kind_is_left_for_validation_to_name() -> None:
    assert canonical_kind("coffee chat") == "coffee chat"
    with pytest.raises(ValidationError):
        RecordIn(kind="coffee chat", company="Acme", role="AI", stage="x", date="2026-10-18")


@pytest.mark.parametrize(
    "kind",
    [
        "invite",
        "application",
        "hr_call",
        "assessment",
        "interview",
        "offer",
        "rejection",
        "withdrawal",
    ],
)
def test_every_kind_is_a_valid_record(kind: str) -> None:
    record = RecordIn(
        kind=kind, company="Acme Test", role="AI Engineer", stage="s", date="2026-10-18"
    )
    assert record.kind == kind


def test_platform_filter_and_grouping() -> None:
    from app.agents.routing import canonical_platform, records_query
    from app.agents.tools.records import RecordItem, RecordsQuery, RecordsResult, match_stage

    query = records_query("how many invites from naukri this month?", TODAY)
    assert query is not None and query.platform == "Naukri" and query.kind == "invite"
    assert match_stage(query)["platform"] == {"$regex": "^Naukri$", "$options": "i"}
    by_site = records_query("how many invites per platform this year?", TODAY)
    assert by_site is not None and by_site.group_by == "platform"
    assert canonical_platform("naukri.com") == "Naukri"
    assert canonical_platform("a friend") == "a friend"
    result = RecordsResult(
        query=RecordsQuery(kind="hr_call"),
        total=1,
        items=[
            RecordItem(
                company="Acme Test", role="AI", stage="HR call", date=TODAY, platform="LinkedIn"
            )
        ],
    )
    assert result.summary().startswith("You have 1 HR call.")
    assert "via LinkedIn" in result.summary()


def test_contact_is_stored_but_never_in_an_ask_chat_records_answer() -> None:
    from app.agents.tools.records import RecordsQuery, build_pipeline

    record = RecordIn(
        kind="hr_call",
        company="Acme Test",
        role="AI Engineer",
        stage="HR call",
        date="2026-10-18",
        contact="Priya, +91 90000 00000",
    )
    assert record.contact == "Priya, +91 90000 00000"
    projection = build_pipeline(RecordsQuery(kind="hr_call"))[1]["$facet"]["items"][2]["$project"]
    assert "contact" not in projection
