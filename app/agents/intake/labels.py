"""Owner-labelled field cues and pasted JSON, applied before grounding.

Labelled values always win over the model. Unknown labelled keys become `extras` entries so the
owner's attributes are kept instead of dropped by the field allowlist.
"""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, ValidationError

from app.services.profile import MAX_EXTRA_LEN, MAX_EXTRAS

# Field names the owner may write as a label (with spaces or camelCase).
_FIELD_ALIASES: dict[str, str] = {
    "kind": "kind",
    "company": "company",
    "role": "role",
    "stage": "stage",
    "date": "date",
    "notes": "notes",
    "note": "notes",
    "platform": "platform",
    "contact": "contact",
    "location": "location",
    "work mode": "work_mode",
    "work_mode": "work_mode",
    "workmode": "work_mode",
    "extras": "extras",
    "record": "record",
    "document": "document",
    "title": "title",
    "body": "body",
    "category": "category",
    "tags": "tags",
    "filename": "filename",
    "urls": "urls",
    "repos": "repos",
    "tier": "tier",
    "summary": "summary",
    "question": "question",
    "text": "text",
    "memory": "memory",
    "action": "action",
}

# A job / req id labelled by the owner becomes a notes line, never a contact.
_NOTE_ALIASES = frozenset({"job id", "job_id", "jobid", "req id", "req_id", "reference", "ref"})

# Multi-word labels we recognise; everything else is a single token (so "Initech role: DE"
# does not swallow the company value into an extras key named "Initech role").
_MULTI_LABELS = ("work mode", "job id", "req id", "salary range")
_MULTI = "|".join(
    re.escape(name).replace(r"\ ", r"[ \t]+")
    for name in sorted(_MULTI_LABELS, key=len, reverse=True)
)
# "Role: X", "work mode: remote". May follow a line break, punctuation or a space.
_LABEL = re.compile(
    rf"(?:^|(?<=[\n,;.!? \t]))(?P<label>{_MULTI}|[A-Za-z][A-Za-z0-9_/]*)[ \t]*:[ \t]*",
    re.I | re.M,
)
# "in notes please mention …" / "in notes: …" without a colon label.
_IN_NOTES = re.compile(
    r"\bin[ \t]+notes(?:[ \t]+please)?(?:[ \t]+mention)?[ \t]*[:\s][ \t]*"
    r"(?P<notes>.+?)(?=$|\n|(?=\b[A-Za-z][\w ]{0,20}:))",
    re.I | re.S,
)
_CAMEL = re.compile(r"([a-z0-9])([A-Z])")
_JSON_OBJECT = re.compile(r"\{[\s\S]*\}")


def snake_key(name: str) -> str:
    """workMode / Work Mode → work_mode."""
    text = name.strip().replace("-", "_").replace(" ", "_")
    return _CAMEL.sub(r"\1_\2", text).casefold()


def resolve_field(label: str, known: frozenset[str]) -> str | None:
    """Map an owner label to a known field name, or None when it should become an extras key."""
    folded = " ".join(label.casefold().split())
    if folded in _NOTE_ALIASES:
        return "notes"
    alias = _FIELD_ALIASES.get(folded) or _FIELD_ALIASES.get(snake_key(label))
    if alias is not None and (not known or alias in known or alias == "action"):
        return alias
    if snake_key(label) in known:
        return snake_key(label)
    return None


def _value_at(text: str, start: int, next_label: int | None) -> str:
    end = next_label if next_label is not None else len(text)
    raw = text[start:end].strip().rstrip(",;")
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        return raw[1:-1].strip()
    # Stop at a newline so "Role: X\nlocation: Y" stays clean even without another label match.
    if "\n" in raw:
        raw = raw.split("\n", 1)[0].strip().rstrip(",;")
    return raw


