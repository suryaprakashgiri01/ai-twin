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
    ],
)
def test_intake_kind_wording_is_normalised(loose: str, kind: str) -> None:
    assert canonical_kind(loose) == kind


def test_unknown_kind_is_left_for_validation_to_name() -> None:
    assert canonical_kind("coffee chat") == "coffee chat"
    with pytest.raises(ValidationError):
        RecordIn(kind="coffee chat", company="Acme", role="AI", stage="x", date="2026-10-18")


@pytest.mark.parametrize(
    "kind", ["application", "assessment", "interview", "offer", "rejection", "withdrawal"]
)
def test_every_kind_is_a_valid_record(kind: str) -> None:
    record = RecordIn(
        kind=kind, company="Acme Test", role="AI Engineer", stage="s", date="2026-10-18"
    )
    assert record.kind == kind
