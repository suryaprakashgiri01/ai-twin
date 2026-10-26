# Spec: dev-graphify-ollama

Status: approved via plan · Owner: Surya Prakash Giri · Ticket: none

## Problem
1. Graphify is used by agents for codebase orientation, but there are no make / PowerShell
   wrappers, so owners must remember the CLI by hand and `doctor` never says whether it is
   installed.
2. When the desktop / tray Ollama app starts the server, `make ollama ARGS=start` does nothing
   and `vault/logs/ollama.log` is never written, so `logs` and status give no path to the real
   log (`~/.ollama/logs/server.log` on macOS, `%LOCALAPPDATA%\Ollama\server.log` on Windows).
3. A CPU-only machine with a context length above 16k is painfully slow; `doctor` does not warn.

## Goals
1. `scripts/graph.py` with `make graph-update`, `graph-query`, `graph-explain`, `graph-path`
   (and the same via `scripts/dev.ps1`). `doctor` reports whether graphify is on PATH.
2. `make ollama ARGS="logs"` / `status` report who started Ollama and which log file applies;
   `logs --tail N` prints the end of that file.
3. `doctor` warns when the configured Ollama context length is above 16k on a CPU-only host.

## Non-goals
API contract, docs rewrite, changing how Ollama is started on the desktop app.

## Acceptance criteria
1. `make graph-update` and `graph-query ARGS="…"`. run the graphify CLI (or print an install
   hint when missing).
2. `ollama status` names the launcher (script vs desktop) and the log path; `ollama logs`
   tails that file.
3. `doctor` includes a graphify finding and a context-length warning when applicable.
4. Unit tests cover log path resolution and the doctor findings with fakes.
