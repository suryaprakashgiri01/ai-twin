"""The guaranteed public answer: the owner's public projects when the knowledge base has
nothing (the visitor gets an answer, and the gap is logged for the owner to close)."""

from __future__ import annotations

from pathlib import Path

import pytest
from structlog.testing import capture_logs

from app.agents.prompts.public import NO_CONTEXT
from app.core.config import DEFAULT_PUBLIC_PROJECTS, Settings
from app.services.public_fallback import PublicFallback, PublicProjectIndex, load_index
from tests.public.fakes import ANSWER, answer_of, make_public

Q = "What are your recent personal projects?"


@pytest.fixture
def fallback() -> PublicFallback:
    return PublicFallback(
        PublicProjectIndex(
            portfolio_url="https://surya-prakash-giri.vercel.app/",
            github_url="https://github.com/suryaprakashgiri01",
            projects=[
                {
                    "name": "ai-twin",
                    "repo": "suryaprakashgiri01/ai-twin",
                    "summary": "my portfolio knowledge base and chat service.",
                },
                {
                    "name": "AgentForge",
                    "repo": "suryaprakashgiri01/AgentForge",
                    "summary": "a framework for multi-agent systems.",
                },
            ],
        )
    )


def test_the_shipped_index_lists_the_public_projects() -> None:
    index = load_index(DEFAULT_PUBLIC_PROJECTS)
    assert index.projects
    assert {p.name for p in index.projects} >= {"ai-twin", "AgentForge"}
    assert index.github_url.startswith("https://github.com/")
    assert index.portfolio_url.startswith("https://")


def test_a_missing_or_broken_index_means_no_fallback(tmp_path: Path) -> None:
    assert load_index(tmp_path / "nope.yaml").projects == []
    broken = tmp_path / "broken.yaml"
    broken.write_text("projects: [oops\n", encoding="utf-8")
    assert load_index(broken).projects == []


def test_the_fallback_is_off_by_a_setting() -> None:
    assert Settings().public_fallback_enabled is True
    assert Settings(public_fallback_enabled=False).public_fallback_enabled is False


async def test_an_empty_knowledge_base_falls_back_to_the_public_projects(
    fallback: PublicFallback,
) -> None:
    public = make_public(hits=[], fallback=fallback)
    events = await public.ask(Q)
    answer = answer_of(events)
    assert "ai-twin" in answer and "AgentForge" in answer
    assert "github.com/suryaprakashgiri01" in answer
    assert NO_CONTEXT not in answer


async def test_a_model_that_cannot_answer_falls_back(fallback: PublicFallback) -> None:
    """The context had passages, but the model found nothing usable in them."""
    public = make_public(generator=[NO_CONTEXT], fallback=fallback)
    events = await public.ask(Q)
    answer = answer_of(events)
    assert "ai-twin" in answer
    assert NO_CONTEXT not in answer


async def test_the_fallback_is_logged_as_a_knowledge_base_gap(
    fallback: PublicFallback,
) -> None:
    public = make_public(hits=[], fallback=fallback)
    with capture_logs() as logs:
        await public.ask(Q)
    gaps = [log for log in logs if log.get("event") == "kb fallback"]
    assert len(gaps) == 1
    gap = gaps[0]
    assert gap["reason"] == "empty_context"
    assert gap["intent"] == "about_owner"
    assert gap["source"] == "public_projects"
    # The fingerprint identifies the question; the question itself is never logged.
    assert gap["fingerprint"] and Q not in str(gap)


async def test_without_a_fallback_the_old_reply_stays() -> None:
    public = make_public(hits=[])
    events = await public.ask(Q)
    assert answer_of(events) == NO_CONTEXT


async def test_a_real_answer_is_never_replaced_by_the_fallback(fallback: PublicFallback) -> None:
    public = make_public(generator=[ANSWER], fallback=fallback)
    events = await public.ask(Q)
    assert answer_of(events) == ANSWER
