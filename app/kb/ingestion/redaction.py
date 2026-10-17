"""Redact personal identifiers before anything is stored as text, logged or sent to a model.

Presidio finds e-mail addresses, phone numbers, cards, IBANs, IP addresses and (with the spaCy
`en_core_web_md` model) person names; our own recognisers find Indian identifiers: PAN,
Aadhaar (Verhoeff-checked), UAN, IFSC, bank account, passport, roll/enrolment numbers and
dates of birth. Each match becomes a typed placeholder such as `[AADHAAR]`. Strings on the
owner's public allowlist (`PUBLIC_CONTACT_ALLOWLIST`) are kept. The report holds counts per
placeholder, never values.

Labelled identifiers (UAN, account, passport, roll number, DOB) need their label nearby, so a
bare 12-digit number in a log line is not mistaken for one; the label itself is kept.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

SPACY_MODEL = "en_core_web_md"

PLACEHOLDERS: dict[str, str] = {
    "EMAIL_ADDRESS": "EMAIL",
    "PHONE_NUMBER": "PHONE",
    "PERSON": "PERSON",
    "CREDIT_CARD": "CARD",
    "IBAN_CODE": "IBAN",
    "IP_ADDRESS": "IP",
    "IN_PAN": "PAN",
    "IN_AADHAAR": "AADHAAR",
    "IN_UAN": "UAN",
    "IN_IFSC": "IFSC",
    "BANK_ACCOUNT": "BANK_ACCOUNT",
    "IN_PASSPORT": "PASSPORT",
    "ROLL_NUMBER": "ROLL_NUMBER",
    "DATE_OF_BIRTH": "DOB",
}
SCORE_THRESHOLD = 0.5
# spaCy handles at most ~1M characters per call; blocks keep memory flat on big files.
BLOCK_CHARS = 20_000

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


# --- custom recognisers -----------------------------------------------------------------------

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
    score: float = 0.85
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


def _rule_spans(text: str) -> list[tuple[int, int, str, float]]:
    spans = []
    for rule in RULES:
        for match in rule.pattern.finditer(text):
            value = match.group("value")
            if rule.check is not None and not rule.check(value):
                continue
            spans.append((match.start("value"), match.end("value"), rule.entity, rule.score))
    return spans


# --- presidio ---------------------------------------------------------------------------------


def load_nlp(model_path: Path | None) -> Any:
    """spaCy `en_core_web_md` from `model_path` (setup puts it in vault/models) or the venv."""
    import spacy

    if model_path is not None and (model_path / "config.cfg").exists():
        return spacy.load(model_path)
    if spacy.util.is_package(SPACY_MODEL):
        return spacy.load(SPACY_MODEL)
    raise RuntimeError(
        f"spaCy model {SPACY_MODEL} not found; run the setup script (make setup / dev.ps1 setup)"
    )


class PresidioDetector:
    """Presidio with only the recognisers we use, on an already-loaded spaCy pipeline."""

    def __init__(self, nlp: Any) -> None:
        from presidio_analyzer import AnalyzerEngine, RecognizerRegistry
        from presidio_analyzer.nlp_engine import SpacyNlpEngine
        from presidio_analyzer.predefined_recognizers import (
            CreditCardRecognizer,
            EmailRecognizer,
            IbanRecognizer,
            IpRecognizer,
            PhoneRecognizer,
            SpacyRecognizer,
        )

        engine = SpacyNlpEngine(models=[{"lang_code": "en", "model_name": SPACY_MODEL}])
        # Hand presidio the loaded pipeline so it never tries to download one.
        engine.nlp = {"en": nlp}  # type: ignore[assignment]  # set by load() otherwise
        registry = RecognizerRegistry(
            recognizers=[
                EmailRecognizer(),
                PhoneRecognizer(supported_regions=("IN", "US", "GB")),
                CreditCardRecognizer(),
                IbanRecognizer(),
                IpRecognizer(),
                SpacyRecognizer(supported_entities=["PERSON"]),
            ],
            supported_languages=["en"],
        )
        self._analyzer = AnalyzerEngine(
            registry=registry, nlp_engine=engine, supported_languages=["en"]
        )

    def spans(self, text: str) -> list[tuple[int, int, str, float]]:
        results = self._analyzer.analyze(text=text, language="en", score_threshold=SCORE_THRESHOLD)
        return [(r.start, r.end, r.entity_type, float(r.score)) for r in results]


# --- the redactor -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Redaction:
    text: str
    report: dict[str, int] = field(default_factory=dict)


def _blocks(text: str) -> Iterable[tuple[int, str]]:
    """(offset, block) pieces of at most ~BLOCK_CHARS, split on blank lines."""
    start = 0
    while start < len(text):
        end = min(len(text), start + BLOCK_CHARS)
        if end < len(text):
            cut = text.rfind("\n\n", start, end)
            end = cut + 2 if cut > start else end
        yield start, text[start:end]
        start = end


def _resolve(spans: Sequence[tuple[int, int, str, float]]) -> list[tuple[int, int, str]]:
    """Non-overlapping spans: higher score first, then the longer span."""
    chosen: list[tuple[int, int, str]] = []
    for start, end, entity, _score in sorted(spans, key=lambda s: (-s[3], s[0] - s[1])):
        if all(end <= s or start >= e for s, e, _ in chosen):
            chosen.append((start, end, entity))
    return sorted(chosen)


class Redactor:
    def __init__(self, detector: PresidioDetector | None, allowlist: Iterable[str] = ()) -> None:
        self._detector = detector
        self._allow = {a.strip().lower() for a in allowlist if a.strip()}

    def _kept(self, value: str) -> bool:
        return value.strip().lower() in self._allow

    def redact(self, text: str) -> Redaction:
        spans: list[tuple[int, int, str, float]] = []
        for offset, block in _blocks(text):
            found = _rule_spans(block)
            if self._detector is not None:
                found += self._detector.spans(block)
            spans += [(s + offset, e + offset, ent, sc) for s, e, ent, sc in found]
        counts: Counter[str] = Counter()
        pieces: list[str] = []
        cursor = 0
        for start, end, entity in _resolve(spans):
            if entity not in PLACEHOLDERS or self._kept(text[start:end]):
                continue
            placeholder = PLACEHOLDERS[entity]
            pieces += [text[cursor:start], f"[{placeholder}]"]
            counts[placeholder] += 1
            cursor = end
        pieces.append(text[cursor:])
        return Redaction(text="".join(pieces), report=dict(sorted(counts.items())))


@lru_cache(maxsize=4)
def default_detector(model_path: Path | None) -> PresidioDetector:
    detector = PresidioDetector(load_nlp(model_path))
    logger.info("redaction model loaded", model=SPACY_MODEL)
    return detector
