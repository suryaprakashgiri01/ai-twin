# Spec: local-ollama

Status: approved by the owner in chat · Owner: Surya Prakash Giri · Ticket: none (personal repo; `tasks.md` is the task list)

## Problem
Ollama is the only real model allowed to read confidential text, but nothing in the repo helps run it. Without it,
owner-chat answers withhold confidential sources, and labelling falls back to rules. Setting it up by hand differs
on macOS and Windows, and a container is the wrong home for it: on macOS a container gets no GPU (Metal), and the
podman VM has a few GB of RAM.

## Goals
1. `python -m scripts.ollama <command>` (Make and `dev.ps1` wrappers) manages a **native** Ollama on the host:
   - `status` (default): whether the binary is installed and the server answers at `OLLAMA_BASE_URL`; version;
     pulled models; whether the configured model is among them.
   - `install`: macOS `brew install ollama`; Windows `winget install --id Ollama.Ollama -e`; other systems get the
     official instructions printed (no piped shell installers).
   - `start`: if the server already answers, nothing to do. Otherwise start `ollama serve` detached, bound to the
     host and port in `OLLAMA_BASE_URL`, with its log under `vault/logs/ollama.log` and its pid in
     `vault/run/ollama.pid`. Wait until it answers.
   - `stop`: stop the server this script started (by pid file). A server it did not start (the desktop app,
     `brew services`) is left alone, and the script says how to stop it.
   - `pull [model]`: pull the configured model (`OLLAMA_MODEL`, else the inventory's `ollama-local` model) through
     the HTTP API, with progress.
   - `smoke`: one tiny fixed prompt through `/api/chat`; reports latency and token counts. No vault content.
2. **Loopback only.** Confidential text goes to Ollama, so `start` and `smoke` refuse an `OLLAMA_BASE_URL` whose
   host is not `127.0.0.1`, `localhost` or `::1`.
3. `doctor` reports Ollama: not configured (info), configured but not answering (warn, with the start command), model
   not pulled (warn, with the pull command), non-loopback URL (fail).

## Non-goals
Running Ollama in a container, GPU tuning, choosing models per machine beyond `OLLAMA_MODEL`, a Linux service unit.

## Acceptance criteria
1. On a machine with Ollama installed, `start` → `pull` → `smoke` brings up a working local model, and owner chat
   answers a confidential question with `done.tier: confidential` and `model: ollama-local`.
2. `start` when a server already answers starts nothing; `stop` only stops a server the script started.
3. A non-loopback `OLLAMA_BASE_URL` is refused by `start`/`smoke` and fails `doctor`.
4. Every command has a macOS and a Windows (PowerShell 7) form in the dev-notes.
5. `make check` passes; unit tests use a mock HTTP transport and fake process launcher (no real Ollama needed).
