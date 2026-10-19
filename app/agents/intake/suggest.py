"""One deterministic next step after a write. A suggestion with an action becomes a new pending
draft: nothing happens until the owner says yes to it."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.agents.intake.actions import Outcome, documents_of


@dataclass(frozen=True)
class Suggestion:
    text: str
    action: str | None = None
    payload: dict[str, Any] | None = None


def suggest(outcome: Outcome) -> Suggestion | None:
    action = outcome.get("action")
    if action in ("add_note", "ingest_url", "ingest_file", "ingest_github", "source_status"):
        return _after_ingest(documents_of(outcome))
    if action in ("add_record", "update_record"):
        return Suggestion("Anything else from this round?")
    return None


def _after_ingest(docs: list[dict[str, Any]]) -> Suggestion | None:
    restricted = [d for d in docs if d["tier"] == "restricted"]
    publishable = [
        d
        for d in docs
        if d["tier"] == "public" and d["review_status"] == "pending" and not d["published"]
    ]
    if publishable:
        doc = publishable[0]
        return Suggestion(
            f"“{doc['title']}” looks public. Approve and publish it to the portfolio?",
            "approve_publish",
            {"document": doc["id"], "document_id": doc["id"], "document_title": doc["title"]},
        )
    if restricted:
        titles = ", ".join(f"“{d['title']}”" for d in restricted)
        return Suggestion(f"{titles} is restricted: kept as a file only, no text or vectors.")
    return None
