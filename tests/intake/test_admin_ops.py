"""owner-intake T1: AdminOps audits with the caller's actor and keeps route errors."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from app.exceptions import AppError, ErrorCode
from app.services.admin_ops import TextIn, intake_actor, key_actor
from app.services.profile import RecordIn
from tests.intake.harness import Twin

RECORD = {
    "kind": "interview",
    "company": "Acme",
    "role": "SRE",
    "stage": "screen",
    "date": "2026-10-01T00:00:00",
}


async def test_actor_is_whatever_the_caller_names(make_twin: Callable[..., Twin]) -> None:
    twin = make_twin(["{}"])
    record = await twin.ops.create_record(RecordIn.model_validate(RECORD), key_actor("cli"))
    await twin.ops.delete_record(record.id, intake_actor("cli"))
    assert [(e["action"], e["actor"]) for e in twin.audit.entries] == [
        ("record.create", "key:cli"),
        ("record.delete", "agent:intake via key:cli"),
    ]


async def test_errors_match_the_routes(make_twin: Callable[..., Twin]) -> None:
    twin = make_twin(["{}"])
    with pytest.raises(AppError) as missing:
        await twin.ops.delete_record("0" * 24, key_actor("cli"))
    assert missing.value.code == ErrorCode.NOT_FOUND
    with pytest.raises(AppError) as bad_id:
        await twin.ops.get_document("not-an-id")
    assert bad_id.value.code == ErrorCode.NOT_FOUND
    body = TextIn(title="t", body="b", category="blog")
    submitted = await twin.ops.ingest_text(body, intake_actor("cli"))
    again = await twin.ops.ingest_text(body, intake_actor("cli"))
    assert again.source_id == submitted.source_id
