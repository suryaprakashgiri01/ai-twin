"""owner-console T7: `make run` opens its own PowerShell window on Windows, else runs here."""

from __future__ import annotations

import sys
from typing import Any

import pytest

from scripts import run


def test_windows_opens_a_new_window_without_make_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def popen(command: list[str], **kw: Any) -> None:
        seen.update(command=command, **kw)

    monkeypatch.setattr("subprocess.Popen", popen)
    monkeypatch.setattr(run, "powershell", lambda: "pwsh")
    monkeypatch.setenv("MAKELEVEL", "1")
    assert run.main([], os_name="nt") == 0
    assert seen["command"][:2] == ["pwsh", "-NoExit"]
    assert seen["command"][-2:] == [str(run.DEV_PS1), "run"]
    assert "MAKELEVEL" not in seen["env"]  # so the server there is not quieted
    assert seen["cwd"] == run.ROOT


@pytest.mark.parametrize(("os_name", "argv"), [("posix", []), ("nt", ["--here"])])
def test_otherwise_the_server_replaces_this_process(
    monkeypatch: pytest.MonkeyPatch, os_name: str, argv: list[str]
) -> None:
    seen: list[Any] = []
    monkeypatch.setattr("os.execv", lambda path, args: seen.append((path, args)))
    monkeypatch.setattr("subprocess.Popen", lambda *a, **k: pytest.fail("no new window"))
    run.main(argv, os_name=os_name)
    assert seen == [(sys.executable, [sys.executable, "-m", "app.main", "--reload"])]
