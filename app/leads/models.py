"""Lead fields and their validation. Code decides whether a lead is complete, never a model.

`LeadDraft` is what extraction may hand back: every field optional, nothing trusted. `check`
validates field by field, so one bad value costs one more question and never drops the others.
`LeadIn` is a complete, valid lead (the form endpoint takes it directly). A `link` is kept as
text: it is never fetched and never rendered as a link.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal, get_args

from email_validator import EmailNotValidError, validate_email
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from app.api.envelope import ApiModel

LeadSource = Literal["assistant", "form"]
# `new` waits for (or has had) its email; the owner moves it on from there.
LeadStatus = Literal["new", "reviewed", "replied", "archived", "suppressed", "failed"]
OwnerStatus = Literal["new", "reviewed", "replied", "archived"]
EmploymentType = Literal["full-time", "contract", "freelance"]
TriageLabel = Literal["recruiter", "hiring_manager", "client", "collaboration", "spam", "other"]

REQUIRED: tuple[str, ...] = ("role", "company", "message", "name", "email")
OPTIONAL: tuple[str, ...] = ("location", "employment_type", "link")
FIELDS: tuple[str, ...] = REQUIRED + OPTIONAL
LIMITS: Mapping[str, int] = {
    "name": 100,
    "email": 254,
    "company": 120,
    "role": 120,
    "message": 2000,
    "location": 120,
    "employment_type": 20,
    "link": 500,
}
EMPLOYMENT_TYPES: tuple[str, ...] = get_args(EmploymentType)
_EMPLOYMENT_ALIASES: Mapping[str, str] = {
    "full time": "full-time",
    "fulltime": "full-time",
    "full-time": "full-time",
    "permanent": "full-time",
    "fte": "full-time",
    "contract": "contract",
    "contractor": "contract",
    "contracting": "contract",
    "fixed term": "contract",
    "freelance": "freelance",
    "freelancer": "freelance",
    "freelancing": "freelance",
    "consulting": "freelance",
    "consultant": "freelance",
}
# What a visitor has to say for an employment type to count as theirs.
EMPLOYMENT_WORDS: frozenset[str] = frozenset(k.replace("-", " ") for k in _EMPLOYMENT_ALIASES) | {
    "full time",
    "part time",
}
# Control characters (CR/LF included) never survive into a stored value.
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_LINE_BREAKS = re.compile(r"[\r\n\u2028\u2029]+")
_LINK = re.compile(r"^https?://[^\s<>\"']+$", re.IGNORECASE)


def clean(value: str, *, multiline: bool = False) -> str:
    """Strip control characters; single-line fields lose line breaks and extra spaces too."""
    text = value.replace("\r\n", "\n").replace("\r", "\n").replace("\u2028", "\n")
    if not multiline:
        text = _LINE_BREAKS.sub(" ", text)
    text = _CONTROL.sub("", text)
    return text.strip() if multiline else " ".join(text.split())


def email_hash(email: str, salt: str) -> str:
    """What logs carry instead of a visitor's address: a keyed, one-way hash."""
    return hmac.new(salt.encode(), email.casefold().encode(), hashlib.sha256).hexdigest()[:16]


def normalise_email(value: str) -> str:
    """The address as validated (domain lower-cased); raises ValueError when invalid."""
    try:
        result = validate_email(clean(value), check_deliverability=False)
    except EmailNotValidError as exc:
        raise ValueError("not a valid email address") from exc
    return result.normalized


def normalise_employment(value: str) -> str:
    key = clean(value).casefold().replace("_", " ").replace("/", " ")
    found = _EMPLOYMENT_ALIASES.get(key) or _EMPLOYMENT_ALIASES.get(key.replace("-", " "))
    if found is None:
        raise ValueError(f"one of {', '.join(EMPLOYMENT_TYPES)}")
    return found


def normalise_link(value: str) -> str:
    text = clean(value)
    if not _LINK.match(text):
        raise ValueError("an http(s) address")
    return text


def _text(name: str, *, multiline: bool = False) -> Any:
    def check(value: str) -> str:
        text = clean(value, multiline=multiline)
        if not text:
            raise ValueError("empty")
        if len(text) > LIMITS[name]:
            raise ValueError(f"at most {LIMITS[name]} characters")
        return text

    return AfterValidator(check)


Name = Annotated[str, _text("name")]
Company = Annotated[str, _text("company")]
RoleText = Annotated[str, _text("role")]
Message = Annotated[str, _text("message", multiline=True)]
Location = Annotated[str, _text("location")]
Email = Annotated[str, AfterValidator(normalise_email)]
Employment = Annotated[str, AfterValidator(normalise_employment)]
Link = Annotated[str, AfterValidator(normalise_link)]


class LeadDraft(BaseModel):
    """What the extraction model may return. Every field optional and unchecked."""

    model_config = ConfigDict(extra="ignore")

    name: str | None = None
    email: str | None = None
    company: str | None = None
    role: str | None = None
    message: str | None = None
    location: str | None = None
    employment_type: str | None = None
    link: str | None = None


class LeadIn(ApiModel):
    """A complete, valid lead: what is stored and emailed."""

    model_config = ConfigDict(extra="forbid")

    name: Name
    email: Email
    company: Company
    role: RoleText
    message: Message
    location: Location | None = None
    employment_type: Employment | None = None
    link: Link | None = None

    def details(self) -> dict[str, str]:
        return {k: v for k in OPTIONAL if (v := getattr(self, k)) is not None}


class LeadForm(LeadIn):
    """The form endpoint's body: a lead plus the Turnstile token the widget passes through."""

    turnstile_token: str | None = Field(default=None, max_length=2048)


# One validator per field, so a draft is checked field by field.
_ADAPTERS: dict[str, TypeAdapter[str]] = {
    "name": TypeAdapter(Name),
    "email": TypeAdapter(Email),
    "company": TypeAdapter(Company),
    "role": TypeAdapter(RoleText),
    "message": TypeAdapter(Message),
    "location": TypeAdapter(Location),
    "employment_type": TypeAdapter(Employment),
    "link": TypeAdapter(Link),
}


def check_field(name: str, value: str) -> str:
    """The cleaned value, or ValueError with a short reason."""
    try:
        checked = _ADAPTERS[name].validate_python(value)
    except ValidationError as exc:
        reason = str(exc.errors()[0].get("msg", "invalid")).removeprefix("Value error, ")
        raise ValueError(reason) from None
    return str(checked)


@dataclass
class DraftCheck:
    values: dict[str, str] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)
    invalid: dict[str, str] = field(default_factory=dict)

    @property
    def complete(self) -> bool:
        return not self.missing and not self.invalid

    def lead(self) -> LeadIn:
        return LeadIn.model_validate(self.values)


def check(draft: Mapping[str, Any]) -> DraftCheck:
    """Validate a draft field by field. Missing required fields come back in asking order;
    an invalid value is dropped and reported (a required one is then missing too)."""
    result = DraftCheck()
    for name in FIELDS:
        raw = draft.get(name)
        if raw is None or not str(raw).strip():
            continue
        try:
            result.values[name] = check_field(name, str(raw))
        except ValueError as exc:
            result.invalid[name] = str(exc)
    result.missing = [n for n in REQUIRED if n not in result.values]
    return result


def dedupe_key(value: str) -> str:
    """Email and company as compared for duplicates: case and spacing ignored."""
    return " ".join(value.casefold().split())
