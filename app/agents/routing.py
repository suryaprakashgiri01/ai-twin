"""The routing decision schema and the keyword fallback used when no router model answers.

The router model fills `RouteDecision` through structured output. When it cannot (no eligible
model, every model failed, invalid output), `heuristic_route` decides from keywords, so the
owner still gets an answer with only the mock or an unreachable local model.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Literal

from pydantic import BaseModel, Field

from app.agents.tools.records import GroupBy, RecordsQuery
from app.services.profile import RecordKind

Route = Literal["kb_search", "records", "profile", "chat", "write"]
Guard = Literal["none", "records_rejected", "filters_dropped"]


class RouteDecision(BaseModel):
    route: Route
    # The question rewritten to stand alone (follow-ups resolved from history).
    query: str = Field(min_length=1, max_length=2000)
    records: RecordsQuery | None = None


_COUNT = re.compile(r"\b(how many|number of|count|list (?:my|all)|which compan(?:y|ies))\b", re.I)
_DATE_WORDS = re.compile(
    r"\b(this|last) (week|month|year)\b|\b(" + "|".join(calendar.month_name[1:]) + r")\b", re.I
)
# Checked in order: outcomes first, so "offer after the interview" counts offers.
_KIND = {
    "offer": re.compile(r"\b(offers?|offered)\b", re.I),
    "rejection": re.compile(r"\b(rejections?|rejected|turned down)\b", re.I),
    "withdrawal": re.compile(r"\b(withdr[ae]wn?|withdrawals?|pulled out)\b", re.I),
    # Before interview and application: "HR interview call", "invited to apply".
    "hr_call": re.compile(
        r"\b(hr|recruiter|talent acquisition|ta)[- ](calls?|screens?|screening|chats?|"
        r"discussions?|conversations?)\b|\bcalls? (from|with) (an? |the )?(hr|recruiter)\b|"
        r"\bhr[_ ]calls?\b",
        re.I,
    ),
    "invite": re.compile(
        r"\b(application )?invit(es?|ations?|ed)\b|\brecruiter (messages?|outreach|reached out)\b|"
        r"\breached out\b",
        re.I,
    ),
    "assessment": re.compile(
        r"\b(assessments?|assignments?|take[- ]?homes?|online tests?|coding tests?|"
        r"coding challenges?|hackerrank|codility)\b",
        re.I,
    ),
    "interview": re.compile(r"\binterview(?:s|ed)?\b", re.I),
    "application": re.compile(r"\b(applications?|applied|apply)\b", re.I),
}
_PROFILE = re.compile(
    r"\b(availability|available|notice period|preferred roles?|locations?|relocat\w*|"
    r"open to work)\b",
    re.I,
)
_CHAT = re.compile(r"^\s*(hi|hello|hey|thanks|thank you|ok|okay|cool)\b[\s!.?]*$", re.I)
_FOLLOW_UP = re.compile(
    r"^\s*(and|what about|how about|also)\b|\b(that one|before that|the one|those)\b", re.I
)


# Owner chat only reads. A request to add, change or fetch something belongs to the intake
# assistant (`make twin`), which confirms before writing.
_WRITE_VERB = re.compile(
    r"^\s*(?:please\s+|pls\s+|can you\s+|could you\s+|kindly\s+)?"
    r"(add|ingest|save|store|sync|upload|import|publish|unpublish|delete|remove|record|log|"
    r"track|update|change|set|fetch|check|browse|crawl|scrape)\b",
    re.I,
)
_WRITE_PHRASE = re.compile(
    r"\b(please add|add this|add these|add it|save this|sync (my|this|the)|ingest (this|my)|"
    r"(check|fetch|look at|browse) (my )?(github|repo|profile|website|link|url))\b|"
    r"\badd (this|these|it) (source|link|url|repo)",
    re.I,
)


_QUESTION = re.compile(
    r"^\s*(how|what|which|when|where|who|why|did|do|does|is|are|was|were)\b", re.I
)
_BROWSE = re.compile(
    r"\b(check|fetch|browse|look up|look at|visit|open|go to|crawl|scrape)\b.{0,60}?"
    r"\b(github|git hub|website|site|web|online|internet|link|url|profile)\b|"
    r"\b(github|website|web|online|link|url)\b.{0,60}?\b(check|fetch|browse|look up|visit)\b",
    re.I,
)


def looks_like_write(text: str) -> bool:
    """An imperative to add, change or fetch something, or a request to browse the web; not a
    question about what the vault already holds."""
    if _BROWSE.search(text) or _WRITE_PHRASE.search(text):
        return True
    return not _QUESTION.match(text) and bool(_WRITE_VERB.match(text))


def looks_like_records(text: str) -> bool:
    return bool(_COUNT.search(text) or _DATE_WORDS.search(text))


def _kind(text: str) -> RecordKind | None:
    for kind, pattern in _KIND.items():
        if pattern.search(text):
            return kind  # type: ignore[return-value]
    return None


def canonical_kind(value: str) -> str:
    """A record kind from loose wording ("take-home", "got rejected"); unknown text is returned
    unchanged so validation can name it."""
    cleaned = value.strip().lower()
    if cleaned in _KIND:
        return cleaned
    return _kind(cleaned) or cleaned


def _month_range(month: int, today: date) -> tuple[date, date]:
    # A month name means its most recent occurrence: this year's, unless that is still ahead.
    year = today.year if month <= today.month else today.year - 1
    return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])


def _relative_range(which: str, unit: str, today: date) -> tuple[date, date]:
    if unit == "week":
        start = today - timedelta(days=today.weekday())
        if which == "last":
            start -= timedelta(days=7)
        return start, start + timedelta(days=6)
    if unit == "month":
        first = today.replace(day=1)
        if which == "last":
            first = (first - timedelta(days=1)).replace(day=1)
        return first, first.replace(day=calendar.monthrange(first.year, first.month)[1])
    year = today.year - (1 if which == "last" else 0)
    return date(year, 1, 1), date(year, 12, 31)


def date_range(text: str, today: date) -> tuple[date | None, date | None]:
    match = _DATE_WORDS.search(text)
    if match is None:
        return None, None
    if match.group(1):
        return _relative_range(match.group(1).lower(), match.group(2).lower(), today)
    month = list(calendar.month_name).index(match.group(3).capitalize())
    return _month_range(month, today)


# Job platforms an invite or application comes through, by the name the owner uses.
PLATFORMS: dict[str, str] = {
    "naukri": "Naukri",
    "linkedin": "LinkedIn",
    "instahyre": "Instahyre",
    "indeed": "Indeed",
    "hirist": "Hirist",
    "iimjobs": "iimjobs",
    "cutshort": "Cutshort",
    "wellfound": "Wellfound",
    "angellist": "Wellfound",
    "foundit": "foundit",
    "monster": "foundit",
    "glassdoor": "Glassdoor",
    "referral": "Referral",
}
_PLATFORM = re.compile(r"\b(" + "|".join(PLATFORMS) + r")\b", re.I)
_BY_PLATFORM = re.compile(r"\b(which|what|per|by) (platform|site|portal|source)s?\b", re.I)


def platform_in(text: str) -> str | None:
    match = _PLATFORM.search(text)
    return PLATFORMS[match.group(1).lower()] if match else None


def canonical_platform(value: str) -> str:
    """A known platform's usual spelling ("naukri.com" → "Naukri"); anything else as given."""
    return platform_in(value) or value.strip()


