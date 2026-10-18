"""The owner's lead emails: rendering (every value escaped) and the SMTP client.

Mail only ever goes to `LEAD_EMAIL_TO`. The mailer holds that address and passes it to SMTP as
the one envelope recipient; nothing in a lead can add another. `From` is always the owner's
address and `Reply-To` is the visitor's, so the owner replies from their own inbox. There is no
acknowledgement to the visitor, so the service can't be used as a relay.
"""

from __future__ import annotations

import html
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid
from typing import Literal, Protocol
from zoneinfo import ZoneInfo

import aiosmtplib
import structlog

from app.core.config import Settings
from app.leads.models import clean, normalise_email
from app.leads.store import StoredLead

logger = structlog.get_logger(__name__)

SUBJECT_PREFIX = "Portfolio AI ·"
SUBJECT_MAX = 150
Security = Literal["starttls", "tls", "none"]


class MailError(Exception):
    """Sending failed; `code` is a short, content-free reason for logs and the lead record."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class MailSettings:
    host: str
    port: int
    username: str
    password: str
    security: Security
    to: str
    sender: str
    timeout: float = 20.0

    @classmethod
    def from_settings(cls, settings: Settings) -> MailSettings | None:
        """None until SMTP and both addresses are configured."""
        if not (settings.smtp_host and settings.lead_email_to and settings.lead_email_from):
            return None
        return cls(
            host=settings.smtp_host,
            port=settings.smtp_port,
            username=settings.smtp_username,
            password=settings.smtp_password.get_secret_value(),
            security=settings.smtp_security,
            to=settings.lead_email_to,
            sender=settings.lead_email_from,
            timeout=settings.smtp_timeout_seconds,
        )


class Mailer(Protocol):
    """Sends a rendered message to the owner. There is no recipient parameter by design."""

    @property
    def owner(self) -> str: ...

    @property
    def sender(self) -> str: ...

    async def send(self, message: EmailMessage) -> None: ...


class SmtpMailer:
    def __init__(self, settings: MailSettings) -> None:
        self._s = settings

    @property
    def owner(self) -> str:
        return self._s.to

    @property
    def sender(self) -> str:
        return self._s.sender

    async def send(self, message: EmailMessage) -> None:
        s = self._s
        if message["To"] != s.to or message["Cc"] or message["Bcc"]:
            raise MailError("recipient_mismatch")
        try:
            await aiosmtplib.send(
                message,
                sender=s.sender,
                recipients=[s.to],
                hostname=s.host,
                port=s.port,
                username=s.username or None,
                password=s.password or None,
                use_tls=s.security == "tls",
                start_tls=s.security == "starttls",
                timeout=s.timeout,
            )
        except aiosmtplib.SMTPException as exc:
            raise MailError(type(exc).__name__) from exc
        except OSError as exc:
            raise MailError(type(exc).__name__) from exc


# --- rendering ------------------------------------------------------------------------------


def subject_line(text: str) -> str:
    """One line (CR/LF and control characters stripped), capped in length."""
    line = clean(text)
    return line if len(line) <= SUBJECT_MAX else line[: SUBJECT_MAX - 1].rstrip() + "…"


def _e(value: object) -> str:
    return html.escape(str(value), quote=True)


def _received(lead: StoredLead, timezone: ZoneInfo) -> str:
    return lead.created_at.astimezone(timezone).strftime("%d %b %Y, %H:%M %Z")


def _rows(lead: StoredLead) -> list[tuple[str, str]]:
    rows = [
        ("Name", lead.name),
        ("Email", lead.email),
        ("Company", lead.company or ""),
        ("Role", lead.role or ""),
        ("Type", lead.details.employment_type or ""),
        ("Location", lead.details.location or ""),
        ("Link (not opened)", lead.details.link or ""),
        ("Source", "AI assistant" if lead.source == "assistant" else "contact form"),
    ]
    return [(k, v) for k, v in rows if v]


def _triage(lead: StoredLead) -> str:
    label = lead.triage or "unclassified"
    if lead.triage_confidence is None:
        return label
    return f"{label} ({lead.triage_confidence:.0%})"


def _reply_to(lead: StoredLead) -> str | None:
    try:
        return normalise_email(lead.email)
    except ValueError:
        return None


def _message(subject: str, sender: str, to: str, reply_to: str | None) -> EmailMessage:
    message = EmailMessage()
    message["Subject"] = subject_line(subject)
    message["From"] = sender
    message["To"] = to
    if reply_to:
        message["Reply-To"] = reply_to
    message["Date"] = format_datetime(datetime.now().astimezone())
    message["Message-ID"] = make_msgid(domain="ai-twin.local")
    return message


def render_lead(lead: StoredLead, mailer: Mailer, timezone: ZoneInfo) -> EmailMessage:
    subject = f"{SUBJECT_PREFIX} New opportunity: {lead.role} at {lead.company}"
    message = _message(subject, mailer.sender, mailer.owner, _reply_to(lead))
    summary = lead.summary or (
        "No summary (the conversation summary was unavailable)."
        if lead.source == "assistant"
        else "No conversation summary (sent through the contact form)."
    )
    fit = lead.fit_note or "No fit note."
    received = _received(lead, timezone)
    rows = _rows(lead)
    text = "\n".join(
        [
            "New opportunity from your portfolio.",
            "",
            *(f"{k}: {v}" for k, v in rows),
            "",
            "Their message:",
            *(f"> {line}" for line in lead.message.splitlines() or [""]),
            "",
            "Conversation summary:",
            summary,
            "",
            f"Triage: {_triage(lead)}",
            f"Fit: {fit}",
            f"Received: {received}",
            f"Lead id: {lead.id}",
            "",
            "Reply to this email to answer them directly.",
        ]
    )
    table = "".join(
        f"<tr><th align='left' style='padding:2px 12px 2px 0'>{_e(k)}</th><td>{_e(v)}</td></tr>"
        for k, v in rows
    )
    quoted = "<br>".join(_e(line) for line in lead.message.splitlines())
    body = (
        "<html><body style='font-family:sans-serif'>"
        "<p>New opportunity from your portfolio.</p>"
        f"<table>{table}</table>"
        "<h3>Their message</h3>"
        "<blockquote style='border-left:3px solid #ccc;margin:0;padding-left:12px'>"
        f"{quoted}</blockquote>"
        "<h3>Conversation summary</h3>"
        f"<p style='white-space:pre-wrap'>{_e(summary)}</p>"
        f"<p><b>Triage:</b> {_e(_triage(lead))}<br><b>Fit:</b> {_e(fit)}<br>"
        f"<b>Received:</b> {_e(received)}<br><b>Lead id:</b> {_e(lead.id)}</p>"
        "<p>Reply to this email to answer them directly.</p>"
        "</body></html>"
    )
    message.set_content(text)
    message.add_alternative(body, subtype="html")
    return message


def render_digest(leads: Sequence[StoredLead], mailer: Mailer, timezone: ZoneInfo) -> EmailMessage:
    subject = f"{SUBJECT_PREFIX} Suppressed leads digest ({len(leads)})"
    message = _message(subject, mailer.sender, mailer.owner, None)

    def line(lead: StoredLead) -> str:
        who = f"{lead.name} · {lead.role or '?'} at {lead.company or '?'}"
        return f"{_received(lead, timezone)} · {who} · {_triage(lead)} · {lead.id}"

    lines = [line(lead) for lead in leads]
    text = "\n".join(
        [
            "Leads triaged as spam were stored as suppressed and not emailed one by one:",
            "",
            *lines,
            "",
            "Review or restore them with GET /admin/v1/leads?status=suppressed.",
        ]
    )
    items = "".join(f"<li>{_e(entry)}</li>" for entry in lines)
    body = (
        "<html><body style='font-family:sans-serif'>"
        "<p>Leads triaged as spam were stored as suppressed and not emailed one by one:</p>"
        f"<ul>{items}</ul>"
        "<p>Review or restore them with GET /admin/v1/leads?status=suppressed.</p>"
        "</body></html>"
    )
    message.set_content(text)
    message.add_alternative(body, subtype="html")
    return message
