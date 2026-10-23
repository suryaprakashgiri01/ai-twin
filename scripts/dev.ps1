# Windows wrapper (PowerShell 7). Mirrors the Makefile: every command runs a Python script.
#   ./scripts/dev.ps1 setup
#   ./scripts/dev.ps1 logs mongodb --follow
#   ./scripts/dev.ps1 cleanup --apply
#   ./scripts/dev.ps1 keys create owner --scope admin
#   ./scripts/dev.ps1 models --embeddings
#   ./scripts/dev.ps1 ollama start   (status | logs | install | start | stop | pull [model] | smoke)
#   ./scripts/dev.ps1 graph update   (graph query … | graph explain … | graph path A B)
#   ./scripts/dev.ps1 import vault/exports/<archive>.atv --apply
#   ./scripts/dev.ps1 seed --apply            (remove: seed --remove --apply)
#   ./scripts/dev.ps1 chat                    (chat list | chat continue <name|id> | chat rename <name|id> <new-name>)
#   ./scripts/dev.ps1 migrate status         (migrate upgrade | migrate downgrade 0003 --apply)
#   ./scripts/dev.ps1 memory                  (memory list --pending | memory add "text" | memory accept <id> | memory forget <id>)
#   ./scripts/dev.ps1 twin                    (intake assistant: add notes, records, files in plain language)
#   ./scripts/dev.ps1 prep                    (interview and application answers in your voice)
#   ./scripts/dev.ps1 super                   (super agent: saves, answers and prepares in one place)
param(
    [Parameter(Position = 0)][string]$Command = "help",
    [Parameter(ValueFromRemainingArguments = $true)][string[]]$Rest
)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)

$scripts = @("setup", "doctor", "models", "ollama", "up", "down", "status", "logs", "cleanup", "check", "keys", "export", "import", "seed", "kb-sync", "models-check", "eval", "chat", "migrate", "memory", "graph")

switch ($Command) {
    "help" {
        Write-Host "Commands: $($scripts -join ', '), run, test, twin, prep, super, graph-update/query/explain/path via: graph <subcommand>"
    }
    "run" {
        uv run python -m app.main --reload
    }
    "test" {
        uv run pytest @Rest
    }
    "twin" {
        uv run python -m scripts.chat twin @Rest
    }
    "prep" {
        uv run python -m scripts.chat prep @Rest
    }
    "super" {
        uv run python -m scripts.chat super @Rest
    }
    default {
        if ($scripts -notcontains $Command) {
            Write-Error "Unknown command '$Command'. Try: ./scripts/dev.ps1 help"
        }
        # `import` is a Python keyword, so its module is `import_`.
        $module = if ($Command -eq "import") { "import_" } else { $Command -replace "-", "_" }
        uv run python -m "scripts.$module" @Rest
    }
}
exit $LASTEXITCODE
