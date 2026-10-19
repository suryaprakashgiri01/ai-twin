"""Shared helpers for the dev scripts: paths, coloured output, `.env`, compose runtime.

Everything here is plain Python so macOS (podman) and Windows (docker) run the same code.
No Go-template format strings: container state is read as JSON and parsed here.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, BinaryIO

ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = ROOT / "deploy" / "compose.yml"
ENV_FILE = ROOT / ".env"
ENV_EXAMPLE = ROOT / ".env.example"

COMPOSE_OVERRIDE_KEY = "AI_TWIN_COMPOSE"
# Docker first, Podman as the fallback (same order on every machine).
COMPOSE_CANDIDATES: tuple[tuple[str, ...], ...] = (
    ("docker", "compose"),
    ("docker-compose",),
    ("podman", "compose"),
)

_COLOURS = {"red": "31", "green": "32", "yellow": "33", "blue": "34", "dim": "2", "bold": "1"}


def _use_colour() -> bool:
    return sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def paint(text: str, colour: str) -> str:
    if not _use_colour():
        return text
    return f"\x1b[{_COLOURS[colour]}m{text}\x1b[0m"


def info(message: str) -> None:
    print(f"{paint('•', 'blue')} {message}")


def ok(message: str) -> None:
    print(f"{paint('✓', 'green')} {message}")


def warn(message: str) -> None:
    print(f"{paint('!', 'yellow')} {message}")


def fail(message: str) -> None:
    print(f"{paint('✗', 'red')} {message}", file=sys.stderr)


def read_env(path: Path = ENV_FILE) -> dict[str, str]:
    """Parse KEY=VALUE lines; comments and blanks are skipped, quotes are not interpreted."""
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = unquote(value.strip())
    return values


def unquote(value: str) -> str:
    """`"docker compose"` → `docker compose`, as docker compose and pydantic-settings read it."""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def resolve(argv: Sequence[str]) -> list[str] | None:
    """`argv` with its program replaced by the full path `shutil.which` finds, or None.

    Windows `CreateProcess` only appends `.exe` to a bare name, so `.cmd` / `.bat` shims (and
    anything missing) fail with WinError 2. `shutil.which` honours `PATHEXT`.
    """
    program = shutil.which(argv[0]) if argv else None
    if program and os.name == "nt" and not _windows_executable(program):
        # Docker Desktop ships an extensionless `docker` shell script beside docker.exe; some
        # Python 3.12 releases return it, and CreateProcess cannot start it.
        program = None
    return [program, *argv[1:]] if program else None


def _windows_executable(path: str) -> bool:
    exts = (os.environ.get("PATHEXT") or ".COM;.EXE;.BAT;.CMD").upper().split(";")
    return os.path.splitext(path)[1].upper() in exts


def _resolved(argv: Sequence[str]) -> list[str]:
    # Unresolvable programs keep their name so the OS error still names what is missing.
    return resolve(argv) or list(argv)


def run(
    argv: Sequence[str],
    *,
    check: bool = True,
    capture: bool = False,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a command without a shell, so quoting behaves the same on every OS."""
    try:
        return subprocess.run(
            _resolved(argv),
            check=check,
            text=True,
            capture_output=capture,
            cwd=ROOT,
            env={**os.environ, **(env or {})},
        )
    except FileNotFoundError:
        missing(argv[0])
        raise SystemExit(127) from None


def missing(program: str) -> None:
    """Name the program Windows could not start (WinError 2) instead of a bare traceback."""
    fail(f"Could not start '{program}': it is not on PATH.")
    print(f"  Check: where.exe {program}   (macOS: command -v {program})")
    print(f"  For docker / podman, also check {COMPOSE_OVERRIDE_KEY} in .env.")


