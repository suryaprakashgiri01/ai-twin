"""Outward safety for answers the owner will send to other companies.

Prep answers leave the vault by design: pasted into an application form or said in an
interview. So before one is shown, code (not the prompt) removes internal identifiers:

- employer denylist matches (internal repo, service and product names, internal hosts)
- ticket keys (`ABC-123`)
- cache and queue keys (`user:session:42`)
- event topics and similar dotted names (`orders.events.v2`)
- links to hosts that are not on the public allowlist

Each match is replaced by a generic phrase and listed, so the owner sees what changed. The
list holds the category and the replacement, never the removed text, because it is logged and
stored with the answer.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from app.kb.ingestion.guard import DenyRule

Rules = Callable[[], Sequence[DenyRule]]

DEFAULT_PUBLIC_HOSTS = (
    "github.com",
    "linkedin.com",
    "vercel.app",
    "medium.com",
    "dev.to",
    "stackoverflow.com",
    "kaggle.com",
)

_URL = re.compile(r"https?://[^\s)\]>\"']+", re.IGNORECASE)
_TICKET = re.compile(r"\b[A-Z][A-Z0-9]{1,9}-\d{1,6}\b")
_KEY = re.compile(r"\b[a-z][a-z0-9_]*(?::[a-z0-9_{}*]+){2,}\b", re.IGNORECASE)
# Three or more dot/underscore-joined lowercase parts with at least one letter-only part:
# `orders.events.v2`, `billing_retry_dlq`. Ordinary prose never looks like this.
_TOPIC = re.compile(r"\b[a-z][a-z0-9]*(?:[._][a-z0-9]+){2,}\b")
_FILE_LIKE = re.compile(r"\.(py|ts|js|md|json|yaml|yml|txt|pdf|html|go|java)$")


@dataclass(frozen=True)
class Removal:
    kind: str
    replacement: str


@dataclass(frozen=True)
class Filtered:
    text: str
    removed: list[Removal] = field(default_factory=list)

    def summary(self) -> list[str]:
        """One line per kind: "2 ticket keys → “a tracked ticket”"."""
        counts: dict[tuple[str, str], int] = {}
        for item in self.removed:
            key = (item.kind, item.replacement)
            counts[key] = counts.get(key, 0) + 1
        return [f"{n} {kind} → “{rep}”" for (kind, rep), n in counts.items()]


def _host_allowed(url: str, hosts: Iterable[str]) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in hosts)


@dataclass
class OutwardFilter:
    denylist: Rules = lambda: ()
    public_hosts: Sequence[str] = DEFAULT_PUBLIC_HOSTS
    # Words the owner allows through even though they look internal (their own repo names).
    allow: frozenset[str] = frozenset()

    def apply(self, text: str) -> Filtered:
        removed: list[Removal] = []

        def swap(kind: str, replacement: str) -> Callable[[re.Match[str]], str]:
            def replace(match: re.Match[str]) -> str:
                value = match.group(0)
                if value.lower() in self.allow:
                    return value
                removed.append(Removal(kind, replacement))
                return replacement

            return replace

        out = _URL.sub(self._url_swap(removed), text)
        for rule in self.denylist():
            out = rule.pattern.sub(swap("internal names", "an internal system"), out)
        out = _TICKET.sub(swap("ticket keys", "a tracked ticket"), out)
        out = _KEY.sub(swap("cache or queue keys", "a cache key"), out)
        out = _TOPIC.sub(self._topic_swap(swap("event topics", "an event topic")), out)
        return Filtered(text=out, removed=removed)

    def _url_swap(self, removed: list[Removal]) -> Callable[[re.Match[str]], str]:
        def replace(match: re.Match[str]) -> str:
            url = match.group(0)
            if _host_allowed(url, self.public_hosts):
                return url
            removed.append(Removal("internal links", "an internal link"))
            return "an internal link"

        return replace

    @staticmethod
    def _topic_swap(
        inner: Callable[[re.Match[str]], str],
    ) -> Callable[[re.Match[str]], str]:
        def replace(match: re.Match[str]) -> str:
            value = match.group(0)
            # File names and version-like strings are not topics.
            if _FILE_LIKE.search(value) or not re.search(r"[a-z]{3,}", value):
                return value
            return inner(match)

        return replace
