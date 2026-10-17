"""Deterministic red-team fixtures (`tests/redteam/*.yaml`) against the public agent with fake
models. Each case says which layer must stop it and what the visitor may see; CI runs them all.

Expectations:
- `blocked`: the input guard stops it; no router, retrieval or generation call.
- `refused`: a fixed reply for `intent`; no retrieval or generation call.
- `fallback`: a (scripted, compromised) generator answer is replaced by the safe fallback and
  the named security event is recorded.
- `answered`: passes every input layer and gets `intent` (default `about_owner`).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from app.agents.prompts.public import NO_CONTEXT
from app.guardrails.identifiers import verhoeff_check_digit
from app.guardrails.input import BLOCKED_REPLY
from app.guardrails.scope import FIXED_REPLIES
from llm_orchestrator import AllModelsFailedError, Role, Tier
from tests.public.fakes import ANSWER, MARKER, answer_of, make_public

HERE = Path(__file__).parent
EXPECTATIONS = {"blocked", "refused", "fallback", "answered"}
AADHAAR = "2345 6789 012" + verhoeff_check_digit("23456789012")


def _cases() -> list[Any]:
    params = []
    for path in sorted(HERE.glob("*.yaml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        for case in data["cases"]:
            case = {"category": data["category"], **case}
            params.append(pytest.param(case, id=case["id"]))
    return params


CASES = _cases()


def _fill(text: str) -> str:
    return text.replace("{canary}", MARKER).replace("{aadhaar}", AADHAAR)


def test_fixture_files_are_well_formed() -> None:
    ids = [p.values[0]["id"] for p in CASES]
    assert len(ids) == len(set(ids)), "duplicate case ids"
    categories = {p.values[0]["category"] for p in CASES}
    assert {
        "injection",
        "jailbreak",
        "prompt_extraction",
        "meta_probe",
        "out_of_scope",
        "action_request",
        "encoded_payload",
        "canary_exfiltration",
        "benign",
    } <= categories
    assert all(p.values[0]["expect"] in EXPECTATIONS for p in CASES)
    assert len(CASES) >= 70


@pytest.mark.parametrize("case", CASES)
async def test_redteam_case(case: dict[str, Any]) -> None:
    expect = case["expect"]
    label = case.get("classifier", case.get("intent", "about_owner"))
    public = make_public(
        label=label,
        guard_scores=[case.get("guard_score", 0.01)],
        generator=[_fill(case["generator"])] if "generator" in case else None,
    )
    events = await public.ask(_fill(case["message"]))
    answer = answer_of(events)
    intent = events[0][1]["intent"]
    no_model_work = public.searcher.calls == [] and public.generations() == 0

    if expect == "blocked":
        assert intent == "blocked" and answer == BLOCKED_REPLY
        assert no_model_work and public.classifier.seen == []
        [event] = public.events.events
        assert event["kind"] == "input_blocked"
        assert set(case.get("rules", [])) <= set(event["rules"])
        assert case["message"] not in str(event)
    elif expect == "refused":
        assert intent == case["intent"] and answer == FIXED_REPLIES[case["intent"]]
        assert no_model_work
    elif expect == "fallback":
        assert answer == NO_CONTEXT
        assert MARKER not in answer and AADHAAR not in answer
        kinds = public.events.kinds()
        assert case["event"] in kinds
        if "event_rule" in case:
            event = next(e for e in public.events.events if e["kind"] == case["event"])
            assert event["rules"] == [case["event_rule"]]
        if case["event"] == "canary_hit":
            assert public.events.events[-1]["severity"] == "critical"
    else:
        expected = case.get("intent", "about_owner")
        assert intent == expected
        if expected == "about_owner":
            assert answer == ANSWER and public.generations() == 1


REFUSALS = [p for p in CASES if p.values[0]["expect"] == "refused"]


@pytest.mark.parametrize("case", REFUSALS)
async def test_refusals_hold_without_the_classifier(case: dict[str, Any]) -> None:
    """With the classifier down, rules and keyword fallbacks still refuse (where marked)."""
    if case.get("fallback") is False or case.get("classifier") == "meta_probe":
        pytest.skip("this wording needs the classifier")
    public = make_public(label=AllModelsFailedError(Role.CLASSIFIER, Tier.PUBLIC, []))
    events = await public.ask(case["message"])
    assert events[0][1]["intent"] == case["intent"]
    assert public.searcher.calls == [] and public.generations() == 0
