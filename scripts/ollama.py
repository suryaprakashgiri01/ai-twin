"""Run Ollama natively on this machine: `python -m scripts.ollama [command]`.

Ollama is the only real model allowed to read confidential text, so it runs on the host (Metal on
macOS, the GPU driver on Windows), never in the compose stack, and only on a loopback address.

- `status`  installed? answering at OLLAMA_BASE_URL? which models are pulled?
- `install` macOS: `brew install ollama` · Windows: `winget install --id Ollama.Ollama -e`
- `start`   start `ollama serve` in the background unless one answers (log: vault/logs/ollama.log)
- `stop`    stop the server `start` launched (a desktop-app or `brew services` server is left alone)
- `pull`    pull OLLAMA_MODEL (else the inventory's `ollama-local` model)
- `smoke`   one tiny fixed prompt; prints latency and token counts
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import yaml

from scripts._common import ROOT, fail, info, ok, read_env, run, warn

DEFAULT_URL = "http://127.0.0.1:11434"
INVENTORY = ROOT / "config" / "models.yaml"
LOG_FILE = ROOT / "vault" / "logs" / "ollama.log"
PID_FILE = ROOT / "vault" / "run" / "ollama.pid"
LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})
SMOKE_PROMPT = "Reply with the single word: ready"

Launcher = Callable[[list[str], dict[str, str], Path], int]


def hint(command: str) -> str:
    return f'make ollama ARGS="{command}"  (Windows: ./scripts/dev.ps1 ollama {command})'


class OllamaError(Exception):
    """A problem the user can fix; the message says how."""


@dataclass(frozen=True)
class Target:
    url: str
    host: str
    port: int
    model: str

    @property
    def loopback(self) -> bool:
        return self.host in LOOPBACK


def inventory_model(path: Path = INVENTORY) -> str:
    data: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    for spec in data.get("models", []):
        if spec.get("id") == "ollama-local":
            return str(spec["model"])
    raise OllamaError("config/models.yaml has no ollama-local model")


def target(env: dict[str, str]) -> Target:
    url = (env.get("OLLAMA_BASE_URL") or DEFAULT_URL).rstrip("/")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise OllamaError(f"OLLAMA_BASE_URL is not a URL: {url}")
    port = parts.port or (443 if parts.scheme == "https" else 11434)
    model = env.get("OLLAMA_MODEL") or inventory_model()
    return Target(url=url, host=parts.hostname, port=port, model=model)


def require_loopback(t: Target) -> None:
    if not t.loopback:
        raise OllamaError(
            f"OLLAMA_BASE_URL points at {t.host}; confidential text may only go to a model on this "
            "machine. Use http://127.0.0.1:11434"
        )


class Api:
    def __init__(self, t: Target, transport: httpx.BaseTransport | None = None) -> None:
        self.t = t
        self._transport = transport

    def _client(self, timeout: float) -> httpx.Client:
        return httpx.Client(base_url=self.t.url, timeout=timeout, transport=self._transport)

    def version(self, timeout: float = 1.0) -> str | None:
        try:
            with self._client(timeout) as client:
                response = client.get("/api/version")
                response.raise_for_status()
                return str(response.json().get("version", "?"))
        except (httpx.HTTPError, ValueError):
            return None

    def models(self) -> list[str]:
        with self._client(5.0) as client:
            response = client.get("/api/tags")
            response.raise_for_status()
            return [m["name"] for m in response.json().get("models", [])]

    def pull(self, model: str, progress: Callable[[dict[str, Any]], None]) -> None:
        with (
            self._client(None) as client,  # type: ignore[arg-type]
            client.stream("POST", "/api/pull", json={"model": model, "stream": True}) as response,
        ):
            response.raise_for_status()
            for line in response.iter_lines():
                if not line:
                    continue
                event = json.loads(line)
                if "error" in event:
                    raise OllamaError(f"pull failed: {event['error']}")
                progress(event)

    def chat(self, model: str, prompt: str) -> dict[str, Any]:
        body = {"model": model, "stream": False, "messages": [{"role": "user", "content": prompt}]}
        with self._client(300.0) as client:
            response = client.post("/api/chat", json=body)
            response.raise_for_status()
            result: dict[str, Any] = response.json()
            return result


def has_model(pulled: list[str], model: str) -> bool:
    """`llama3.2:3b` matches itself; a bare `name` matches `name:latest`."""
    wanted = model if ":" in model else f"{model}:latest"
    return wanted in pulled


# --- commands ----------------------------------------------------------------------------------


def cmd_status(t: Target, api: Api) -> int:
    binary = shutil.which("ollama")
    (ok if binary else warn)(f"ollama binary: {binary or 'not installed; run: ' + hint('install')}")
    version = api.version()
    if version is None:
        warn(f"No Ollama answering at {t.url}; run: {hint('start')}")
        return 1
    ok(f"Ollama {version} at {t.url}{'' if t.loopback else '  (NOT loopback)'}")
    pulled = api.models()
    info(f"Pulled: {', '.join(pulled) or 'none'}")
    if has_model(pulled, t.model):
        ok(f"Configured model {t.model} is pulled")
        return 0
    warn(f"Configured model {t.model} is not pulled; run: {hint('pull')}")
    return 1


def install_argv(system: str) -> list[str] | None:
    if system == "Darwin" and shutil.which("brew"):
        return ["brew", "install", "ollama"]
    if system == "Windows" and shutil.which("winget"):
        return ["winget", "install", "--id", "Ollama.Ollama", "-e"]
    return None


def cmd_install(system: str | None = None) -> int:
    if shutil.which("ollama"):
        ok("Ollama is already installed")
        return 0
    argv = install_argv(system or platform.system())
    if argv is None:
        fail("No supported package manager found. Install from https://ollama.com/download")
        return 1
    info(f"Running: {' '.join(argv)}")
    return run(argv, check=False).returncode


def default_launcher(argv: list[str], env: dict[str, str], log: Path) -> int:
    log.parent.mkdir(parents=True, exist_ok=True)
    kwargs: dict[str, Any] = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS  # type: ignore[attr-defined]
    else:
        kwargs["start_new_session"] = True
    with log.open("ab") as out:
        process = subprocess.Popen(
            argv, env={**os.environ, **env}, stdout=out, stderr=subprocess.STDOUT, **kwargs
        )
    return process.pid


def cmd_start(
    t: Target,
    api: Api,
    launcher: Launcher = default_launcher,
    *,
    timeout: float = 30.0,
    pid_file: Path = PID_FILE,
) -> int:
    require_loopback(t)
    if api.version() is not None:
        ok(f"Ollama already answers at {t.url}; nothing started")
        return 0
    binary = shutil.which("ollama")
    if binary is None:
        fail(f"Ollama is not installed; run: {hint('install')}")
        return 1
    pid = launcher([binary, "serve"], {"OLLAMA_HOST": f"{t.host}:{t.port}"}, LOG_FILE)
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(str(pid), encoding="utf-8")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if (version := api.version()) is not None:
            ok(f"Ollama {version} started (pid {pid}) at {t.url}; log: {LOG_FILE}")
            return 0
        time.sleep(0.5)
    fail(f"Ollama did not answer within {timeout:.0f}s; see {LOG_FILE}")
    return 1


def cmd_stop(pid_file: Path = PID_FILE, kill: Callable[[int, int], None] = os.kill) -> int:
    if not pid_file.exists():
        info(
            "No server started by this script. A desktop app is quit from its menu bar / tray "
            "icon; a Homebrew service with: brew services stop ollama"
        )
        return 0
    pid = int(pid_file.read_text(encoding="utf-8").strip())
    try:
        kill(pid, signal.SIGTERM)
        ok(f"Stopped Ollama (pid {pid})")
    except ProcessLookupError:
        info(f"Ollama (pid {pid}) was not running")
    pid_file.unlink()
    return 0


def cmd_pull(t: Target, api: Api, model: str | None) -> int:
    name = model or t.model
    if api.version() is None:
        fail(f"No Ollama answering at {t.url}; run: {hint('start')}")
        return 1
    info(f"Pulling {name}")
    last = {"status": ""}

    def progress(event: dict[str, Any]) -> None:
        status = event.get("status", "")
        total, done = event.get("total"), event.get("completed")
        if total and done:
            print(f"\r  {status}: {done * 100 // total}%   ", end="", flush=True)
        elif status != last["status"]:
            print(f"\n  {status}", end="", flush=True)
        last["status"] = status

    api.pull(name, progress)
    print()
    ok(f"{name} is ready")
    return 0


def cmd_smoke(t: Target, api: Api) -> int:
    require_loopback(t)
    started = time.perf_counter()
    reply = api.chat(t.model, SMOKE_PROMPT)
    seconds = time.perf_counter() - started
    text = reply.get("message", {}).get("content", "").strip()
    tokens = reply.get("eval_count", 0)
    rate = tokens / (reply.get("eval_duration", 0) / 1e9) if reply.get("eval_duration") else 0
    ok(f"{t.model} answered in {seconds:.1f}s ({tokens} tokens, {rate:.0f} tok/s): {text[:40]!r}")
    return 0


def main(
    argv: list[str] | None = None,
    *,
    transport: httpx.BaseTransport | None = None,
    launcher: Launcher = default_launcher,
) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument(
        "command",
        nargs="?",
        default="status",
        choices=["status", "install", "start", "stop", "pull", "smoke"],
    )
    parser.add_argument("model", nargs="?", help="model for `pull` (default: the configured one)")
    args = parser.parse_args(argv)
    try:
        t = target(read_env())
        api = Api(t, transport)
        match args.command:
            case "install":
                return cmd_install()
            case "start":
                return cmd_start(t, api, launcher)
            case "stop":
                return cmd_stop()
            case "pull":
                return cmd_pull(t, api, args.model)
            case "smoke":
                return cmd_smoke(t, api)
            case _:
                return cmd_status(t, api)
    except OllamaError as exc:
        fail(str(exc))
        return 1
    except httpx.HTTPError as exc:
        fail(f"Ollama request failed: {type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
