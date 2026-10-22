"""AC6: Jev request/response mapping (recorded responses), errors, and the flagged switch."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from structlog.testing import capture_logs

from llm_orchestrator import AllModelsFailedError, NoEligibleModelError, Tier
from llm_orchestrator.classify import (
    JEV_URL,
    Classification,
    FlaggedClassifier,
    JevClassifier,
    JevError,
    LayaClassifier,
    LayaError,
    LLMClassifier,
)
from tests.llm_orchestrator.fakes import SECRET_PROMPT, Factory, inventory, orchestrator, spec

LABELS = {"about_owner": "Questions about the owner", "off_topic": "Anything else"}
QUESTION = "Which intent does the visitor have?"

# Shape recorded from the documented systemone response.
RECORDED_OK: dict[str, Any] = {
    "model": "jev-1.13.0",
    "answers": {
        "q": {
            "type": "choice",
            "choice": "about_owner",
            "probabilities": {"about_owner": 0.82, "off_topic": 0.18},
            "confidence": 0.82,
        }
    },
    "usage": {"input_tokens": 41, "output_tokens": 1},
}


class Recorder:
    def __init__(self, responses: list[httpx.Response]) -> None:
        self.responses = responses
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.responses[min(len(self.requests) - 1, len(self.responses) - 1)]


def _jev(recorder: Recorder, sleeps: list[float] | None = None, **kwargs: Any) -> JevClassifier:
    async def fake_sleep(seconds: float) -> None:
        (sleeps if sleeps is not None else []).append(seconds)

    client = httpx.AsyncClient(transport=httpx.MockTransport(recorder))
    return JevClassifier(SecretStr("tsk-test-not-real"), client=client, sleep=fake_sleep, **kwargs)


async def test_jev_request_matches_documented_body_and_maps_answer() -> None:
    recorder = Recorder([httpx.Response(200, json=RECORDED_OK)])
    result = await _jev(recorder).classify(
        "Where did you study?", question=QUESTION, labels=LABELS, tier=Tier.PUBLIC
    )
    request = recorder.requests[0]
    assert (request.method, str(request.url)) == ("POST", JEV_URL)
    assert request.headers["authorization"] == "Bearer tsk-test-not-real"
    assert json.loads(request.content) == {
        "state": "Where did you study?",
        "model": "jev-1.13.0",
        "questions": {
            "q": {"type": "choice", "instructions": QUESTION, "criteria": LABELS},
        },
    }
    assert result == Classification(
        label="about_owner",
        probabilities={"about_owner": 0.82, "off_topic": 0.18},
        confidence=0.82,
        model="jev:jev-1.13.0",
    )


async def test_jev_retries_429_and_529_honouring_retry_after() -> None:
    sleeps: list[float] = []
    recorder = Recorder(
        [
            httpx.Response(429, headers={"retry-after": "2"}),
            httpx.Response(529),
            httpx.Response(200, json=RECORDED_OK),
        ]
    )
    result = await _jev(recorder, sleeps, max_attempts=3).classify(
        "x", question=QUESTION, labels=LABELS, tier=Tier.PUBLIC
    )
    assert result.label == "about_owner"
    assert len(recorder.requests) == 3
    assert sleeps[0] == 2.0


@pytest.mark.parametrize("status", [400, 401, 413, 422])
async def test_jev_permanent_errors_do_not_retry(status: int) -> None:
    recorder = Recorder([httpx.Response(status, json={"error": "nope"})])
    with pytest.raises(JevError) as info:
        await _jev(recorder).classify("x", question=QUESTION, labels=LABELS, tier=Tier.PUBLIC)
    assert (info.value.transient, info.value.status_code) == (False, status)
    assert len(recorder.requests) == 1


async def test_jev_transient_exhaustion_raises_transient() -> None:
    recorder = Recorder([httpx.Response(503)])
    with pytest.raises(JevError) as info:
        await _jev(recorder, max_attempts=2).classify(
            "x", question=QUESTION, labels=LABELS, tier=Tier.PUBLIC
        )
    assert info.value.transient is True
    assert len(recorder.requests) == 2


async def test_jev_transport_error_is_transient() -> None:
    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    jev = JevClassifier(
        SecretStr("k"),
        client=httpx.AsyncClient(transport=httpx.MockTransport(broken)),
        max_attempts=1,
    )
    with pytest.raises(JevError) as info:
        await jev.classify("x", question=QUESTION, labels=LABELS, tier=Tier.PUBLIC)
    assert info.value.transient is True


@pytest.mark.parametrize(
    "body",
    [
        {"answers": {}},
        {"answers": {"q": {"choice": "elsewhere", "confidence": 0.5}}},
        {"answers": {"q": {"choice": "about_owner"}}},
        [1, 2],
    ],
)
async def test_jev_malformed_answers_are_permanent(body: Any) -> None:
    recorder = Recorder([httpx.Response(200, json=body)])
    with pytest.raises(JevError) as info:
        await _jev(recorder).classify("x", question=QUESTION, labels=LABELS, tier=Tier.PUBLIC)
    assert info.value.transient is False


async def test_jev_invalid_json_is_permanent() -> None:
    recorder = Recorder([httpx.Response(200, content=b"<html>")])
    with pytest.raises(JevError, match="invalid JSON"):
        await _jev(recorder).classify("x", question=QUESTION, labels=LABELS, tier=Tier.PUBLIC)


async def test_jev_missing_probabilities_become_one_hot() -> None:
    body = {"model": "jev-1.13.0", "answers": {"q": {"choice": "off_topic", "confidence": 0.6}}}
    result = await _jev(Recorder([httpx.Response(200, json=body)])).classify(
        "x", question=QUESTION, labels=LABELS, tier=Tier.PUBLIC
    )
    assert result.probabilities == {"about_owner": 0.0, "off_topic": 1.0}


async def test_jev_refuses_above_public_without_a_request() -> None:
    recorder = Recorder([httpx.Response(200, json=RECORDED_OK)])
    with pytest.raises(NoEligibleModelError):
        await _jev(recorder).classify("x", question=QUESTION, labels=LABELS, tier=Tier.PRIVATE)
    assert recorder.requests == []


async def test_jev_logs_carry_no_text() -> None:
    recorder = Recorder([httpx.Response(429), httpx.Response(200, json=RECORDED_OK)])
    with capture_logs() as events:
        await _jev(recorder).classify(
            SECRET_PROMPT, question=QUESTION, labels=LABELS, tier=Tier.PUBLIC
        )
    assert [e["outcome"] for e in events if e["event"] == "llm attempt"] == [
        "transient_error",
        "ok",
    ]
    assert all("CANARY" not in repr(e) and "tsk-" not in repr(e) for e in events)


# --- LLM classifier ---------------------------------------------------------------------------


def _llm(replies: list[Any]) -> LLMClassifier:
    factory = Factory({"m": replies})
    return LLMClassifier(orchestrator(inventory(spec("m", max_tier="confidential")), factory))


async def test_llm_classifier_maps_structured_answer() -> None:
    reply = json.dumps(
        {
            "label": "off_topic",
            "confidence": 0.7,
            "probabilities": {"off_topic": 7, "about_owner": 3},
        }
    )
    result = await _llm([reply]).classify("x", question=QUESTION, labels=LABELS, tier=Tier.PRIVATE)
    assert result.label == "off_topic"
    assert result.probabilities == pytest.approx({"off_topic": 0.7, "about_owner": 0.3})
    assert (result.confidence, result.model) == (0.7, "m")


async def test_llm_classifier_repairs_unknown_label_and_one_hots() -> None:
    replies = [
        '{"label": "weather", "confidence": 0.9}',
        '{"label": "about_owner", "confidence": 1}',
    ]
    result = await _llm(replies).classify("x", question=QUESTION, labels=LABELS, tier=Tier.PUBLIC)
    assert result.label == "about_owner"
    assert result.probabilities == {"about_owner": 1.0, "off_topic": 0.0}


async def test_llm_classifier_gives_up_after_repair() -> None:
    with pytest.raises(AllModelsFailedError):
        await _llm(['{"label": "weather", "confidence": 0.9}']).classify(
            "x", question=QUESTION, labels=LABELS, tier=Tier.PUBLIC
        )


async def test_classifiers_reject_empty_labels() -> None:
    with pytest.raises(ValueError):
        await _llm(["{}"]).classify("x", question=QUESTION, labels={}, tier=Tier.PUBLIC)
    with pytest.raises(ValueError):
        await _jev(Recorder([httpx.Response(200)])).classify(
            "x", question=QUESTION, labels={}, tier=Tier.PUBLIC
        )


# --- flagged switch ---------------------------------------------------------------------------


class StubClassifier:
    def __init__(self, name: str, *, fail: bool = False) -> None:
        self.name = name
        self.fail = fail
        self.calls = 0

    async def classify(
        self, text: str, *, question: str, labels: Mapping[str, str], tier: Tier
    ) -> Classification:
        self.calls += 1
        if self.fail:
            raise JevError("jev down", transient=True, status_code=529)
        label = next(iter(labels))
        return Classification(
            label=label, probabilities={label: 1.0}, confidence=1, model=self.name
        )


@pytest.mark.parametrize(
    "enabled, tier, expected",
    [
        (True, Tier.PUBLIC, "jev"),
        (False, Tier.PUBLIC, "llm"),
        (True, Tier.PRIVATE, "llm"),
        (True, Tier.CONFIDENTIAL, "llm"),
    ],
)
async def test_flagged_uses_jev_only_when_on_and_public(
    enabled: bool, tier: Tier, expected: str
) -> None:
    llm, jev = StubClassifier("llm"), StubClassifier("jev")
    flagged = FlaggedClassifier(llm, jev, enabled=enabled)
    result = await flagged.classify("x", question=QUESTION, labels=LABELS, tier=tier)
    assert result.model == expected
    assert jev.calls == (1 if expected == "jev" else 0)


async def test_flagged_falls_back_to_llm_on_jev_error() -> None:
    llm, jev = StubClassifier("llm"), StubClassifier("jev", fail=True)
    with capture_logs() as events:
        result = await FlaggedClassifier(llm, jev, enabled=True).classify(
            "x", question=QUESTION, labels=LABELS, tier=Tier.PUBLIC
        )
    assert result.model == "llm"
    assert (jev.calls, llm.calls) == (1, 1)
    assert any(e["event"] == "jev failed, using llm classifier" for e in events)


async def test_flagged_reads_flag_each_call_and_handles_no_jev() -> None:
    flag = {"on": False}
    llm, jev = StubClassifier("llm"), StubClassifier("jev")
    flagged = FlaggedClassifier(llm, jev, enabled=lambda: flag["on"])

    async def model() -> str:
        result = await flagged.classify("x", question=QUESTION, labels=LABELS, tier=Tier.PUBLIC)
        return result.model

    assert await model() == "llm"
    flag["on"] = True
    assert await model() == "jev"
    no_jev = FlaggedClassifier(llm, None, enabled=True)
    assert not no_jev.uses_jev(Tier.PUBLIC)


class FakeLayaRouter:
    """Laya's `Router.predict(state, questions)` as its docs describe the answer."""

    def __init__(self, choice: str = "off_topic") -> None:
        self.choice = choice
        self.seen: list[tuple[str, dict[str, Any]]] = []

    def predict(self, state: str, questions: dict[str, Any]) -> dict[str, Any]:
        self.seen.append((state, questions))
        answer = {"choice": self.choice, "answer_confidence": 0.8}
        return {"answers": {"q": answer}, "routing": {"model": "english"}}


