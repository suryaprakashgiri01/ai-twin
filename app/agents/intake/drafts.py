"""Drafts: what the model extracted, made exact by code.

The model only proposes an action and field values. Code merges them into the draft, normalises
dates and lists, and decides completeness: required fields still empty are `missing`, values the
action's pydantic model rejects are `invalid`. A bad extraction costs one more question, never a
wrong write.
"""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from zoneinfo import ZoneInfo

import dateparser
from dateparser.search import search_dates
from pydantic import BaseModel, Field, ValidationError, field_validator

from app.agents.routing import canonical_kind

if TYPE_CHECKING:
    from app.agents.intake.actions import ActionSpec

ActionName = Literal[
    "add_note",
    "ingest_url",
    "ingest_file",
    "ingest_github",
    "add_record",
    "update_record",
    "delete_record",
    "update_profile",
    "show_review",
    "source_status",
    "approve",
    "publish",
    "unpublish",
    "relabel",
    "delete_document",
    "answer",
]

WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_ISO_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:[T ].*)?$")
_WEEKDAY = re.compile(
    r"^(?:(last|past|previous|this|on)\s+)?(mon|tues?|wed(?:nes)?|thu(?:rs?)?|fri|sat(?:ur)?|sun)"
    r"(?:day)?$"
)
_SPACE = re.compile(r"\s+")
_TOKEN = re.compile(r"[a-z0-9]+")
_WEEKDAY_IN_TEXT = re.compile(
    r"\b(?:(?:last|past|previous|this|on)\s+)?(?:mon|tues?|wed(?:nes)?|thu(?:rs?)?|fri|sat(?:ur)?"
    r"|sun)(?:day)?\b"
)
# Share of a value's words that must appear in the owner's message for it to count as theirs.
GROUNDED_SHARE = 0.6
# References to existing things: resolved against real data and shown in the draft, so they
# need not be in the owner's words ("delete it" names nothing).
REFERENCES = frozenset({"record", "document", "source_id"})
STEM = 5


ACTION_NAMES: tuple[str, ...] = ActionName.__args__  # type: ignore[attr-defined]
REPLY_MAX = 600


class IntakeDraft(BaseModel):
    """The model's reading of the owner's latest message. Never trusted for completeness.

    Lenient on purpose: a small model's unknown action, null payload or long reply should cost
    a question, not a failed turn."""

    action: str | None = Field(
        default=None,
        description="The action the owner wants, or null when there is none",
        json_schema_extra={"enum": [*ACTION_NAMES, None]},
    )
    payload: dict[str, Any] = Field(
        default_factory=dict,
        description="Only the fields the owner gave or changed, with their words for dates",
    )
    reply: str = Field(default="", description="A short reply when no action")

    @field_validator("action", mode="before")
    @classmethod
    def _known_action(cls, value: Any) -> str | None:
        name = str(value).strip().lower() if value is not None else ""
        return name if name in ACTION_NAMES else None

    @field_validator("payload", mode="before")
    @classmethod
    def _dict_payload(cls, value: Any) -> dict[str, Any]:
        return dict(value) if isinstance(value, dict) else {}

    @field_validator("reply", mode="before")
    @classmethod
    def _short_reply(cls, value: Any) -> str:
        return "" if value is None else str(value)[:REPLY_MAX]


@dataclass
class DraftCheck:
    payload: dict[str, Any]
    missing: list[str] = field(default_factory=list)
    # Field name -> why the value was rejected (the value is dropped from the payload).
    invalid: dict[str, str] = field(default_factory=dict)

    @property
    def complete(self) -> bool:
        return not self.missing and not self.invalid


# --- dates -------------------------------------------------------------------------------------


def local_today(tz: ZoneInfo, now: datetime) -> date:
    return now.astimezone(tz).date()


def _weekday(text: str, today: date) -> date | None:
    match = _WEEKDAY.match(text)
    if match is None:
        return None
    qualifier, stem = match.groups()
    target = next(i for i, name in enumerate(WEEKDAYS) if name.startswith(stem[:3]))
    back = (today.weekday() - target) % 7
    # "last Friday" on a Friday is a week ago; a bare "Friday" on a Friday is today.
    if back == 0 and qualifier in ("last", "past", "previous"):
        back = 7
    return today - timedelta(days=back)


