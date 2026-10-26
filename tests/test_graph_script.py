"""scripts.graph: missing binary exits with a hint; subcommands forward argv."""

from __future__ import annotations

import shutil
from typing import Any

from scripts import graph


def test_missing_graphify_prints_hint(monkeypatch: Any, capsys: Any) -> None:
    monkeypatch.setattr(shutil, "which", lambda _: None)
    assert graph.main(["update"]) == 1
    captured = capsys.readouterr()
    assert "graphify not found" in captured.out + captured.err


def test_update_forwards_to_cli(monkeypatch: Any) -> None:
    calls: list[list[str]] = []

    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/graphify")

    class Result:
        returncode = 0

    def fake_run(argv: list[str], **_: Any) -> Result:
        calls.append(list(argv))
        return Result()

    monkeypatch.setattr(graph, "run", fake_run)
    assert graph.main(["update"]) == 0
    assert calls == [["/usr/bin/graphify", "update", "."]]


def test_query_joins_words(monkeypatch: Any) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(shutil, "which", lambda _: "graphify")

    class Result:
        returncode = 0

    def fake_run(argv: list[str], **_: Any) -> Result:
        calls.append(list(argv))
        return Result()

    monkeypatch.setattr(graph, "run", fake_run)
    assert graph.main(["query", "how", "does", "intake", "work"]) == 0
    assert calls[0][1:] == ["query", "how does intake work"]
