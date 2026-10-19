"""Start the local stack and wait until every service is healthy: `python -m scripts.up`."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import time

from scripts._common import ENV_FILE, compose, fail, ok, service_states, warn
from scripts.status import render


def all_healthy(rows: list[dict[str, str]]) -> bool:
    return bool(rows) and all(
        row["state"] == "running" and row["health"] in ("healthy", "-") for row in rows
    )


def wait_healthy(timeout_seconds: float, interval_seconds: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if all_healthy(service_states()):
            return True
        time.sleep(interval_seconds)
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=120, help="seconds to wait for health")
    args = parser.parse_args(argv)

    if not ENV_FILE.exists():
        fail(
            ".env is missing. Run the setup script first: "
            "make setup  (Windows: ./scripts/dev.ps1 setup)"
        )
        return 1
    try:
        compose("up", "-d")
    except subprocess.CalledProcessError:
        hint = "podman machine start" if shutil.which("podman") else "start Docker Desktop"
        fail(f"Could not start the stack; is the container engine running? Try: {hint}")
        print("Then: make up  (Windows: ./scripts/dev.ps1 up). Details: make doctor")
        return 1
    if wait_healthy(args.timeout):
        ok("Stack is healthy")
        print(render(service_states()))
        return 0
    warn(f"Not healthy after {args.timeout:.0f}s. Current state:")
    print(render(service_states()))
    print("Inspect with: make logs  (Windows: ./scripts/dev.ps1 logs)")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