def parse_day(value: Any, tz: ZoneInfo, now: datetime) -> date | None:
    """The calendar day the owner meant, in their time zone; None when it can't be read."""
    if isinstance(value, datetime):
        return value.astimezone(tz).date() if value.tzinfo else value.date()
    if isinstance(value, date):
        return value
    text = _SPACE.sub(" ", str(value)).strip().lower().rstrip(".")
    if not text:
        return None
    today = local_today(tz, now)
    if iso := _ISO_DATE.match(text):
        try:
            return date(int(iso[1]), int(iso[2]), int(iso[3]))
        except ValueError:
            return None
    if (weekday := _weekday(text, today)) is not None:
        return weekday
    parsed = dateparser.parse(text, languages=["en"], settings=_dateparser_settings(tz, now))
    return parsed.date() if parsed is not None else None


def _dateparser_settings(tz: ZoneInfo, now: datetime) -> Any:
    return {
        "TIMEZONE": str(tz),
        "RETURN_AS_TIMEZONE_AWARE": False,
        "DATE_ORDER": "DMY",
        "PREFER_DATES_FROM": "past",
        "RELATIVE_BASE": now.astimezone(tz).replace(tzinfo=None),
    }


def find_day(text: str, tz: ZoneInfo, now: datetime) -> date | None:
    """The first date phrase in free text ("round 2, yesterday", "last Friday"), or None."""
    lowered = text.casefold()
    if (match := _WEEKDAY_IN_TEXT.search(lowered)) is not None:
        return _weekday(match.group(0), local_today(tz, now))
    found = search_dates(lowered, languages=["en"], settings=_dateparser_settings(tz, now)) or []
    for phrase, when in found:
        if re.search(r"[a-z/-]", phrase):
            return when.date()
    return None


def day_value(day: date) -> datetime:
    """Records and notes keep the owner's calendar day as a naive midnight."""
    return datetime(day.year, day.month, day.day)


def show_day(value: Any) -> str:
    if isinstance(value, str):
        text = value
        try:
            value = datetime.fromisoformat(text)
        except ValueError:
            return text
    if isinstance(value, datetime | date):
        return value.strftime("%a %d %b %Y")
    return str(value)


# --- normalising and checking ------------------------------------------------------------------


def _clean(value: Any) -> Any:
    if isinstance(value, str):
        value = value.strip()
        return value or None
    if isinstance(value, list):
        items = [v for v in (_clean(v) for v in value) if v is not None]
        return items or None
    return value


def _as_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    return [str(v).strip() for v in value if str(v).strip()]


def _tokens(text: str) -> list[str]:
    return _TOKEN.findall(text.casefold())


def grounded(value: Any, source: str) -> bool:
    """True when the value is (mostly) in the owner's own words: every word of a short value,
    most words of a longer one. Words match on a shared stem ("interviewed" ~ "interview")."""
    want = _tokens(str(value))
    have = set(_tokens(source))
    if not want:
        return False

    def seen(token: str) -> bool:
        return token in have or (len(token) >= STEM and any(h[:STEM] == token[:STEM] for h in have))

    hits = sum(1 for token in want if seen(token))
    return hits == len(want) if len(want) <= 2 else hits / len(want) >= GROUNDED_SHARE


def ground(
    spec: ActionSpec,
    extracted: dict[str, Any],
    message: str,
    current: dict[str, Any],
    tz: ZoneInfo,
    now: datetime,
) -> tuple[dict[str, Any], list[str]]:
    """Keep only values the owner actually gave in `message` (or already in the draft); a
    small model fills gaps with plausible inventions. Dates the model computed are replaced by
    the date phrase found in the message. Returns the kept values and the dropped field names."""
    kept: dict[str, Any] = {}
    dropped: list[str] = []
    for key, value in extracted.items():
        if _clean(value) is None:
            continue
        if key in spec.inferred or key in REFERENCES or current.get(key) == value:
            kept[key] = value
        elif key in spec.date_fields:
            day = find_day(message, tz, now)
            if grounded(value, message):
                kept[key] = value
            elif day is not None:
                kept[key] = day_value(day)
            else:
                dropped.append(key)
        elif key in spec.list_fields and isinstance(value, list | str):
            items = [v for v in _as_list(value) if grounded(v, message)]
            if items:
                kept[key] = items
            else:
                dropped.append(key)
        elif grounded(value, message):
            kept[key] = value
        else:
            dropped.append(key)
    return kept, dropped


def merge(current: dict[str, Any], update: dict[str, Any]) -> dict[str, Any]:
    """The draft so far with the newly extracted values on top; empty values never erase."""
    merged = dict(current)
    for key, value in update.items():
        cleaned = _clean(value)
        if cleaned is not None:
            merged[key] = cleaned
    return merged


