"""Alert policies over stored security events, and the alert and digest emails.

| Event kind | Policy |
|---|---|
| `canary_hit` | immediate, always |
| `output_blocked` | immediate when >= 3 in 10 min, else digest |
| `input_blocked` | immediate when >= 5 from one visitor in 10 min, else digest |
| `auth_failed` (admin and public) | immediate when >= 5 in 10 min, else digest |
| `lead_delivery_failed` | immediate |
| `meta_probe`, `rate_limited`, `lead_refused`, `grounding_fail_open` | digest |
| `llm_unavailable`, `ingestion_failed` (ops) | immediate when >= 5 in 10 min, else digest |

Every immediate policy has a cooldown (default 30 min): events arriving while it cools down stay
pending, and once it ends they go out in one follow-up email with their count. Every event,
alerted or not, is counted in the next daily digest.

An email holds kinds, severities, counts, rule names, short visitor-hash prefixes and times. It
never holds message or document text, names, email addresses, paths or ids: the renderers only
read those fields of an event, and rule names are reduced to a safe alphabet.
"""

from __future__ import annotations

import html
import re
from collections import Counter
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid
from typing import Any, Literal, Protocol
from zoneinfo import ZoneInfo

from beanie import PydanticObjectId
from beanie.operators import In

from app.db.documents import SecurityEvent
from app.leads.email import Mailer, subject_line

Mode = Literal["immediate", "threshold", "digest"]
SEVERITY_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}
WINDOW = timedelta(minutes=10)
VISITOR_PREFIX = 8
MAX_RULES = 10
MAX_VISITORS = 5
_SAFE_RULE = re.compile(r"[^A-Za-z0-9_:.\-]")


