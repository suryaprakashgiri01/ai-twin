# Windows wrapper (PowerShell 7). Mirrors the Makefile: every command runs a Python script.
#   ./scripts/dev.ps1 setup
#   ./scripts/dev.ps1 logs mongodb --follow
#   ./scripts/dev.ps1 cleanup --apply
param(
    [Parameter(Position = 0)][string]$Command = "help",
    [Parameter(ValueFromRemainingArguments = $true)][string[]]$Rest
)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)

$scripts = @("setup", "doctor", "up", "down", "status", "logs", "cleanup", "check")

switch ($Command) {
    "help" {
        Write-Host "Commands: $($scripts -join ', '), run, test"
    }
    "run" {
        $env:RELOAD = "true"
        uv run python -m app.main
    }
    "test" {
        uv run pytest @Rest
    }
    default {
        if ($scripts -notcontains $Command) {
            Write-Error "Unknown command '$Command'. Try: ./scripts/dev.ps1 help"
        }
        uv run python -m "scripts.$Command" @Rest
    }
}
exit $LASTEXITCODE
