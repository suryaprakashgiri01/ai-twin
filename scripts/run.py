"""Run the API locally with reload: `make run`.

On Windows, `make` exits at Ctrl+C before the server has finished stopping, so PowerShell prints
its prompt above the last shutdown lines. There, the server opens in its own PowerShell window
(`scripts/dev.ps1 run`, no make in between): Ctrl+C in that window shows the full shutdown and
then a clean prompt, and this terminal is free at once. Pass `--here` to run it in this terminal
instead. Elsewhere the server simply runs here.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from scripts._common import info

ROOT = Path(__file__).resolve().parents[1]
DEV_PS1 = ROOT / "scripts" / "dev.ps1"
# make marks every process it starts; the new window is not run by make.
MAKE_VARS = ("MAKELEVEL", "MAKEFLAGS", "MFLAGS", "MAKE_TERMOUT", "MAKE_TERMERR")


def server_command(args: Sequence[str]) -> list[str]:
    return [sys.executable, "-m", "app.main", "--reload", *args]


def window_command(shell: str) -> list[str]:
    return [
        shell,
        "-NoExit",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(DEV_PS1),
        "run",
    ]


def window_env(environ: Mapping[str, str]) -> dict[str, str]:
    return {k: v for k, v in environ.items() if k not in MAKE_VARS}


def powershell() -> str:
    """PowerShell 7 when installed, else Windows PowerShell."""
    return shutil.which("pwsh") or shutil.which("powershell") or "powershell"


def main(argv: Sequence[str] | None = None, *, os_name: str = os.name) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    here = "--here" in args
    args = [a for a in args if a != "--here"]
    if os_name == "nt" and not here:
        subprocess.Popen(
            window_command(powershell()),
            cwd=ROOT,
            env=window_env(os.environ),
            creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
        )
        info("The server opened in its own PowerShell window; Ctrl+C there stops it.")
        return 0
    # Replaces this process, so Ctrl+C and the exit code are the server's own.
    os.execv(sys.executable, server_command(args))


if __name__ == "__main__":
    sys.exit(main())
