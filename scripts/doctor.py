"""Check this machine is ready to run ai-twin: `python -m scripts.doctor`.

Reports tools, the compose runtime, the container engine, `.env` completeness (key names
only, never values), port availability, local models and the native Ollama server. Exits 1 when
something blocks `setup` or `up`, or OLLAMA_BASE_URL points off this machine.
"""

from __future__ import annotations

import shutil
import socket
import sys
from dataclasses import dataclass
from typing import Literal

from scripts import models, ollama
from scripts._common import (
    ENV_EXAMPLE,
    ENV_FILE,
    compose,
    detect_compose,
    fail,
    ok,
    read_env,
    service_states,
    warn,
)

Level = Literal["ok", "warn", "fail"]
PORT_KEYS = {
    "MONGO_PORT": 27018,
    "REDIS_PORT": 6380,
    "QDRANT_HTTP_PORT": 6334,
    "QDRANT_GRPC_PORT": 6335,
}


@dataclass(frozen=True)
class Finding:
    level: Level
    message: str


def check_python(version_info: tuple[int, ...] | None = None) -> Finding:
    parts = version_info or tuple(sys.version_info[:3])
    version = ".".join(map(str, parts))
    if parts[:2] == (3, 12):
        return Finding("ok", f"Python {version}")
    return Finding(
        "fail", f"Python {version}; ai-twin needs 3.12 (uv installs it: uv python install 3.12)"
    )


def check_uv() -> Finding:
    if shutil.which("uv"):
        return Finding("ok", "uv found")
    return Finding("fail", "uv not found: https://docs.astral.sh/uv/getting-started/installation/")


def check_compose() -> Finding:
    prefix = detect_compose()
    if prefix is None:
        return Finding(
            "fail", "no compose runtime (docker compose, docker-compose or podman compose)"
        )
    return Finding("ok", f"compose runtime: {' '.join(prefix)}")


def check_engine() -> Finding:
    if detect_compose() is None:
        return Finding("warn", "container engine not checked (no compose runtime)")
    if not ENV_FILE.exists():
        # compose refuses to parse the stack file without the generated secrets.
        return Finding("warn", "container engine not checked until .env exists")
    result = compose("ps", "--format", "json", capture=True, check=False)
    if result.returncode == 0:
        return Finding("ok", "container engine reachable")
    hint = "podman machine start" if shutil.which("podman") else "start Docker Desktop"
    return Finding("fail", f"container engine not reachable; try: {hint}")


def check_env(env: dict[str, str], example: dict[str, str]) -> list[Finding]:
    if not env:
        return [Finding("warn", ".env missing; the setup script creates it")]
    findings = [Finding("ok", ".env present")]
    missing = sorted(set(example) - set(env))
    if missing:
        findings.append(Finding("warn", f".env lacks keys from .env.example: {', '.join(missing)}"))
    placeholders = sorted(k for k, v in env.items() if v == "CHANGE_ME")
    if placeholders:
        findings.append(Finding("fail", f".env still has CHANGE_ME for: {', '.join(placeholders)}"))
    return findings


def check_ingestion_deps(env: dict[str, str]) -> list[Finding]:
    """The vault dependency group and the spaCy model; missing ones only block ingestion."""
    findings: list[Finding] = []
    try:
        import presidio_analyzer  # noqa: F401
        import spacy  # noqa: F401
    except ImportError:
        findings.append(Finding("warn", "vault dependencies missing; run: uv sync --all-groups"))
    base = models.models_dir(env)
    if models.spacy_ready(base):
        findings.append(Finding("ok", f"spaCy {models.SPACY_MODEL} present"))
    else:
        findings.append(
            Finding(
                "warn",
                f"spaCy {models.SPACY_MODEL} missing (ingestion cannot redact); "
                "run: uv run python -m scripts.models",
            )
        )
    if models.reranker_ready(base):
        findings.append(Finding("ok", "reranker model present"))
    else:
        findings.append(
            Finding(
                "warn",
                "reranker model missing (downloads on the first owner chat); "
                "run: uv run python -m scripts.models --embeddings",
            )
        )
    return findings