def _works(argv: Sequence[str]) -> bool:
    if shutil.which(argv[0]) is None:
        return False
    try:
        result = subprocess.run(
            _resolved([*argv, "version"]), capture_output=True, text=True, timeout=15, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def detect_compose(override: str | None = None) -> tuple[str, ...] | None:
    """The first compose implementation that answers `version`; an override wins."""
    chosen = (
        override or os.environ.get(COMPOSE_OVERRIDE_KEY) or read_env().get(COMPOSE_OVERRIDE_KEY)
    )
    if chosen:
        return tuple(chosen.split())
    for candidate in COMPOSE_CANDIDATES:
        if _works(candidate):
            return candidate
    return None


def podman_docker_host() -> str | None:
    """Socket URL for standalone docker-compose driving Podman on macOS/Linux.

    `podman compose` sets this itself; plain `docker-compose` needs DOCKER_HOST pointed
    at the Podman machine's socket, read here from `podman machine inspect` JSON.
    """
    if os.environ.get("DOCKER_HOST") or shutil.which("docker") or not shutil.which("podman"):
        return None
    result = subprocess.run(
        _resolved(["podman", "machine", "inspect"]), capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        return None
    try:
        machines = json.loads(result.stdout)
        path = machines[0]["ConnectionInfo"]["PodmanSocket"]["Path"]
    except (ValueError, LookupError, TypeError):
        return None
    return f"unix://{path}" if path else None


def compose_env(prefix: Sequence[str]) -> dict[str, str]:
    if tuple(prefix) == ("docker-compose",):
        host = podman_docker_host()
        if host:
            return {"DOCKER_HOST": host, "PODMAN_COMPOSE_WARNING_LOGS": "false"}
    return {"PODMAN_COMPOSE_WARNING_LOGS": "false"}


def require_compose() -> tuple[str, ...]:
    prefix = detect_compose()
    if prefix is not None and resolve(prefix) is None:
        fail(f"'{prefix[0]}' is not on PATH (from {COMPOSE_OVERRIDE_KEY} or detection).")
        prefix = None
    if prefix is None:
        fail("No working Compose implementation found.")
        print(
            "  macOS:   brew install podman docker-compose && podman machine init"
            " && podman machine start"
        )
        print("  Windows: install Docker Desktop (it ships `docker compose`)")
        print(
            f"  Or set {COMPOSE_OVERRIDE_KEY} in .env, e.g. {COMPOSE_OVERRIDE_KEY}=podman compose"
        )
        raise SystemExit(1)
    return prefix


def _compose_argv(prefix: Sequence[str]) -> list[str]:
    argv = [*prefix, "-f", str(COMPOSE_FILE)]
    if ENV_FILE.exists():
        argv += ["--env-file", str(ENV_FILE)]
    return argv


def compose(
    *args: str, check: bool = True, capture: bool = False
) -> subprocess.CompletedProcess[str]:
    prefix = require_compose()
    return run(
        [*_compose_argv(prefix), *args], check=check, capture=capture, env=compose_env(prefix)
    )


def compose_exec_binary(
    service: str,
    command: Sequence[str],
    *,
    stdin: BinaryIO | None = None,
    stdout: BinaryIO | None = None,
) -> None:
    """`compose exec -T` with raw byte streams (dumps and restores), no shell on the host."""
    prefix = require_compose()
    try:
        subprocess.run(
            _resolved([*_compose_argv(prefix), "exec", "-T", service, *command]),
            check=True,
            stdin=stdin,
            stdout=stdout,
            cwd=ROOT,
            env={**os.environ, **compose_env(prefix)},
        )
    except FileNotFoundError:
        missing(prefix[0])
        raise SystemExit(127) from None


def parse_ps_json(text: str) -> list[dict[str, Any]]:
    """`compose ps --format json`: docker prints one object per line, podman one array."""
    text = text.strip()
    if not text:
        return []
    if text.startswith("["):
        parsed = json.loads(text)
        return [row for row in parsed if isinstance(row, dict)]
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def service_states() -> list[dict[str, str]]:
    result = compose("ps", "--all", "--format", "json", capture=True, check=False)
    if result.returncode != 0:
        return []
    rows = parse_ps_json(result.stdout)
    return [
        {
            "service": str(row.get("Service", row.get("Name", "?"))),
            "state": str(row.get("State", "?")),
            "health": str(row.get("Health", "") or "-"),
            "status": str(row.get("Status", "")),
        }
        for row in rows
    ]