def normalise(
    spec: ActionSpec, payload: dict[str, Any], tz: ZoneInfo, now: datetime
) -> tuple[dict[str, Any], dict[str, str]]:
    """Known fields only, lists split, dates resolved to a calendar day. Returns the payload
    and the fields that could not be read."""
    out: dict[str, Any] = {}
    invalid: dict[str, str] = {}
    for key, value in payload.items():
        if key not in spec.fields and key not in spec.resolved:
            continue
        if key in spec.list_fields:
            value = _as_list(value)
        elif key in spec.date_fields:
            day = parse_day(value, tz, now)
            if day is None:
                invalid[key] = f"couldn't read the date {str(value)[:40]!r}"
                continue
            value = day_value(day)
        if key in spec.lower_fields and isinstance(value, str):
            value = value.lower()
        if key == "kind" and isinstance(value, str):
            value = canonical_kind(value)
        out[key] = value
    return out, invalid


def _field_of(error: Any) -> str:
    loc = error.get("loc") or ("",)
    return f"{loc[0]}"


def check(spec: ActionSpec, payload: dict[str, Any]) -> DraftCheck:
    """Required fields first; the pydantic model then rejects bad values field by field."""
    result = DraftCheck(payload=dict(payload))
    result.missing = [name for name in spec.required if result.payload.get(name) in (None, [], "")]
    if spec.needs_any and not any(result.payload.get(name) for name in spec.needs_any):
        result.missing.append(" or ".join(spec.needs_any))
    if result.missing or spec.model is None:
        return result
    try:
        spec.model.model_validate(spec.model_input(result.payload))
    except ValidationError as exc:
        for error in exc.errors():
            name = _field_of(error)
            result.invalid[name] = str(error.get("msg", "invalid"))
            result.payload.pop(name, None)
    return result


# --- the inbox ---------------------------------------------------------------------------------


class InboxError(Exception):
    """A file name the inbox refuses; the message is safe to show the owner."""


def resolve_inbox(inbox: Path, name: str) -> Path:
    """A regular file directly inside `inbox`, by bare name. Refuses paths, `..`, absolute
    names, symlinks and anything outside the folder. Only metadata is read, never the file."""
    clean = name.strip()
    if (
        not clean
        or clean in (".", "..")
        or any(sep in clean for sep in ("/", "\\", "\x00", ":"))
        or Path(clean).is_absolute()
    ):
        raise InboxError(f"Give a file name inside the inbox ({inbox}), not a path")
    root = inbox.resolve()
    candidate = root / clean
    try:
        info = os.lstat(candidate)
    except FileNotFoundError as exc:
        raise InboxError(f"No file named {clean!r} in the inbox ({inbox})") from exc
    if stat.S_ISLNK(info.st_mode):
        raise InboxError(f"{clean!r} is a link; copy the file itself into the inbox")
    if not stat.S_ISREG(info.st_mode):
        raise InboxError(f"{clean!r} is not a regular file")
    if candidate.parent != root:
        raise InboxError(f"{clean!r} is outside the inbox")
    return candidate


# --- GitHub repositories named in free text ------------------------------------------------------

_GITHUB_REPO_URL = re.compile(
    r"https?://(?:www\.)?github\.com/([A-Za-z0-9][A-Za-z0-9-]{0,38})/([A-Za-z0-9._-]+)", re.I
)
# A bare owner/name, not part of a URL or a path (`a/b/tree/develop` keeps `a/b`).
_BARE_REPO = re.compile(
    r"(?<![\w./:@-])([A-Za-z][A-Za-z0-9-]{0,38})/([A-Za-z0-9._-]*[A-Za-z][A-Za-z0-9._-]*)"
)
_URL = re.compile(r"https?://\S+", re.I)


def _repo(owner: str, name: str) -> str:
    name = name.removesuffix(".git").rstrip(".,;:")
    return f"{owner}/{name}".lower()


def github_repos(text: str) -> list[str]:
    """Every `owner/name` the owner wrote, as a GitHub URL or bare, in order and without repeats.
    Extra path parts (`/tree/develop`, `.git`) are dropped; a profile URL is not a repo."""
    found: list[str] = []
    for owner, name in _GITHUB_REPO_URL.findall(text):
        found.append(_repo(owner, name))
    without_urls = _URL.sub(" ", text)
    for owner, name in _BARE_REPO.findall(without_urls):
        found.append(_repo(owner, name))
    return list(dict.fromkeys(found))


def only_github_repo_urls(text: str) -> bool:
    """True when the message has URLs and every one of them points at a GitHub repository."""
    urls = _URL.findall(text)
    return bool(urls) and all(_GITHUB_REPO_URL.match(u) for u in urls)
