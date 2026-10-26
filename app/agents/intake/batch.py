"""Split one owner message into several job-search records, and the batch API body.

Used when the owner pastes a list ("today I applied to the following…") or a JSON array.
Shared cues from the intro (date, kind, work mode, company type) apply to every line unless
that line overrides them.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import Field, field_validator

from app.agents.intake.drafts import day_value, find_day
from app.agents.routing import canonical_kind, record_kind_in
from app.api.envelope import ApiModel
from app.db.documents import WORK_MODES, CompanyType
from app.services.profile import MAX_EXTRAS, RecordIn, _clip_extras

MAX_BATCH = 50
# Required for every item in a batch before confirmation.
REQUIRED = ("kind", "company", "role", "stage", "date")

_BATCH_CUE = re.compile(
    r"\b(?:following|these|several|multiple|a few|a couple|the below)\b|"
    r"\b(?:companies|roles|applications|interviews|invites)\b|"
    r"\bdetails?\s+are\s+following\b|"
    r"\bi\s+applied\s+(?:in|to|for)\s+\d+",
    re.I,
)
_LINE_APPLIED = re.compile(
    r"(?i:applied\s+to\s+(?:an?\s+|the\s+)?)"
    r"(?P<role>.+?)\s+(?i:role\s+)?(?i:at)\s+"
    r"(?P<company>[A-Z][\w&.'-]*(?:\s+(?:[A-Z][\w&.'-]*|&))*)"
    r"(?P<rest>.*)$"
)
_LINE_AT = re.compile(
    r"^(?P<role>[A-Z][\w .+/&#\u2013\u2014-]{1,80}?)\s+at\s+"
    r"(?P<company>[A-Z][\w&.'-]*(?:\s+(?:[A-Z][\w&.'-]*|&))*)"
    r"(?P<rest>.*)$"
)
_FOR_LOCATION = re.compile(
    r"\bfor\s+(?:the\s+)?(?:location\s+)?(?P<loc>.+?)"
    r"(?=\s+location\b|$|\n|,?\s+and\b)",
    re.I,
)
_WORK_MODE = re.compile(r"\b(onsite|on-site|remote|hybrid|online)\b", re.I)
_COMPANY_TYPE = re.compile(
    r"\b(product|service|research|startup)(?:\s*[-\s]?\s*based)?\b",
    re.I,
)
_BULLET = re.compile(r"^\s*(?:[-*]|\d+[.)])\s+")
_DEFAULT_STAGE = {"application": "applied", "invite": "invited to apply", "hr_call": "HR call"}


class RecordsBatchIn(ApiModel):
    """Several records in one request or one intake confirmation."""

    records: list[RecordIn] = Field(min_length=1, max_length=MAX_BATCH)

    @field_validator("records", mode="before")
    @classmethod
    def _list(cls, value: Any) -> Any:
        return value if isinstance(value, list) else []


def looks_like_batch(message: str) -> bool:
    if _json_array(message) is not None:
        return True
    lines = _candidate_lines(message)
    if len(lines) >= 2:
        return True
    return bool(_BATCH_CUE.search(message) and len(lines) >= 1)


def parse_record_batch(message: str, tz: ZoneInfo, now: datetime) -> list[dict[str, Any]]:
    """Zero or more record payloads from the owner's message (not yet fully normalised)."""
    if (array := _json_array(message)) is not None:
        return [_as_item(row) for row in array[:MAX_BATCH] if isinstance(row, dict)]

    lines = _candidate_lines(message)
    shared = _shared_defaults(message, lines, tz, now)
    items: list[dict[str, Any]] = []
    for line in lines:
        item = _parse_line(line, tz, now)
        if item is None:
            continue
        merged = {**shared, **{k: v for k, v in item.items() if v}}
        if not merged.get("kind"):
            merged["kind"] = "application"
        kind = canonical_kind(str(merged.get("kind") or ""))
        if kind in _DEFAULT_STAGE and not merged.get("stage"):
            merged["stage"] = _DEFAULT_STAGE[kind]
        if not merged.get("date") and (day := find_day(message, tz, now)) is not None:
            merged["date"] = day_value(day)
        items.append(merged)
        if len(items) >= MAX_BATCH:
            break
    return items


def batch_summary(records: list[dict[str, Any]]) -> str:
    lines = [f"Add {len(records)} records"]
    for index, row in enumerate(records, start=1):
        bits = [
            str(row.get("kind") or "?"),
            str(row.get("company") or "?"),
            str(row.get("role") or "?"),
        ]
        if row.get("location"):
            bits.append(f"at {row['location']}")
        if row.get("company_type"):
            bits.append(f"{row['company_type']}-based")
        if row.get("work_mode"):
            bits.append(str(row["work_mode"]))
        if row.get("stage"):
            bits.append(str(row["stage"]))
        if row.get("date"):
            bits.append(str(row["date"])[:10])
        lines.append(f"  {index}. " + " · ".join(bits))
        if row.get("notes"):
            lines.append(f"     notes: {row['notes']}")
        extras = row.get("extras") or {}
        if extras:
            shown = ", ".join(f"{k}={v}" for k, v in list(extras.items())[:MAX_EXTRAS])
            lines.append(f"     extras: {shown}")
    return "\n".join(lines)


def missing_in_batch(records: list[dict[str, Any]]) -> list[str]:
    out: list[str] = []
    for index, row in enumerate(records, start=1):
        need = [name for name in REQUIRED if row.get(name) in (None, "", [])]
        if need:
            label = row.get("company") or row.get("role") or f"item {index}"
            out.append(f"{label}: {', '.join(need)}")
    return out


