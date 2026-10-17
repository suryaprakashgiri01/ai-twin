"""Employer guard: the owner's denylist of internal domains, ticket keys and system names.

The denylist lives at `vault/config/denylist.yaml` (git-ignored) and is re-read when it
changes. A match quarantines the document; only rule *names* are stored or logged, never the
matched text. Shape (see `config/denylist.example.yaml`):

    rules:
      - name: internal-domain
        regex: '\\bintranet\\.example-corp\\.test\\b'
      - name: internal-services
        terms: [bluebird-gateway, orca-billing]     # literal, case-insensitive, whole words
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import structlog
import yaml

logger = structlog.get_logger(__name__)


class DenylistError(ValueError):
    """The denylist file is malformed; the message names the rule, never a pattern."""


@dataclass(frozen=True)
class DenyRule:
    name: str
    pattern: re.Pattern[str]


def _compile(entry: object, index: int) -> DenyRule:
    if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
        raise DenylistError(f"denylist rule #{index} needs a string `name`")
    name = entry["name"]
    regex, terms = entry.get("regex"), entry.get("terms")
    if (regex is None) == (terms is None):
        raise DenylistError(f"denylist rule {name!r} needs exactly one of `regex` or `terms`")
    try:
        if regex is not None:
            return DenyRule(name, re.compile(str(regex), re.IGNORECASE))
        if not isinstance(terms, list) or not terms:
            raise DenylistError(f"denylist rule {name!r}: `terms` must be a non-empty list")
        alternatives = "|".join(re.escape(str(t)) for t in terms)
        return DenyRule(name, re.compile(rf"(?<!\w)(?:{alternatives})(?!\w)", re.IGNORECASE))
    except re.error as exc:
        raise DenylistError(f"denylist rule {name!r} has an invalid regex") from exc


def parse_denylist(text: str) -> list[DenyRule]:
    data = yaml.safe_load(text) or {}
    if not isinstance(data, dict):
        raise DenylistError("denylist must be a mapping with a `rules` list")
    rules = data.get("rules") or []
    if not isinstance(rules, list):
        raise DenylistError("denylist `rules` must be a list")
    compiled = [_compile(entry, i) for i, entry in enumerate(rules)]
    names = [r.name for r in compiled]
    if len(set(names)) != len(names):
        raise DenylistError("denylist rule names must be unique")
    return compiled


@dataclass
class EmployerGuard:
    """Matches text against the denylist file, reloading it when its mtime changes."""

    path: Path
    _rules: list[DenyRule] = field(default_factory=list)
    _mtime: float | None = None

    def rules(self) -> list[DenyRule]:
        try:
            mtime = self.path.stat().st_mtime
        except FileNotFoundError:
            if self._mtime is not None or not self._rules:
                logger.warning("employer denylist missing; guard has no rules", path=str(self.path))
            self._rules, self._mtime = [], None
            return []
        if mtime != self._mtime:
            self._rules = parse_denylist(self.path.read_text(encoding="utf-8"))
            self._mtime = mtime
            logger.info("employer denylist loaded", rules=len(self._rules))
        return self._rules

    def matches(self, *texts: str) -> list[str]:
        """Sorted names of the rules any of `texts` matches."""
        hits = {rule.name for rule in self.rules() for text in texts if rule.pattern.search(text)}
        return sorted(hits)
