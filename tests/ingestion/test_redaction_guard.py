"""Ingestion T5: redaction (Presidio + Indian identifiers + allowlist) and the employer guard."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from app.guardrails.identifiers import verhoeff_check_digit, verhoeff_valid
from app.kb.ingestion.guard import DenylistError, EmployerGuard, parse_denylist
from app.kb.ingestion.redaction import Redactor, default_detector, load_nlp
from tests.ingestion import persona

EXAMPLE_DENYLIST = Path(__file__).resolve().parents[2] / "config" / "denylist.example.yaml"


def test_every_identifier_becomes_a_placeholder(redactor: Redactor) -> None:
    result = redactor.redact(persona.sensitive_note())
    for raw in persona.RAW_VALUES:
        assert raw not in result.text
    assert persona.aadhaar() not in result.text
    for placeholder in (
        "[PAN]",
        "[AADHAAR]",
        "[BANK_ACCOUNT]",
        "[IFSC]",
        "[UAN]",
        "[PASSPORT]",
        "[DOB]",
        "[ROLL_NUMBER]",
        "[EMAIL]",
        "[PHONE]",
    ):
        assert placeholder in result.text, placeholder
    # Labels stay; only values go.
    assert "UAN: [UAN]" in result.text
    assert "DOB: [DOB]" in result.text
    assert result.report["PAN"] == 1
    assert sum(result.report.values()) == 10


def test_allowlisted_public_email_survives(redactor: Redactor) -> None:
    result = redactor.redact(persona.sensitive_note())
    assert persona.PUBLIC_EMAIL in result.text
    assert result.report["EMAIL"] == 1


def test_clean_text_is_unchanged(redactor: Redactor) -> None:
    text = "Built a rate limiter; p99 fell from 120 ms to 40 ms across 12 services in 2024."
    result = redactor.redact(text)
    assert result.text == text
    assert result.report == {}


@pytest.mark.parametrize(
    "text",
    [
        "order 123456789012 shipped",  # 12 digits, fails Verhoeff
        "build 100200300400 passed",  # UAN-shaped but no label
        "ticket 2013CS0420",  # roll-shaped but no label
        "released 14/03/1995",  # a date, not a birth date
    ],
)
def test_unlabelled_or_invalid_numbers_are_kept(redactor: Redactor, text: str) -> None:
    assert redactor.redact(text).text == text


class GuessingDetector:
    """Stands in for spaCy NER labelling an identifier as a PERSON (seen with real PDFs)."""

    def spans(self, text: str) -> list[tuple[int, int, str, float]]:
        start = text.index(persona.PAN)
        return [(start, start + len(persona.PAN), "PERSON", 0.85)]


def test_identifier_rules_win_over_ner_guesses() -> None:
    redactor = Redactor(GuessingDetector(), allowlist=[])
    result = redactor.redact(f"PAN: {persona.PAN}")
    assert result.text == "PAN: [PAN]"
    assert result.report == {"PAN": 1}


def test_verhoeff() -> None:
    assert verhoeff_check_digit("236") == "3"
    assert verhoeff_valid("2363")
    assert not verhoeff_valid("2364")
    assert not verhoeff_valid("23a4")


def test_large_text_is_redacted_in_blocks(redactor: Redactor) -> None:
    filler = ("lorem ipsum dolor sit amet\n" * 40 + "\n") * 30
    text = filler + f"PAN {persona.PAN}\n\n" + filler + f"PAN {persona.PAN}"
    result = redactor.redact(text)
    assert result.report == {"PAN": 2}
    assert persona.PAN not in result.text


def test_redactor_without_presidio_uses_rules_only() -> None:
    result = Redactor(None).redact(f"PAN {persona.PAN}, mail {persona.PRIVATE_EMAIL}")
    assert "[PAN]" in result.text
    assert persona.PRIVATE_EMAIL in result.text  # e-mail needs presidio


def test_missing_model_is_a_clear_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import spacy

    monkeypatch.setattr(spacy.util, "is_package", lambda name: False)
    with pytest.raises(RuntimeError, match="setup"):
        load_nlp(tmp_path / "absent")


@pytest.mark.integration
def test_real_model_redacts_person_names() -> None:
    model = Path(os.environ.get("SPACY_MODEL_PATH", "vault/models/spacy/en_core_web_md"))
    try:
        detector = default_detector(model)
    except RuntimeError:
        pytest.skip("spaCy en_core_web_md not installed (run setup)")
    redactor = Redactor(detector, allowlist=[persona.PUBLIC_EMAIL, persona.NAME])
    text = (
        f"{persona.NAME} paired with Rahul Sharma on the outage. "
        + persona.sensitive_note()
        + f"\nKavya Testpal - Resume\nPAN: {persona.PAN}"
    )
    result = redactor.redact(text)
    assert "Rahul Sharma" not in result.text
    assert "[PERSON]" in result.text
    assert persona.NAME in result.text
    assert "PAN: [PAN]" in result.text
    for raw in persona.RAW_VALUES:
        assert raw not in result.text


# --- employer guard ---------------------------------------------------------------------------


def test_example_denylist_parses_and_matches() -> None:
    rules = parse_denylist(EXAMPLE_DENYLIST.read_text(encoding="utf-8"))
    assert {r.name for r in rules} == {"internal-domain", "ticket-keys", "internal-services"}
    guard = EmployerGuard(EXAMPLE_DENYLIST)
    assert guard.matches("see wiki.example-corp.test and FAKEPROJ-123") == [
        "internal-domain",
        "ticket-keys",
    ]
    assert guard.matches("the Bluebird-Gateway timed out") == ["internal-services"]
    assert guard.matches("bluebird-gateways", "a personal project") == []


def test_guard_reloads_when_the_file_changes(tmp_path: Path) -> None:
    path = tmp_path / "denylist.yaml"
    guard = EmployerGuard(path)
    assert guard.matches("anything") == []  # missing file: no rules
    path.write_text("rules:\n  - name: one\n    terms: [alpha]\n", encoding="utf-8")
    assert guard.matches("Alpha release") == ["one"]
    path.write_text("rules:\n  - name: two\n    regex: 'beta\\d'\n", encoding="utf-8")
    later = time.time() + 5
    os.utime(path, (later, later))
    assert guard.matches("alpha beta7") == ["two"]
    path.unlink()
    assert guard.matches("beta7") == []


@pytest.mark.parametrize(
    "text",
    [
        "- just a list",
        "rules: nope",
        "rules:\n  - regex: x",
        "rules:\n  - name: a\n    regex: x\n    terms: [y]",
        "rules:\n  - name: a",
        "rules:\n  - name: a\n    terms: []",
        "rules:\n  - name: a\n    regex: '('",
        "rules:\n  - name: a\n    regex: x\n  - name: a\n    regex: y",
    ],
)
def test_malformed_denylists_are_refused(text: str) -> None:
    with pytest.raises(DenylistError):
        parse_denylist(text)
