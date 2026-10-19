"""Ingestion T11: the local-model download script and the doctor's ingestion checks."""

from __future__ import annotations

import hashlib
import io
import zipfile
from pathlib import Path

import pytest

from scripts import doctor, models
from scripts._common import ROOT


def fake_wheel(with_config: bool = True) -> bytes:
    prefix = f"{models.SPACY_MODEL}/{models.SPACY_MODEL}-{models.SPACY_VERSION}"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        if with_config:
            archive.writestr(f"{prefix}/config.cfg", "[nlp]\n")
        archive.writestr(f"{prefix}/vocab/strings.json", "[]")
        archive.writestr(f"{models.SPACY_MODEL}/__init__.py", "")
        archive.writestr(f"{prefix}/", "")
    return buffer.getvalue()


def test_extract_and_ready(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    wheel = fake_wheel()
    monkeypatch.setattr(models, "SPACY_SHA256", hashlib.sha256(wheel).hexdigest())
    assert not models.spacy_ready(tmp_path)
    assert models.ensure_spacy(tmp_path, fetch=lambda url: wheel) is True
    target = models.spacy_path(tmp_path)
    assert (target / "config.cfg").is_file()
    assert (target / "vocab" / "strings.json").is_file()
    assert not (target / "__init__.py").exists()
    assert models.ensure_spacy(tmp_path, fetch=lambda url: b"unused") is False
    # Re-extracting replaces an existing directory.
    models.extract_spacy(wheel, target)
    assert (target / "config.cfg").is_file()


def test_checksum_and_shape_are_enforced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError, match="checksum"):
        models.ensure_spacy(tmp_path, fetch=lambda url: fake_wheel())
    with pytest.raises(ValueError, match=r"config\.cfg"):
        models.extract_spacy(fake_wheel(with_config=False), tmp_path / "m")


def test_main_reports_failures(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(models, "models_dir", lambda env=None: tmp_path)

    def broken(url: str) -> bytes:
        raise ValueError("offline")

    monkeypatch.setattr(models, "download", broken)
    monkeypatch.setattr(models.ensure_spacy, "__defaults__", (broken,))
    assert models.main([]) == 1


def test_models_dir_resolution() -> None:
    assert models.models_dir({}) == ROOT / "vault" / "models"
    assert models.models_dir({"MODELS_DIR": "/abs/m"}) == Path("/abs/m")


def _finding(tmp_path: Path, word: str) -> doctor.Finding:
    findings = doctor.check_ingestion_deps({"MODELS_DIR": str(tmp_path)})
    return next(f for f in findings if word in f.message)


def test_doctor_reports_missing_spacy_model(tmp_path: Path) -> None:
    finding = _finding(tmp_path, "spaCy")
    assert finding.level == "warn"
    assert "scripts.models" in finding.message
    models.spacy_path(tmp_path).mkdir(parents=True)
    (models.spacy_path(tmp_path) / "config.cfg").write_text("x")
    assert _finding(tmp_path, "spaCy").level == "ok"


def test_doctor_reports_missing_reranker(tmp_path: Path) -> None:
    finding = _finding(tmp_path, "reranker")
    assert finding.level == "warn"
    assert "--embeddings" in finding.message
    (tmp_path / "fastembed" / "models--Xenova--ms-marco-MiniLM-L-6-v2").mkdir(parents=True)
    assert _finding(tmp_path, "reranker").level == "ok"
