"""observability-alerts T3: scores and tags on traces (AC3): grounding, output guard, intake
outcome, lead triage; guard rule names as tags; prompt versions as metadata."""

from __future__ import annotations

from typing import Any

import pytest

from app.agents.prompts.public import PROMPT_VERSION
from app.observability.tracing import TraceRun
from app.services.intake import _trace_intake
from tests.leads.harness import extraction, make_capture
from tests.observability.fakes import CapturedTraces, attrs_of
from tests.public.fakes import FakeJudge, make_public


def scores(captured: CapturedTraces) -> dict[str, Any]:
    return {s["name"]: s for s in captured.scores}


async def test_a_passed_public_answer_scores_guard_and_grounding(captured: CapturedTraces) -> None:
    await make_public().ask("What did you build?")
    got = scores(captured)
    assert got["output_guard"]["value"] == 1.0
    assert got["output_guard"]["data_type"] == "BOOLEAN"
    assert got["grounding"] == {
        "name": "grounding",
        "value": "supported",
        "data_type": "CATEGORICAL",
        "comment": None,
    }
    [root] = captured.roots()
    assert attrs_of(root)["langfuse.trace.metadata.prompt_version"] == PROMPT_VERSION


async def test_a_blocked_answer_scores_zero_and_tags_its_rule(captured: CapturedTraces) -> None:
    await make_public(judge=FakeJudge(False)).ask("What did you build?")
    got = scores(captured)
    assert got["output_guard"]["value"] == 0.0
    assert got["output_guard"]["comment"] == "grounding_unsupported"
    assert got["grounding"]["value"] == "unsupported"
    [root] = captured.roots()
    assert "rule:grounding_unsupported" in attrs_of(root)["langfuse.trace.tags"]


async def test_a_cached_answer_is_not_scored_twice(captured: CapturedTraces) -> None:
    public = make_public()
    await public.ask("What did you build?")
    captured.scores.clear()
    await public.ask("What did you build?")
    assert captured.scores == []
    tags = [attrs_of(r)["langfuse.trace.tags"] for r in captured.roots()]
    assert any("cache_hit" in t for t in tags)


async def test_lead_triage_is_scored_with_its_confidence(captured: CapturedTraces) -> None:
    c = make_capture(
        [
            extraction(role="backend", company="Acme", name="Ravi Kumar"),
            extraction(message="We need a senior backend engineer for payment APIs."),
            extraction(email="ravi.kumar@acme.example"),
            "Ravi from Acme is hiring.",
        ]
    )
    for line in (
        "I'm hiring for a backend role at Acme, I'm Ravi Kumar",
        "We need a senior backend engineer for payment APIs.",
        "ravi.kumar@acme.example",
        "yes",
    ):
        await c.say(line)
    got = scores(captured)
    assert got["lead_triage"]["value"] == "recruiter"
    assert got["lead_triage"]["data_type"] == "CATEGORICAL"
    assert "lead_triage_confidence" in got


class RecordingRun(TraceRun):
    def __init__(self) -> None:
        self.scores: list[tuple[str, Any]] = []
        self.tags: list[str] = []

    def score(self, name: str, value: Any, **_: Any) -> None:
        self.scores.append((name, value))

    def tag(self, *tags: str) -> None:
        self.tags.extend(tags)


@pytest.mark.parametrize(
    ("status", "pending", "outcome"),
    [
        ("done", False, "written"),
        ("cancelled", False, "discarded"),
        ("asked", False, "asked"),
        ("draft", True, "asked"),
        ("chat", False, None),
        ("answer", False, None),
    ],
)
def test_intake_outcome_score(status: str, pending: bool, outcome: str | None) -> None:
    run = RecordingRun()
    _trace_intake(run, {"status": status, "reply": "ok"}, pending, resumed=False)
    assert run.tags == [f"status:{status}"]
    assert run.scores == ([("intake_outcome", outcome)] if outcome else [])
