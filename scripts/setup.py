"""First-time setup: `.env`, dependencies, git hooks, local stack: `python -m scripts.setup`.

Idempotent: an existing `.env` is kept, so re-running never rotates working secrets. Keys added
to `.env.example` since it was written are appended (secrets generated), existing values untouched.
"""

from __future__ import annotations

import argparse
import base64
import secrets
from collections.abc import Callable
from pathlib import Path

from scripts import up
from scripts._common import ENV_EXAMPLE, ENV_FILE, ROOT, info, ok, read_env, run, warn

PLACEHOLDER = "CHANGE_ME"


def fernet_key() -> str:
    """A Fernet key: 32 random bytes, urlsafe base64 (what `Fernet.generate_key()` returns)."""
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")


# Secrets with a required shape; every other placeholder gets a random token.
SECRET_GENERATORS: dict[str, Callable[[], str]] = {"BLOB_ENCRYPTION_KEY": fernet_key}


def _secret_for(key: str, make_secret: Callable[[], str] | None) -> str:
    generator = SECRET_GENERATORS.get(key)
    if generator is not None:
        return generator()
    return make_secret() if make_secret else secrets.token_urlsafe(24)


def render_env(template: str, make_secret: Callable[[], str] | None = None) -> str:
    """Fill every `KEY=CHANGE_ME` with a random value, then rebuild the connection strings
    from those values so the app and the containers always agree."""
    lines = template.splitlines()
    values: dict[str, str] = {}
    for i, line in enumerate(lines):
        key, sep, value = line.partition("=")
        if sep and not line.lstrip().startswith("#") and value.strip() == PLACEHOLDER:
            values[key.strip()] = _secret_for(key.strip(), make_secret)
            lines[i] = f"{key}={values[key.strip()]}"

    env = {**_parse(lines), **values}
    derived = {
        "MONGODB_URI": (
            f"mongodb://{env.get('MONGO_ROOT_USERNAME', 'ai_twin')}:{env['MONGO_ROOT_PASSWORD']}"
            f"@127.0.0.1:{env.get('MONGO_PORT', '27018')}/?authSource=admin"
        ),
        "REDIS_URL": (
            f"redis://:{env['REDIS_PASSWORD']}@127.0.0.1:{env.get('REDIS_PORT', '6380')}/0"
        ),
        "QDRANT_URL": f"http://127.0.0.1:{env.get('QDRANT_HTTP_PORT', '6334')}",
    }
    for i, line in enumerate(lines):
        key = line.partition("=")[0].strip()
        if key in derived and not line.lstrip().startswith("#"):
            lines[i] = f"{key}={derived[key]}"
    return "\n".join(lines) + "\n"


def _parse(lines: list[str]) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for line in lines:
        if line.strip() and not line.lstrip().startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            parsed[key.strip()] = value.strip()
    return parsed


def missing_lines(
    current: dict[str, str], template: str, make_secret: Callable[[], str] | None = None
) -> list[str]:
    """`KEY=value` lines for template keys absent from `current`, secrets generated."""
    added: list[str] = []
    for key, value in _parse(template.splitlines()).items():
        if key in current:
            continue
        added.append(f"{key}={_secret_for(key, make_secret) if value == PLACEHOLDER else value}")
    return added


def append_missing(env_file: Path = ENV_FILE, example: Path = ENV_EXAMPLE) -> list[str]:
    """Append keys new in the example to an existing `.env`. Returns the key names added."""
    added = missing_lines(read_env(env_file), example.read_text(encoding="utf-8"))
    if not added:
        return []
    text = env_file.read_text(encoding="utf-8")
    prefix = "" if text.endswith("\n") or not text else "\n"
    block = "\n".join(["# --- added by setup (new in .env.example) ---", *added])
    env_file.write_text(f"{text}{prefix}{block}\n", encoding="utf-8")
    return [line.partition("=")[0] for line in added]


def ensure_env(env_file: Path = ENV_FILE, example: Path = ENV_EXAMPLE) -> bool:
    """Create `.env` from the example. Returns True when a new file was written."""
    if env_file.exists():
        added = append_missing(env_file, example)
        if added:
            ok(f".env: added {', '.join(added)} (values not printed)")
        leftover = [k for k, v in read_env(env_file).items() if v == PLACEHOLDER]
        if leftover:
            warn(f".env still has {PLACEHOLDER} for: {', '.join(leftover)}")
        return False
    env_file.write_text(render_env(example.read_text(encoding="utf-8")), encoding="utf-8")
    # Owner-only: it holds the database passwords. (No effect on Windows ACLs.)
    env_file.chmod(0o600)
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-stack", action="store_true", help="skip starting the containers")
    args = parser.parse_args(argv)

    if ensure_env():
        ok(".env created with generated secrets (values not printed)")
    else:
        info(".env exists, kept as is")

    info("Installing dependencies (uv sync)")
    run(["uv", "sync", "--all-groups"])
    if (ROOT / ".git").exists():
        run(["uv", "run", "pre-commit", "install"])
        ok("pre-commit hooks installed")

    if args.no_stack:
        return 0
    return up.main([])


if __name__ == "__main__":
    raise SystemExit(main())