async def test_laya_maps_the_choice_and_refuses_restricted() -> None:
    router = FakeLayaRouter()
    laya = LayaClassifier(router=router)
    result = await laya.classify("x", question=QUESTION, labels=LABELS, tier=Tier.CONFIDENTIAL)
    assert (result.label, result.model, result.confidence) == ("off_topic", "laya:english", 0.8)
    assert router.seen[0][1]["q"] == {
        "type": "choice",
        "instructions": QUESTION,
        "criteria": LABELS,
    }
    with pytest.raises(NoEligibleModelError):
        await laya.classify("x", question=QUESTION, labels=LABELS, tier=Tier.RESTRICTED)
    with pytest.raises(LayaError, match="unknown label"):
        await LayaClassifier(router=FakeLayaRouter("nope")).classify(
            "x", question=QUESTION, labels=LABELS, tier=Tier.PUBLIC
        )


async def test_flagged_tries_laya_first_then_falls_through() -> None:
    llm, jev, laya = StubClassifier("llm"), StubClassifier("jev"), StubClassifier("laya")
    flagged = FlaggedClassifier(llm, jev, enabled=True, laya=laya, laya_enabled=True)
    assert (
        await flagged.classify("x", question=QUESTION, labels=LABELS, tier=Tier.PUBLIC)
    ).model == "laya"
    broken = StubClassifier("laya", fail=True)
    flagged = FlaggedClassifier(llm, jev, enabled=True, laya=broken, laya_enabled=True)
    assert (
        await flagged.classify("x", question=QUESTION, labels=LABELS, tier=Tier.PUBLIC)
    ).model == "jev"
    # Confidential text: Laya (local) may read it; Jev may not, so the LLM classifier answers.
    off = FlaggedClassifier(llm, jev, enabled=True, laya=laya, laya_enabled=False)
    result = await off.classify("x", question=QUESTION, labels=LABELS, tier=Tier.CONFIDENTIAL)
    assert result.model == "llm"


async def test_laya_without_the_package_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib

    real = importlib.import_module

    def missing(name: str, *args: Any) -> Any:
        if name == "laya":
            raise ImportError("no laya")
        return real(name, *args)

    monkeypatch.setattr(importlib, "import_module", missing)
    with pytest.raises(LayaError, match="not installed"):
        await LayaClassifier().classify("x", question=QUESTION, labels=LABELS, tier=Tier.PUBLIC)
