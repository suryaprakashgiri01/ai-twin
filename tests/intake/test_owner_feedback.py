"""Fixes from owner testing: repos and source ids read by code, GitHub links synced as repos,
publishing a private document, and unlisted repos reported before the draft."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from app.agents.graphs.intake import rule_action
from app.agents.intake.drafts import github_repos, only_github_repo_urls
from tests.ingestion import persona
from tests.intake.harness import Twin, extract

MakeTwin = Callable[..., Twin]

ALLOWED = frozenset({"suryaprakashgiri01/observable-ai-service", "kavya-testpal/rate-limiter"})


@pytest.mark.parametrize(
    ("text", "repos"),
    [
        (
            "here is the details for repos: suryaprakashgiri01/observable-ai-service,"
            "suryaprakashgiri01/ai-twin/tree/develop",
            ["suryaprakashgiri01/observable-ai-service", "suryaprakashgiri01/ai-twin@develop"],
        ),
        (
            "Add https://github.com/suryaprakashgiri01/observable-ai-service.git please",
            ["suryaprakashgiri01/observable-ai-service"],
        ),
        ("my profile https://github.com/suryaprakashgiri01", []),
        (
            "sync suryaprakashgiri01/ai-twin from the develop branch",
            ["suryaprakashgiri01/ai-twin@develop"],
        ),
        ("sync kavya-testpal/notes@release/1.2", ["kavya-testpal/notes@release/1.2"]),
        ("met on 12/10 at 3/4 past", []),
        (
            "Kavya-Testpal/Rate-Limiter and kavya-testpal/rate-limiter",
            ["kavya-testpal/rate-limiter"],
        ),
    ],
)
def test_github_repos_are_read_from_free_text(text: str, repos: list[str]) -> None:
    assert github_repos(text) == repos


def test_rules_route_status_questions_and_github_links() -> None:
    sid = "6ad5afab0d04c371db474c76"
    assert rule_action(f"What is the status of source {sid}?", "ingest_github") == "source_status"
    assert rule_action("is my last upload done? status please", None) == "source_status"
    link = "Add this: https://github.com/suryaprakashgiri01/observable-ai-service"
    assert only_github_repo_urls(link)
    assert rule_action(link, "ingest_url") == "ingest_github"
    mixed = link + " and https://example.com/post"
    assert rule_action(mixed, "ingest_url") == "ingest_url"


async def test_github_link_becomes_a_repo_sync_draft(make_twin: MakeTwin) -> None:
    url = "https://github.com/suryaprakashgiri01/observable-ai-service"
    twin = make_twin([extract("ingest_url", urls=[url], category="project")])
    twin.ops.github_allowlist = lambda: ALLOWED  # type: ignore[method-assign]
    reply = await twin.say(f"Add this github project to the vault: {url}")
    assert reply.draft is not None and reply.draft["action"] == "ingest_github"
    assert reply.draft["payload"]["repos"] == ["suryaprakashgiri01/observable-ai-service"]


async def test_repo_list_with_extra_text_is_understood(make_twin: MakeTwin) -> None:
    twin = make_twin([extract("ingest_github")])
    twin.ops.github_allowlist = lambda: ALLOWED  # type: ignore[method-assign]
    reply = await twin.say(
        "here is the details for repos: suryaprakashgiri01/observable-ai-service,"
        "suryaprakashgiri01/ai-twin/tree/develop"
    )
    assert reply.draft is not None
    assert reply.draft["payload"]["repos"] == ["suryaprakashgiri01/observable-ai-service"]
    assert "suryaprakashgiri01/ai-twin is not in GITHUB_SYNC_REPOS" in reply.draft["summary"]


async def test_publishing_a_private_document_makes_it_public_first(make_twin: MakeTwin) -> None:
    twin = make_twin(
        [
            extract(
                "add_note", title="Rate limiter retro", body=persona.note(), category="project"
            ),
            extract("publish", document="it"),
        ]
    )
    twin.h.classifier.label = "private"
    cid = (await twin.say("Save a note about my rate limiter project")).conversation_id
    await twin.say("yes", cid)
    draft = await twin.say("publish it", cid)
    assert draft.draft is not None and draft.draft["action"] == "publish"
    assert "tier: private → public" in draft.draft["summary"]
    done = await twin.say("yes", cid)
    assert done.result is not None and "Made public, approved, published" in done.text
    doc = next(iter(twin.h.store.documents.values()))
    assert (doc.tier, doc.review_status, doc.published) == ("public", "approved", True)
    actions = [e["action"] for e in twin.audit.entries]
    assert actions[-3:] == ["document.update", "document.approve", "document.publish"]


async def test_confidential_document_is_never_published_from_intake(make_twin: MakeTwin) -> None:
    twin = make_twin(
        [
            extract("add_note", title="Offer", body="Offer details", category="offer"),
            extract("publish", document="it"),
        ]
    )
    cid = (await twin.say("save my offer details")).conversation_id
    await twin.say("yes", cid)
    reply = await twin.say("publish it", cid)
    assert reply.draft is None
    assert "is confidential; I won't publish that from here" in reply.text
    assert not any(d.published for d in twin.h.store.documents.values())
