from __future__ import annotations

from typing import Any

import pytest

from app.kb.ingestion.redaction import PresidioDetector, Redactor
from tests.ingestion import persona


@pytest.fixture(scope="session")
def blank_detector() -> PresidioDetector:
    """Presidio on a blank spaCy pipeline: every pattern recogniser, no NER model download."""
    import spacy

    nlp: Any = spacy.blank("en")
    return PresidioDetector(nlp)


@pytest.fixture
def redactor(blank_detector: PresidioDetector) -> Redactor:
    return Redactor(blank_detector, allowlist=[persona.PUBLIC_EMAIL])
