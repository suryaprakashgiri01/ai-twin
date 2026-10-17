# Plan: local-ollama

- `scripts/ollama.py`: argparse subcommands; `httpx` against `OLLAMA_BASE_URL` (`/api/version`, `/api/tags`,
  `/api/pull` streamed, `/api/chat`); `subprocess.Popen` for `ollama serve` (`start_new_session` on POSIX,
  `CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS` on Windows) with `OLLAMA_HOST=<host>:<port>`; pid file and log under
  `vault/` (git-ignored, outside vault export's data). HTTP transport and launcher are injectable for tests.
- Model name: `OLLAMA_MODEL` if set, else `config/models.yaml` `ollama-local.model`.
- `scripts/doctor.py`: `check_ollama(env, probe)` with a 1 s probe.
- `Makefile` target `ollama`, `scripts/dev.ps1` command `ollama`.
- Risk: a 3B model needs ~3 GB of RAM; machines short on memory set `OLLAMA_MODEL=llama3.2:1b`.
