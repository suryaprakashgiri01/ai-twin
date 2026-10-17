"""Ingestion fields: job state on sources, review fields on documents, the profile resume link.

Sources written before ingestion existed had status `new` and never ran a job; they become
`done` so restart recovery leaves them alone. New indexes are declared on the document classes.
"""

from __future__ import annotations

from typing import Any

from pymongo.asynchronous.database import AsyncDatabase

DESCRIPTION = "ingestion job state, review fields and profile resume link"


async def up(db: AsyncDatabase[dict[str, Any]]) -> None:
    sources = db["sources"]
    await sources.update_many({"status": {"$in": ["new", None]}}, {"$set": {"status": "done"}})
    await sources.update_many({"attempts": {"$exists": False}}, {"$set": {"attempts": 0}})
    await sources.update_many(
        {"updated_at": {"$exists": False}}, [{"$set": {"updated_at": "$created_at"}}]
    )
    documents = db["documents"]
    await documents.update_many(
        {"quarantine_rules": {"$exists": False}}, {"$set": {"quarantine_rules": []}}
    )
    await documents.update_many(
        {"redaction_report": {"$exists": False}}, {"$set": {"redaction_report": {}}}
    )
    await db["profile"].update_many(
        {"resume_document_id": {"$exists": False}}, {"$set": {"resume_document_id": None}}
    )