def port_free(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.3)
        return sock.connect_ex((host, port)) != 0


def check_ports(env: dict[str, str], stack_running: bool) -> list[Finding]:
    findings: list[Finding] = []
    for key, default in PORT_KEYS.items():
        port = int(env.get(key, default))
        if port_free(port):
            findings.append(Finding("ok", f"port {port} ({key}) free"))
        elif stack_running:
            findings.append(Finding("ok", f"port {port} ({key}) in use by the running stack"))
        else:
            findings.append(Finding("warn", f"port {port} ({key}) busy; change {key} in .env"))
    return findings


def check_ollama(env: dict[str, str], api: ollama.Api | None = None) -> list[Finding]:
    """Native Ollama: the only real model that may read confidential text."""
    if not env.get("OLLAMA_BASE_URL"):
        return [Finding("ok", "Ollama not configured (confidential sources will be withheld)")]
    try:
        target = ollama.target(env)
    except ollama.OllamaError as exc:
        return [Finding("fail", str(exc))]
    if not target.loopback:
        return [Finding("fail", f"OLLAMA_BASE_URL host {target.host} is not this machine")]
    api = api or ollama.Api(target)
    version = api.version()
    if version is None:
        return [
            Finding("warn", f"Ollama not answering at {target.url}; run: {ollama.hint('start')}")
        ]
    pulled = api.models()
    missing = [m for m in target.all_models if not ollama.has_model(pulled, m)]
    if missing:
        return [
            Finding("warn", f"Ollama model {m} not pulled; run: {ollama.hint('pull ' + m)}")
            for m in missing
        ]
    return [Finding("ok", f"Ollama {version} with {', '.join(target.all_models)}")]


def check_graphify() -> Finding:
    if shutil.which("graphify"):
        return Finding("ok", "graphify found")
    return Finding(
        "warn",
        "graphify not found (code-graph make targets need it); install the CLI then retry",
    )


def check_ollama_context(env: dict[str, str]) -> Finding | None:
    """Warn when a large context is configured on a CPU-only machine."""
    raw = env.get("OLLAMA_NUM_CTX") or env.get("OLLAMA_CONTEXT_LENGTH") or ""
    try:
        ctx = int(raw) if raw.strip() else 0
    except ValueError:
        return Finding("warn", f"OLLAMA_NUM_CTX is not an integer: {raw!r}")
    if ctx <= 16_000:
        return None
    # Heuristic: no CUDA/Metal env and no nvidia-smi → treat as CPU-only.
    has_gpu = bool(shutil.which("nvidia-smi")) or bool(env.get("CUDA_VISIBLE_DEVICES"))
    if has_gpu:
        return None
    return Finding(
        "warn",
        f"Ollama context {ctx} is above 16k on a CPU-only host; title refresh and intake "
        "will be slow — prefer 8192",
    )


def collect() -> list[Finding]:
    env = read_env(ENV_FILE)
    findings = [check_python(), check_uv(), check_compose(), check_engine()]
    findings += check_env(env, read_env(ENV_EXAMPLE))
    running = (
        any(row["state"] == "running" for row in service_states())
        if findings[3].level == "ok"
        else False
    )
    findings += check_ports(env, running)
    findings += check_ingestion_deps(env)
    findings += check_ollama(env)
    findings.append(check_graphify())
    if ctx := check_ollama_context(env):
        findings.append(ctx)
    return findings


def main() -> int:
    findings = collect()
    printer = {"ok": ok, "warn": warn, "fail": fail}
    for finding in findings:
        printer[finding.level](finding.message)
    return 1 if any(f.level == "fail" for f in findings) else 0


if __name__ == "__main__":
    raise SystemExit(main())
