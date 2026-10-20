"""doctor: graphify presence and Ollama context-length warning."""

from __future__ import annotations

import shutil
from typing import Any

from scripts import doctor


def test_check_graphify_ok(monkeypatch: Any) -> None:
    def which(name: str) -> str | None:
        return "/bin/graphify" if name == "graphify" else None

    monkeypatch.setattr(shutil, "which", which)
    finding = doctor.check_graphify()
    assert finding.level == "ok"


def test_check_graphify_missing(monkeypatch: Any) -> None:
    monkeypatch.setattr(shutil, "which", lambda _: None)
    finding = doctor.check_graphify()
    assert finding.level == "warn"
    assert "graphify" in finding.message


def test_context_warns_on_cpu_large(monkeypatch: Any) -> None:
    monkeypatch.setattr(shutil, "which", lambda _: None)
    finding = doctor.check_ollama_context({"OLLAMA_NUM_CTX": "32768"})
    assert finding is not None
    assert finding.level == "warn"
    assert "16k" in finding.message


def test_context_silent_when_small() -> None:
    assert doctor.check_ollama_context({"OLLAMA_NUM_CTX": "8192"}) is None
