"""Rule recognisers for Indian identifiers, shared by ingestion redaction and the output guard.

Pure regular expressions and a Verhoeff check: no Presidio or spaCy import, so the public
deployment can scan answers without the vault's NLP stack. PAN, Aadhaar (Verhoeff-checked),
IFSC and Indian mobile numbers match on their own; UAN, bank account, passport, roll or
enrolment number and date of birth need their label nearby, so a bare 12-digit number in a
log line is not mistaken for one. The label itself is not part of the match.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

Span = tuple[int, int, str, float]

# --- Verhoeff (Aadhaar check digit) ---------------------------------------------------------
_D = [
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9],
    [1, 2, 3, 4, 0, 6, 7, 8, 9, 5],
    [2, 3, 4, 0, 1, 7, 8, 9, 5, 6],
    [3, 4, 0, 1, 2, 8, 9, 5, 6, 7],
    [4, 0, 1, 2, 3, 9, 5, 6, 7, 8],
    [5, 9, 8, 7, 6, 0, 4, 3, 2, 1],
    [6, 5, 9, 8, 7, 1, 0, 4, 3, 2],
    [7, 6, 5, 9, 8, 2, 1, 0, 4, 3],
    [8, 7, 6, 5, 9, 3, 2, 1, 0, 4],
    [9, 8, 7, 6, 5, 4, 3, 2, 1, 0],
]
_P = [
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9],
    [1, 5, 7, 6, 2, 8, 3, 0, 9, 4],
    [5, 8, 0, 3, 7, 9, 6, 1, 4, 2],
    [8, 9, 1, 6, 0, 4, 3, 5, 2, 7],
    [9, 4, 5, 3, 1, 2, 6, 8, 7, 0],
    [4, 2, 8, 6, 5, 7, 3, 9, 0, 1],
    [2, 7, 9, 3, 8, 0, 6, 4, 1, 5],
    [7, 0, 4, 6, 9, 1, 3, 2, 5, 8],
]
_INV = [0, 4, 3, 2, 1, 5, 6, 7, 8, 9]


def _verhoeff(digits: str, offset: int) -> int:
    check = 0
    for i, digit in enumerate(reversed(digits)):
        check = _D[check][_P[(i + offset) % 8][int(digit)]]
    return check


def verhoeff_check_digit(digits: str) -> str:
    return str(_INV[_verhoeff(digits, 1)])


def verhoeff_valid(digits: str) -> bool:
    return digits.isdigit() and _verhoeff(digits, 0) == 0


# --- recognisers -------------------------------------------------------------------------------

_DATE = (
    r"\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2,4}"
    r"|\d{4}-\d{2}-\d{2}"
    r"|\d{1,2}(?:st|nd|rd|th)?\s+[A-Za-z]{3,9}\.?,?\s+\d{4}"
    r"|[A-Za-z]{3,9}\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4}"
)
_GAP = r"[\s:#.\-]{0,6}(?:is\s+|no\.?\s*|number\s*|num\s*)?[\s:#.\-]{0,4}"


@dataclass(frozen=True)
class Rule:
    entity: str
    pattern: re.Pattern[str]
    # Above spaCy NER's 0.85, so a precise identifier pattern wins an overlap with a guessed
    # PERSON/ORG span and keeps its own placeholder type.
    score: float = 0.95
    # Match only counts when this returns True for the value (e.g. a checksum).
    check: Any = None


def _labelled(label: str, value: str) -> re.Pattern[str]:
    return re.compile(rf"(?i:\b(?:{label})){_GAP}(?P<value>{value})")


RULES: tuple[Rule, ...] = (
    Rule("IN_PAN", re.compile(r"\b(?P<value>[A-Z]{3}[ABCFGHJLPT][A-Z]\d{4}[A-Z])\b")),
    Rule(
        "IN_AADHAAR",
        re.compile(r"(?<![\d-])(?P<value>[2-9]\d{3}[ -]?\d{4}[ -]?\d{4})(?![\d-])"),
        check=lambda v: verhoeff_valid(re.sub(r"\D", "", v)),
    ),
    Rule("IN_IFSC", re.compile(r"\b(?P<value>[A-Z]{4}0[A-Z0-9]{6})\b")),
    # Indian mobile numbers; presidio's generic phone score sits under the threshold for them.
    Rule(
        "PHONE_NUMBER",
        re.compile(r"(?<![\d+])(?P<value>(?:\+91[\s-]?|0)?[6-9]\d{4}[\s-]?\d{5})(?!\d)"),
        score=0.7,
    ),
    Rule("IN_UAN", _labelled(r"UAN|universal\s+account", r"\d{12}")),
    Rule(
        "BANK_ACCOUNT",
        _labelled(r"(?:bank\s+)?a/?c|(?:bank\s+)?account|acct", r"\d{9,18}"),
    ),
    Rule("IN_PASSPORT", _labelled(r"passport", r"[A-Z][0-9]{7}")),
    Rule(
        "ROLL_NUMBER",
        _labelled(
            r"roll|enrol(?:l)?ment|registration|reg\.?|seat|admission",
            r"(?=[A-Z0-9/\-]*\d)[A-Z0-9][A-Z0-9/\-]{3,19}",
        ),
    ),
    Rule(
        "DATE_OF_BIRTH",
        _labelled(r"d\.?o\.?b|date\s+of\s+birth|birth\s*date|born(?:\s+on)?", _DATE),
    ),
)


def rule_spans(text: str, rules: tuple[Rule, ...] | None = None) -> list[Span]:
    """(start, end, entity, score) for every rule match; `rules` defaults to `RULES`."""
    spans = []
    for rule in RULES if rules is None else rules:
        for match in rule.pattern.finditer(text):
            value = match.group("value")
            if rule.check is not None and not rule.check(value):
                continue
            spans.append((match.start("value"), match.end("value"), rule.entity, rule.score))
    return spans


# E-mail addresses: ingestion leaves them to Presidio; the output guard scans answers for them.
EMAIL_RULE = Rule(
    "EMAIL_ADDRESS",
    re.compile(r"(?<![\w.+-])(?P<value>[\w.+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,})"),
)
OUTPUT_RULES: tuple[Rule, ...] = (*RULES, EMAIL_RULE)