@dataclass(frozen=True)
class Policy:
    name: str
    kinds: frozenset[str]
    mode: Mode
    threshold: int = 1
    window: timedelta = WINDOW
    # Count per visitor hash instead of in total.
    per_visitor: bool = False

    def describe(self) -> str:
        if self.mode == "immediate":
            return "immediate"
        if self.mode == "digest":
            return "digest"
        who = " from one visitor" if self.per_visitor else ""
        minutes = int(self.window.total_seconds() // 60)
        return f"immediate when >= {self.threshold}{who} in {minutes} min, else digest"


def _policy(name: str, mode: Mode, threshold: int = 1, *, per_visitor: bool = False) -> Policy:
    return Policy(name, frozenset({name}), mode, threshold, per_visitor=per_visitor)


POLICIES: tuple[Policy, ...] = (
    _policy("canary_hit", "immediate"),
    _policy("output_blocked", "threshold", 3),
    _policy("input_blocked", "threshold", 5, per_visitor=True),
    _policy("auth_failed", "threshold", 5),
    _policy("lead_delivery_failed", "immediate"),
    _policy("llm_unavailable", "threshold", 5),
    _policy("ingestion_failed", "threshold", 5),
    _policy("meta_probe", "digest"),
    _policy("rate_limited", "digest"),
    _policy("lead_refused", "digest"),
    _policy("grounding_fail_open", "digest"),
)


@dataclass(frozen=True)
class StoredEvent:
    """What the alert path reads of an event: never its details beyond rule names."""

    id: str
    kind: str
    severity: str
    rules: tuple[str, ...]
    visitor_hash: str | None
    surface: str | None
    created_at: datetime
    alerted_at: datetime | None = None
    digested_at: datetime | None = None
    # The public id the admin API shows; `id` stays internal (alert and digest marks).
    uid: str = ""


class EventStore(Protocol):
    async def pending(self, kinds: Collection[str], since: datetime) -> list[StoredEvent]:
        """Not yet alerted, created at or after `since`, oldest first."""
        ...

    async def mark_alerted(self, ids: Sequence[str], at: datetime) -> None: ...

    async def undigested(self, since: datetime, before: datetime) -> list[StoredEvent]: ...

    async def mark_digested(self, ids: Sequence[str], at: datetime) -> None: ...

    async def recent(
        self,
        *,
        kind: str | None = None,
        severity: str | None = None,
        since: datetime | None = None,
        limit: int = 50,
    ) -> list[StoredEvent]:
        """Newest first."""
        ...


def _stored(doc: SecurityEvent) -> StoredEvent:
    rules = doc.details.get("rules") or []
    return StoredEvent(
        id=str(doc.id),
        kind=doc.kind,
        severity=doc.severity,
        rules=tuple(str(r) for r in rules if isinstance(r, str | int | float)),
        visitor_hash=doc.visitor_hash,
        surface=doc.surface,
        created_at=doc.created_at,
        alerted_at=doc.alerted_at,
        digested_at=doc.digested_at,
        uid=doc.uid,
    )


def _ids(ids: Sequence[str]) -> list[PydanticObjectId]:
    return [PydanticObjectId(i) for i in ids]


class MongoEventStore:
    async def pending(self, kinds: Collection[str], since: datetime) -> list[StoredEvent]:
        docs = (
            await SecurityEvent.find(
                In(SecurityEvent.kind, list(kinds)),
                SecurityEvent.alerted_at == None,  # noqa: E711  # beanie query: matches missing too
                SecurityEvent.created_at >= since,
            )
            .sort("+created_at")
            .to_list()
        )
        return [_stored(d) for d in docs]

    async def mark_alerted(self, ids: Sequence[str], at: datetime) -> None:
        if ids:
            await SecurityEvent.find(In(SecurityEvent.id, _ids(ids))).update(
                {"$set": {"alerted_at": at}}
            )

    async def undigested(self, since: datetime, before: datetime) -> list[StoredEvent]:
        docs = (
            await SecurityEvent.find(
                SecurityEvent.digested_at == None,  # noqa: E711
                SecurityEvent.created_at >= since,
                SecurityEvent.created_at < before,
            )
            .sort("+created_at")
            .to_list()
        )
        return [_stored(d) for d in docs]

    async def mark_digested(self, ids: Sequence[str], at: datetime) -> None:
        if ids:
            await SecurityEvent.find(In(SecurityEvent.id, _ids(ids))).update(
                {"$set": {"digested_at": at}}
            )

    async def recent(
        self,
        *,
        kind: str | None = None,
        severity: str | None = None,
        since: datetime | None = None,
        limit: int = 50,
    ) -> list[StoredEvent]:
        query: dict[str, Any] = {}
        if kind is not None:
            query["kind"] = kind
        if severity is not None:
            query["severity"] = severity
        if since is not None:
            query["created_at"] = {"$gte": since}
        docs = await SecurityEvent.find(query).sort("-created_at").limit(limit).to_list()
        return [_stored(d) for d in docs]


# --- deciding ---------------------------------------------------------------------------------


def threshold_met(policy: Policy, events: Sequence[StoredEvent], now: datetime) -> bool:
    recent = [e for e in events if e.created_at >= now - policy.window]
    if not policy.per_visitor:
        return len(recent) >= policy.threshold
    per_visitor = Counter(e.visitor_hash for e in recent if e.visitor_hash)
    return bool(per_visitor) and max(per_visitor.values()) >= policy.threshold


def should_alert(
    policy: Policy,
    pending: Sequence[StoredEvent],
    now: datetime,
    last_sent: datetime | None,
    cooldown: timedelta,
) -> bool:
    """Whether `pending` warrants an email now (the caller has checked the cooldown is over)."""
    if policy.mode == "digest" or not pending:
        return False
    if policy.mode == "immediate":
        return True
    if threshold_met(policy, pending, now):
        return True
    # Events that arrived while the last alert cooled down: the storm's follow-up count.
    return last_sent is not None and any(
        last_sent < e.created_at <= last_sent + cooldown for e in pending
    )


# --- rendering --------------------------------------------------------------------------------


def safe_rule(rule: str) -> str:
    return _SAFE_RULE.sub("_", rule)[:64]


def top_severity(events: Sequence[StoredEvent]) -> str:
    return max((e.severity for e in events), key=lambda s: SEVERITY_ORDER.get(s, 0))


def _when(moment: datetime, timezone: ZoneInfo) -> str:
    return moment.astimezone(timezone).strftime("%d %b %Y, %H:%M %Z")


def _rules(events: Sequence[StoredEvent]) -> list[str]:
    counts = Counter(safe_rule(r) for e in events for r in e.rules)
    return [f"{rule} x{n}" for rule, n in counts.most_common(MAX_RULES)]


def _visitors(events: Sequence[StoredEvent]) -> str | None:
    prefixes = sorted({e.visitor_hash[:VISITOR_PREFIX] for e in events if e.visitor_hash})
    if not prefixes:
        return None
    shown = ", ".join(prefixes[:MAX_VISITORS])
    more = len(prefixes) - MAX_VISITORS
    return f"{shown} (+{more} more)" if more > 0 else shown


def _message(subject: str, mailer: Mailer) -> EmailMessage:
    message = EmailMessage()
    message["Subject"] = subject_line(subject)
    message["From"] = mailer.sender
    message["To"] = mailer.owner
    message["Date"] = format_datetime(datetime.now().astimezone())
    message["Message-ID"] = make_msgid(domain="ai-twin.local")
    return message


def _finish(message: EmailMessage, lines: list[str]) -> EmailMessage:
    message.set_content("\n".join(lines))
    body = "".join(f"<p>{html.escape(line)}</p>" if line else "<br>" for line in lines)
    message.add_alternative(
        f"<html><body style='font-family:sans-serif'>{body}</body></html>", subtype="html"
    )
    return message


def render_alert(
    policy: Policy,
    events: Sequence[StoredEvent],
    mailer: Mailer,
    timezone: ZoneInfo,
    *,
    cooldown: timedelta,
    follow_up: bool = False,
) -> EmailMessage:
    severity = top_severity(events)
    message = _message(f"ai-twin alert · {severity} · {policy.name}", mailer)
    severities = Counter(e.severity for e in events)
    surfaces = Counter(e.surface for e in events if e.surface)
    lines = [
        f"Security alert: {policy.name} ({policy.describe()}).",
        "",
        f"Events: {len(events)}" + (" since the last alert" if follow_up else ""),
        "Severity: " + ", ".join(f"{s} x{n}" for s, n in sorted(severities.items())),
        f"First: {_when(events[0].created_at, timezone)}",
        f"Last: {_when(events[-1].created_at, timezone)}",
    ]
    if rules := _rules(events):
        lines.append("Rules: " + ", ".join(rules))
    if surfaces:
        lines.append("Surface: " + ", ".join(f"{s} x{n}" for s, n in sorted(surfaces.items())))
    if visitors := _visitors(events):
        lines.append(f"Visitors (hash prefix): {visitors}")
    minutes = int(cooldown.total_seconds() // 60)
    lines += [
        "",
        f"The next {policy.name} alert waits at least {minutes} min; events until then are "
        "counted in it, and every event is in the daily digest.",
        f"Details: GET /admin/v1/security/events?kind={policy.name}",
    ]
    return _finish(message, lines)


def render_digest(
    events: Sequence[StoredEvent], day: date, mailer: Mailer, timezone: ZoneInfo
) -> EmailMessage:
    message = _message(f"ai-twin daily security digest · {day.isoformat()}", mailer)
    by_kind = Counter((e.kind, e.severity) for e in events)
    alerted = Counter(e.kind for e in events if e.alerted_at is not None)
    lines = [
        f"Security events since the last digest: {len(events)}.",
        f"First: {_when(events[0].created_at, timezone)}",
        f"Last: {_when(events[-1].created_at, timezone)}",
        "",
        "By kind and severity:",
    ]
    for (kind, severity), n in sorted(by_kind.items()):
        note = f" ({alerted[kind]} already alerted)" if alerted[kind] else ""
        lines.append(f"- {kind} · {severity}: {n}{note}")
    if rules := _rules(events):
        lines += ["", "Top rules: " + ", ".join(rules)]
    lines += ["", "Details: GET /admin/v1/security/events"]
    return _finish(message, lines)


def render_test(mailer: Mailer, timezone: ZoneInfo, now: datetime) -> EmailMessage:
    message = _message("ai-twin alert · low · test", mailer)
    return _finish(
        message,
        [
            "Test alert from ai-twin: alert email delivery works.",
            f"Sent: {_when(now, timezone)}",
            "Nothing happened; this was requested with POST /admin/v1/alerts/test.",
        ],
    )
