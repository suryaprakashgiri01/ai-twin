"""Owner-agent T3: typed read-only tools, `query_records` and `get_profile` (AC3)."""

from __future__ import annotations

from datetime import date, datetime

import pytest
from pydantic import ValidationError

from app.agents.tools.profile import ProfileTool
from app.agents.tools.records import (
    RecordsQuery,
    RecordsResult,
    build_pipeline,
    match_stage,
    parse_result,
)
from app.services.profile import ProfileIn
from tests.ingestion.fakes import MemoryProfileStore


def test_query_schema_is_closed_and_ordered() -> None:
    with pytest.raises(ValidationError):
        RecordsQuery.model_validate({"kind": "interview", "$where": "1"})
    with pytest.raises(ValidationError):
        RecordsQuery.model_validate({"kind": "coffee chat"})
    with pytest.raises(ValidationError):
        RecordsQuery(kind="interview", date_from=date(2026, 9, 30), date_to=date(2026, 9, 1))


def test_match_is_exact_case_insensitive_and_escaped() -> None:
    query = RecordsQuery(
        kind="interview",
        company="Acme (Widgets)",
        stage="onsite",
        date_from=date(2026, 9, 1),
        date_to=date(2026, 9, 30),
    )
    match = match_stage(query)
    assert match["kind"] == "interview"
    assert match["company"] == {"$regex": r"^Acme\ \(Widgets\)$", "$options": "i"}
    assert match["date"] == {"$gte": datetime(2026, 9, 1), "$lt": datetime(2026, 10, 1)}


def test_pipeline_counts_without_a_cap_and_groups_on_request() -> None:
    plain = build_pipeline(RecordsQuery(kind="application"))
    facets = plain[1]["$facet"]
    assert set(facets) == {"total", "items"}
    assert facets["total"] == [{"$count": "n"}]
    grouped = build_pipeline(RecordsQuery(kind="application", group_by="month"))
    group = grouped[1]["$facet"]["groups"][0]["$group"]
    assert group["_id"] == {"$dateToString": {"format": "%Y-%m", "date": "$date"}}


def test_parse_and_summary() -> None:
    query = RecordsQuery(kind="interview", date_from=date(2026, 9, 1), date_to=date(2026, 9, 30))
    row = {
        "total": [{"n": 3}],
        "items": [
            {
                "company": "Acme Widgets",
                "role": "Backend",
                "stage": "onsite",
                "date": datetime(2026, 9, 20),
            },
        ],
    }
    result = parse_result(query, row)
    assert result.total == 3
    text = result.summary()
    assert text.startswith("You have 3 interviews from 2026-09-01 to 2026-09-30.")
    assert "Acme Widgets" in text and "(latest 1 of 3 shown)" in text
    empty = parse_result(RecordsQuery(kind="application", company="Nope"), {})
    assert empty.total == 0 and empty.summary() == "You have 0 applications at Nope."
    grouped = RecordsResult.model_validate(
        {
            "query": {"kind": "application", "group_by": "stage"},
            "total": 1,
            "groups": [{"key": "applied", "count": 1}],
        }
    )
    assert grouped.summary() == "You have 1 application.\n- applied: 1"


async def test_profile_tool_reads_the_profile() -> None:
    store = MemoryProfileStore()
    await store.put(
        ProfileIn(
            availability="open", notice_period="30 days", preferred_roles=["Backend"], locations=[]
        ),
        None,
    )
    facts = await ProfileTool(store).get_profile()
    assert facts.availability == "open" and facts.preferred_roles == ["Backend"]
    assert "Locations: not set" in facts.render()