def parse_labels(message: str, known_fields: frozenset[str] | set[str]) -> dict[str, Any]:
    """Pull `key: value` cues and an `in notes …` phrase from the owner's message."""
    known = frozenset(known_fields)
    out: dict[str, Any] = {}
    extras: dict[str, str] = {}
    matches = list(_LABEL.finditer(message))
    for index, match in enumerate(matches):
        label = match.group("label")
        nxt = matches[index + 1].start() if index + 1 < len(matches) else None
        value = _value_at(message, match.end(), nxt)
        if not value:
            continue
        field = resolve_field(label, known)
        if field == "notes" and " ".join(label.casefold().split()) in _NOTE_ALIASES:
            note = value if value.casefold().startswith("job id") else f"Job ID: {value}"
            out["notes"] = _join_notes(out.get("notes"), note)
        elif field is None:
            key = label.strip()[:MAX_EXTRA_LEN]
            if key and len(extras) < MAX_EXTRAS:
                extras[key] = value[:MAX_EXTRA_LEN]
        elif field == "extras" and value.startswith("{"):
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                extras[label.strip()[:MAX_EXTRA_LEN]] = value[:MAX_EXTRA_LEN]
            else:
                if isinstance(parsed, dict):
                    for key, raw in parsed.items():
                        if len(extras) >= MAX_EXTRAS:
                            break
                        extras[str(key)[:MAX_EXTRA_LEN]] = str(raw)[:MAX_EXTRA_LEN]
        else:
            out[field] = value
    if notes_match := _IN_NOTES.search(message):
        out["notes"] = _join_notes(out.get("notes"), notes_match.group("notes").strip())
    if extras:
        out["extras"] = extras
    return out


def _join_notes(existing: Any, addition: str) -> str:
    prior = str(existing).strip() if existing else ""
    if not addition:
        return prior
    if not prior:
        return addition
    if addition.casefold() in prior.casefold():
        return prior
    return f"{prior}; {addition}"


def find_json_object(message: str) -> dict[str, Any] | None:
    """The first top-level JSON object in the message, or None when it is not valid JSON."""
    text = message.strip()
    candidates: list[str] = []
    if text.startswith("{") and text.endswith("}"):
        candidates.append(text)
    match = _JSON_OBJECT.search(message)
    if match:
        candidates.append(match.group(0))
    for raw in candidates:
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None


def camel_to_snake_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Accept camelCase keys from a paste the same way the API will after the contract change."""
    out: dict[str, Any] = {}
    for key, value in payload.items():
        name = snake_key(str(key)) if isinstance(key, str) else str(key)
        if isinstance(value, dict) and name == "extras":
            out[name] = {str(k): str(v) for k, v in value.items()}
        else:
            out[name] = value
    return out


def validate_paste(
    payload: dict[str, Any], model: type[BaseModel] | None, *, partial: bool
) -> tuple[dict[str, Any], dict[str, str]]:
    """Validate a pasted object against the action model. Returns (kept, field_errors).

    When `partial` is True (an in-progress draft), only the keys present are checked; missing
    required fields are left for the normal missing-field ask.
    """
    cleaned = camel_to_snake_payload(payload)
    cleaned.pop("action", None)
    if model is None:
        return cleaned, {}
    if partial:
        # Validate only provided keys by building a patch-shaped dict the model may still reject.
        try:
            model.model_validate(cleaned)
        except ValidationError as exc:
            # Incomplete bodies are expected; keep type/constraint errors only for present keys.
            errors = {
                str((err.get("loc") or ("",))[0]): str(err.get("msg", "invalid"))
                for err in exc.errors()
                if (err.get("loc") or ("",))[0] in cleaned and err.get("type") != "missing"
            }
            if errors:
                kept = {k: v for k, v in cleaned.items() if k not in errors}
                return kept, errors
        return cleaned, {}
    try:
        parsed = model.model_validate(cleaned)
    except ValidationError as exc:
        errors = {
            str((err.get("loc") or ("",))[0]): str(err.get("msg", "invalid"))
            for err in exc.errors()
        }
        return {}, errors
    data = parsed.model_dump(mode="json")
    # Dates stay as the owner wrote them when the paste used a date string the model accepted.
    if "date" in cleaned:
        data["date"] = cleaned["date"]
    return data, {}


def merge_labelled(payload: dict[str, Any], labelled: dict[str, Any]) -> dict[str, Any]:
    """Overlay labelled values; extras dicts are merged key by key."""
    if not labelled:
        return payload
    out = dict(payload)
    for key, value in labelled.items():
        if key == "extras" and isinstance(value, dict):
            merged = dict(out.get("extras") or {})
            merged.update({str(k): str(v) for k, v in value.items()})
            out["extras"] = merged
        elif key == "notes":
            out["notes"] = _join_notes(out.get("notes"), str(value))
        else:
            out[key] = value
    return out
