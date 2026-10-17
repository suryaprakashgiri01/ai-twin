"""Redact personal identifiers before anything is stored as text, logged or sent to a model.

Presidio finds e-mail addresses, phone numbers, cards, IBANs, IP addresses and (with the spaCy
`en_core_web_md` model) person names; the rule recognisers in `app.guardrails.identifiers`
find Indian identifiers: PAN, Aadhaar (Verhoeff-checked), UAN, IFSC, bank account, passport,
roll/enrolment numbers and dates of birth. Each match becomes a typed placeholder such as
`[AADHAAR]`. Strings on the owner's public allowlist (`PUBLIC_CONTACT_ALLOWLIST`) are kept.
The report holds counts per placeholder, never values.

Labelled identifiers (UAN, account, passport, roll number, DOB) need their label nearby, so a
bare 12-digit number in a log line is not mistaken for one; the label itself is kept.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Protocol

import structlog

from app.guardrails.identifiers import Span, rule_spans

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


class Detector(Protocol):
    def spans(self, text: str) -> list[Span]:
        """(start, end, entity, score) for every identifier found."""
        ...


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

    def spans(self, text: str) -> list[Span]:
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


def _resolve(spans: Sequence[Span]) -> list[tuple[int, int, str]]:
    """Non-overlapping spans: higher score first, then the longer span."""
    chosen: list[tuple[int, int, str]] = []
    for start, end, entity, _score in sorted(spans, key=lambda s: (-s[3], s[0] - s[1])):
        if all(end <= s or start >= e for s, e, _ in chosen):
            chosen.append((start, end, entity))
    return sorted(chosen)


class Redactor:
    def __init__(self, detector: Detector | None, allowlist: Iterable[str] = ()) -> None:
        self._detector = detector
        self._allow = {a.strip().lower() for a in allowlist if a.strip()}

    def _kept(self, value: str) -> bool:
        return value.strip().lower() in self._allow

    def redact(self, text: str) -> Redaction:
        spans: list[Span] = []
        for offset, block in _blocks(text):
            found = rule_spans(block)
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
