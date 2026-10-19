# Windows wrapper (PowerShell 7). Mirrors the Makefile: every command runs a Python script.
#   ./scripts/dev.ps1 setup
#   ./scripts/dev.ps1 logs mongodb --follow
#   ./scripts/dev.ps1 cleanup --apply
#   ./scripts/dev.ps1 keys create owner --scope admin
#   ./scripts/dev.ps1 models --embeddings
#   ./scripts/dev.ps1 ollama start   (status | install | start | stop | pull [model] | smoke)
#   ./scripts/dev.ps1 import vault/exports/<archive>.atv --apply
#   ./scripts/dev.ps1 seed --apply            (remove: seed --remove --apply)
#   ./scripts/dev.ps1 chat                    (chat list | chat continue <name|id> | chat rename <name|id> <new-name>)
#   ./scripts/dev.ps1 migrate status         (migrate upgrade | migrate downgrade 0003 --apply)
#   ./scripts/dev.ps1 memory                  (memory list --pending | memory add "text" | memory accept <id> | memory forget <id>)
#   ./scripts/dev.ps1 twin                    (intake assistant: add notes, records, files in plain language)
param(
    [Parameter(Position = 0)][string]$Command = "help",
    [Parameter(ValueFromRemainingArguments = $true)][string[]]$Rest
)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)

$scripts = @("setup", "doctor", "models", "ollama", "up", "down", "status", "logs", "cleanup", "check", "keys", "export", "import", "seed", "chat", "migrate", "memory")

switch ($Command) {
    "help" {
        Write-Host "Commands: $($scripts -join ', '), run, test, twin"
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
    default {
        if ($scripts -notcontains $Command) {
            Write-Error "Unknown command '$Command'. Try: ./scripts/dev.ps1 help"
        }
        # `import` is a Python keyword, so its module is `import_`.
        $module = if ($Command -eq "import") { "import_" } else { $Command }
        uv run python -m "scripts.$module" @Rest
    }
}
exit $LASTEXITCODE
