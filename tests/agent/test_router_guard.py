"""Router-guard T1-T2: a model's records route and filters must be earned by the owner's words."""

from __future__ import annotations

from collections.abc import MutableMapping
from typing import Any

from app.agents.routing import RouteDecision, guard_records
from app.agents.tools.records import RecordsQuery
from llm_orchestrator import Tier
from tests.agent.harness import CONF_TEXT, TODAY, hit, make_agent, route_json

FOLLOW_UP = "and which other company am I weighing it against?"
OFFER = "what did the Initech Cloud offer say about base pay?"


def model_records(query: str, **filters: object) -> RouteDecision:
    records = RecordsQuery.model_validate({"kind": "application", **filters})
    return RouteDecision(route="records", query=query, records=records)


def test_unearned_records_route_falls_back_to_keywords() -> None:
    invented = model_records(
        "which companies",
        company="Globex Labs",
        stage="open",
        date_from="2026-01-01",
        date_to="2026-12-31",
    )
    result = guard_records(invented, FOLLOW_UP, OFFER, TODAY)
    assert result.guard == "records_rejected"
    assert result.decision.route == "kb_search"
    assert result.decision.query == FOLLOW_UP and result.decision.records is None


def test_counting_question_keeps_the_model_route() -> None:
    q = "how many interviews in September?"
    decision = model_records(q, date_from="2026-09-01", date_to="2026-09-30")
    result = guard_records(decision, q, None, TODAY)
    assert result.guard == "none" and result.decision is decision


def test_short_follow_up_inherits_the_previous_records_question() -> None:
    decision = model_records("how many applications in August?", date_from="2026-08-01")
    result = guard_records(decision, "and in August?", "how many applications in July?", TODAY)
    assert result.guard == "none" and result.decision.route == "records"


def test_unmentioned_filters_are_dropped_and_mentioned_ones_kept() -> None:
    q = "how many interviews with globex labs this month?"
    decision = model_records(q, company="Globex Labs", stage="open")
    result = guard_records(decision, q, None, TODAY)
    assert result.guard == "filters_dropped" and result.dropped == ("stage",)
    assert result.decision.records is not None
    assert result.decision.records.company == "Globex Labs"
    assert result.decision.records.stage is None


def test_other_routes_pass_through() -> None:
    decision = RouteDecision(route="kb_search", query=FOLLOW_UP)
    assert guard_records(decision, FOLLOW_UP, OFFER, TODAY).decision is decision


INVENTED = {
    "kind": "application",
    "company": "Globex Labs",
    "stage": "open",
    "date_from": "2026-01-01",
    "date_to": "2026-12-31",
}


def route_line(logs: list[MutableMapping[str, Any]]) -> MutableMapping[str, Any]:
    return next(e for e in logs if e.get("step") == "route")


async def test_follow_up_misrouted_to_records_searches_the_kb(
    owner_logs: list[MutableMapping[str, Any]],
) -> None:
    agent = make_agent(
        router=[route_json("kb_search", OFFER), route_json("records", "which companies", INVENTED)],
        local=["Base pay [1].", "Globex Labs [1]."],
        hits=[hit("conf", Tier.CONFIDENTIAL, CONF_TEXT)],
    )
    first, _ = await agent.run(OFFER)
    owner_logs.clear()
    state, _ = await agent.run(FOLLOW_UP, conversation_id=first["conversation_id"])
    assert state["route"] == "kb_search" and state["route_source"] == "model"
    assert agent.searcher.calls[-1]["query"] == FOLLOW_UP
    assert agent.records.calls == []
    assert state["answer"].startswith("Globex Labs [1].")
    line = route_line(owner_logs)
    assert line["guard"] == "records_rejected" and line["route"] == "kb_search"
    dumped = str(owner_logs)
    for text in (FOLLOW_UP, "which companies", "Globex Labs", OFFER):
        assert text not in dumped


async def test_invented_filters_are_dropped_before_the_records_tool(
    owner_logs: list[MutableMapping[str, Any]],
) -> None:
    q = "how many interviews in September?"
    filters = {"kind": "interview", "company": "Globex Labs", "stage": "open"}
    agent = make_agent(router=[route_json("records", q, filters | {"date_from": "2026-09-01"})])
    state, _ = await agent.run(q)
    assert state["route"] == "records"
    sent = agent.records.calls[0]
    assert (sent.company, sent.stage) == (None, None)
    line = route_line(owner_logs)
    assert line["guard"] == "filters_dropped" and line["dropped"] == ["company", "stage"]
    assert "Globex Labs" not in str(owner_logs)
