"""Run Ollama natively on this machine: `python -m scripts.ollama [command]`.

Ollama is the only real model allowed to read confidential text, so it runs on the host (Metal on
macOS, the GPU driver on Windows), never in the compose stack, and only on a loopback address.

- `status`  installed? answering at OLLAMA_BASE_URL? which models are pulled?
- `install` macOS: `brew install ollama` · Windows: `winget install --id Ollama.Ollama -e`
- `start`   start `ollama serve` in the background unless one answers (log: vault/logs/ollama.log)
- `stop`    stop the server `start` launched (a desktop-app or `brew services` server is left alone)
- `pull`    pull every configured Ollama model, or the one named (`pull qwen2.5:7b`)
- `smoke`   one tiny fixed prompt to each configured model; prints latency and token counts

Configured models: each Ollama entry in config/models.yaml, with OLLAMA_MODEL replacing
`ollama-local`'s and OLLAMA_QUALITY_MODEL replacing `ollama-qwen`'s.
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
# Inventory id -> the .env key that replaces its model name.
MODEL_ENV = {"ollama-local": "OLLAMA_MODEL", "ollama-qwen": "OLLAMA_QUALITY_MODEL"}
# Desktop / tray app log locations when we did not start the server ourselves.
MAC_APP_LOG = Path.home() / ".ollama" / "logs" / "server.log"
WIN_APP_LOG = Path(os.environ.get("LOCALAPPDATA", "")) / "Ollama" / "server.log"

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
    # Every configured Ollama model, `model` (the router/classifier one) first.
    models: tuple[str, ...] = ()

    @property
    def all_models(self) -> tuple[str, ...]:
        return self.models or (self.model,)

    @property
    def loopback(self) -> bool:
        return self.host in LOOPBACK


def inventory_models(path: Path = INVENTORY) -> dict[str, str]:
    """Ollama entries of the inventory: id -> model name, in file order."""
    data: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    return {
        str(spec["id"]): str(spec["model"])
        for spec in data.get("models", [])
        if spec.get("provider") == "ollama"
    }


def inventory_model(path: Path = INVENTORY) -> str:
    models = inventory_models(path)
    if "ollama-local" not in models:
        raise OllamaError("config/models.yaml has no ollama-local model")
    return models["ollama-local"]


def configured_models(env: dict[str, str], path: Path = INVENTORY) -> tuple[str, ...]:
    """Each Ollama entry's model after .env overrides; `ollama-local`'s first, no repeats."""
    found = inventory_models(path)
    ordered = ["ollama-local", *(i for i in found if i != "ollama-local")]
    names = [env.get(MODEL_ENV.get(i, "")) or found[i] for i in ordered if i in found]
    return tuple(dict.fromkeys(names))


def target(env: dict[str, str]) -> Target:
    url = (env.get("OLLAMA_BASE_URL") or DEFAULT_URL).rstrip("/")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise OllamaError(f"OLLAMA_BASE_URL is not a URL: {url}")
    port = parts.port or (443 if parts.scheme == "https" else 11434)
    model = env.get("OLLAMA_MODEL") or inventory_model()
    models = tuple(dict.fromkeys((model, *configured_models(env))))
    return Target(url=url, host=parts.hostname, port=port, model=model, models=models)


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


def launcher_and_log(*, pid_file: Path = PID_FILE) -> tuple[str, Path]:
    """Who started Ollama and which log file to read."""
    if pid_file.exists():
        return "script (make ollama ARGS=start)", LOG_FILE
    if platform.system() == "Windows":
        return "desktop / tray app (or other)", WIN_APP_LOG
    return "desktop app / brew services (or other)", MAC_APP_LOG


def cmd_status(t: Target, api: Api) -> int:
    binary = shutil.which("ollama")
    (ok if binary else warn)(f"ollama binary: {binary or 'not installed; run: ' + hint('install')}")
    who, log_path = launcher_and_log()
    info(f"Launcher: {who}")
    info(f"Log: {log_path}" + (" (exists)" if log_path.exists() else " (missing)"))
    version = api.version()
    if version is None:
        warn(f"No Ollama answering at {t.url}; run: {hint('start')}")
        return 1
    ok(f"Ollama {version} at {t.url}{'' if t.loopback else '  (NOT loopback)'}")
    pulled = api.models()
    info(f"Pulled: {', '.join(pulled) or 'none'}")
    missing = [m for m in t.all_models if not has_model(pulled, m)]
    for model in t.all_models:
        if model not in missing:
            ok(f"Configured model {model} is pulled")
    for model in missing:
        warn(f"Configured model {model} is not pulled; run: {hint('pull ' + model)}")
    return 1 if missing else 0


def cmd_logs(*, lines: int = 80, pid_file: Path = PID_FILE) -> int:
    """Print the end of the log that matches who started Ollama."""
    who, log_path = launcher_and_log(pid_file=pid_file)
    info(f"Launcher: {who}")
    if not log_path.exists():
        warn(f"No log at {log_path}")
        return 1
    text = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    tail = text[-max(1, lines) :]
    print("\n".join(tail))
    ok(f"{len(tail)} line(s) from {log_path}")
    return 0


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
    if api.version() is None:
        fail(f"No Ollama answering at {t.url}; run: {hint('start')}")
        return 1
    for name in (model,) if model else t.all_models:
        _pull_one(api, name)
    return 0


def _pull_one(api: Api, name: str) -> None:
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


def cmd_smoke(t: Target, api: Api, model: str | None = None) -> int:
    require_loopback(t)
    for name in (model,) if model else t.all_models:
        started = time.perf_counter()
        reply = api.chat(name, SMOKE_PROMPT)
        seconds = time.perf_counter() - started
        text = reply.get("message", {}).get("content", "").strip()
        tokens = reply.get("eval_count", 0)
        rate = tokens / (reply.get("eval_duration", 0) / 1e9) if reply.get("eval_duration") else 0
        ok(f"{name} answered in {seconds:.1f}s ({tokens} tokens, {rate:.0f} tok/s): {text[:40]!r}")
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
        choices=["status", "logs", "install", "start", "stop", "pull", "smoke"],
    )
    parser.add_argument(
        "model", nargs="?", help="model for `pull` / `smoke` (default: every configured one)"
    )
    parser.add_argument(
        "--tail",
        type=int,
        default=80,
        help="lines to show for `logs` (default: 80)",
    )
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
                return cmd_smoke(t, api, args.model)
            case "logs":
                return cmd_logs(lines=args.tail)
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
