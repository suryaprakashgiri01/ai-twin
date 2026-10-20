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


_RECORD_TIPS: dict[str, str] = {
    "application": ("Anything else about this application (referral, salary range, location)?"),
    "invite": "Anything else about this invite (platform, deadline, notes)?",
    "hr_call": "Anything else from this call (contact, next step)?",
    "assessment": "Anything else about this assessment (deadline, link, notes)?",
    "interview": "Anything else from this round?",
    "offer": "Anything else about this offer (comp, deadline, notes)?",
    "rejection": "Anything else to note about this rejection?",
    "withdrawal": "Anything else to note about this withdrawal?",
}


def suggest(outcome: Outcome) -> Suggestion | None:
    action = outcome.get("action")
    if action in ("add_note", "ingest_url", "ingest_file", "ingest_github", "source_status"):
        return _after_ingest(documents_of(outcome))
    if action in ("add_record", "update_record", "add_records"):
        return _after_record(outcome)
    return None


def _after_record(outcome: Outcome) -> Suggestion:
    if outcome.get("action") == "add_records":
        return Suggestion(
            "Anything else about these applications (referral, salary range, company type)?"
        )
    raw = outcome.get("record")
    record: dict[str, Any] = raw if isinstance(raw, dict) else {}
    kind = str(record.get("kind") or "")
    tip = _RECORD_TIPS.get(kind, "Anything else to add to this record?")
    return Suggestion(tip)


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