def records_query(text: str, today: date) -> RecordsQuery | None:
    kind = _kind(text)
    if kind is None:
        return None
    date_from, date_to = date_range(text, today)
    group_by: GroupBy | None = None
    if re.search(r"\bwhich compan", text, re.I):
        group_by = "company"
    elif _BY_PLATFORM.search(text):
        group_by = "platform"
    return RecordsQuery(
        kind=kind,
        platform=platform_in(text),
        date_from=date_from,
        date_to=date_to,
        group_by=group_by,
    )


def standalone(question: str, previous_question: str | None) -> str:
    """Fold a short follow-up into the previous question; otherwise keep it as asked."""
    if previous_question and _FOLLOW_UP.search(question) and len(question.split()) <= 8:
        return f"{previous_question} {question}"
    return question


def heuristic_route(question: str, previous_question: str | None, today: date) -> RouteDecision:
    query = standalone(question, previous_question)
    if _CHAT.match(question):
        return RouteDecision(route="chat", query=query)
    if looks_like_records(query):
        records = records_query(query, today)
        if records is not None:
            # A follow-up's own dates ("and in August?") win over the folded-in question's.
            own_from, own_to = date_range(question, today)
            if own_from is not None:
                records = records.model_copy(update={"date_from": own_from, "date_to": own_to})
            return RouteDecision(route="records", query=query, records=records)
    if _PROFILE.search(query):
        return RouteDecision(route="profile", query=query)
    return RouteDecision(route="kb_search", query=query)


@dataclass(frozen=True)
class GuardResult:
    decision: RouteDecision
    guard: Guard = "none"
    dropped: tuple[str, ...] = ()


def guard_records(
    decision: RouteDecision, question: str, previous_question: str | None, today: date
) -> GuardResult:
    """Check a router model's `records` route against the owner's own words.

    A small router can send a follow-up to `records` with an invented rewrite and filters. The
    route stands only if the question (a short follow-up folded into the previous question) looks
    like a records question; otherwise the keyword router decides. On an accepted route, company
    and stage filters the text never mentions are dropped. The model's rewrite is not trusted here.
    """
    if decision.route != "records":
        return GuardResult(decision)
    text = standalone(question, previous_question)
    if not looks_like_records(text):
        return GuardResult(heuristic_route(question, previous_question, today), "records_rejected")
    if decision.records is None:
        return GuardResult(decision)
    lowered = text.casefold()
    dropped = tuple(
        name
        for name in ("company", "stage", "platform")
        if (value := getattr(decision.records, name)) and value.casefold() not in lowered
    )
    if not dropped:
        return GuardResult(decision)
    records = decision.records.model_copy(update=dict.fromkeys(dropped))
    return GuardResult(decision.model_copy(update={"records": records}), "filters_dropped", dropped)