def parse_company_type(text: str) -> CompanyType | None:
    if not (m := _COMPANY_TYPE.search(text)):
        return None
    word = m.group(1).casefold()
    if word in {"product", "service", "research", "startup"}:
        return word  # type: ignore[return-value]
    return None


def parse_work_mode(text: str) -> str | None:
    if not (m := _WORK_MODE.search(text)):
        return None
    word = m.group(1).casefold().replace("-", "")
    if word == "online":
        return "remote"
    if word == "onsite":
        return "onsite"
    return word if word in WORK_MODES else None


def _json_array(message: str) -> list[Any] | None:
    text = message.strip()
    if not (text.startswith("[") and text.endswith("]")):
        match = re.search(r"\[[\s\S]*\]", message)
        text = match.group(0) if match else ""
    if not text:
        return None
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, list) and len(value) >= 2 else None


def _as_item(row: dict[str, Any]) -> dict[str, Any]:
    from app.agents.intake.labels import camel_to_snake_payload

    out = camel_to_snake_payload(row)
    if "extras" in out:
        out["extras"] = _clip_extras(out.get("extras"))
    return out


def _candidate_lines(message: str) -> list[str]:
    lines: list[str] = []
    for raw in message.splitlines():
        line = _BULLET.sub("", raw).strip().rstrip("-\u2013\u2014:")
        if not line or len(line) < 8:
            continue
        if (
            _BATCH_CUE.search(line)
            and " at " not in line.casefold()
            and "role" not in line.casefold()
        ):
            continue
        if " at " in line.casefold() or _LINE_APPLIED.search(line):
            lines.append(line)
    if len(lines) < 2:
        for part in re.split(r"\s*;\s*|\band\s+applied\s+", message, flags=re.I):
            part = part.strip()
            if (
                part
                and (" at " in part.casefold() or _LINE_APPLIED.search(part))
                and part not in lines
            ):
                lines.append(part)
    return lines[:MAX_BATCH]


def _shared_defaults(message: str, lines: list[str], tz: ZoneInfo, now: datetime) -> dict[str, Any]:
    """Cues from the intro only (before the first record line), so a type on line 2 stays there."""
    at = message.find(lines[0]) if lines else -1
    intro = message[:at] if at >= 0 else message[:200]
    shared: dict[str, Any] = {}
    if found_kind := record_kind_in(intro) or record_kind_in(message):
        shared["kind"] = found_kind
    if mode := parse_work_mode(intro):
        shared["work_mode"] = mode
    if ctype := parse_company_type(intro):
        shared["company_type"] = ctype
    if (day := find_day(intro, tz, now) or find_day(message, tz, now)) is not None:
        shared["date"] = day_value(day)
    resolved = canonical_kind(str(shared.get("kind") or "application"))
    if resolved in _DEFAULT_STAGE:
        shared.setdefault("kind", resolved)
        shared.setdefault("stage", _DEFAULT_STAGE[resolved])
    return shared


def _parse_line(line: str, tz: ZoneInfo, now: datetime) -> dict[str, Any] | None:
    match = _LINE_APPLIED.search(line) or _LINE_AT.match(line)
    if match is None:
        return None
    role = re.sub(r"\s+", " ", match.group("role")).strip(" -\u2013\u2014")
    role = re.sub(r"\s+role$", "", role, flags=re.I).strip()
    company = match.group("company").strip()
    rest = match.group("rest") or ""
    item: dict[str, Any] = {"role": role, "company": company}
    if loc := _line_location(rest):
        item["location"] = loc
    if mode := parse_work_mode(line):
        item["work_mode"] = mode
    if ctype := parse_company_type(line):
        item["company_type"] = ctype
    if (day := find_day(line, tz, now)) is not None:
        item["date"] = day_value(day)
    if notes := _line_notes(rest, item.get("location")):
        item["notes"] = notes
    return item


def _line_location(rest: str) -> str | None:
    if m := _FOR_LOCATION.search(rest):
        place = re.sub(r"\s+", " ", m.group("loc")).strip(" .,")
        # Drop a trailing company-type phrase from the location capture.
        place = _COMPANY_TYPE.sub("", place).strip(" .,")
        return place or None
    if m := re.search(r"\blocation\s*:?\s*(.+)$", rest, re.I):
        return re.sub(r"\s+", " ", m.group(1)).strip(" .,")
    return None


def _line_notes(rest: str, location: str | None) -> str | None:
    text = rest
    text = _FOR_LOCATION.sub(" ", text)
    text = re.sub(r"\blocation\b", " ", text, flags=re.I)
    text = _WORK_MODE.sub(" ", text)
    # Keep "product based" wording out of notes once company_type is set from it.
    text = _COMPANY_TYPE.sub(" ", text)
    text = re.sub(r"\b(?:for the|for)\b", " ", text, flags=re.I)
    if location:
        text = re.sub(re.escape(location), " ", text, flags=re.I)
    text = re.sub(r"\s+", " ", text).strip(" .,;:-")
    # Drop a leftover "and they prefer company" fragment with no substance.
    text = re.sub(r"^(?:and\s+)?they\s+prefer\s+company,?\s*", "", text, flags=re.I)
    text = re.sub(r"^and\s+", "", text, flags=re.I).strip(" .,;:-")
    if len(text) < 8:
        return None
    return text
