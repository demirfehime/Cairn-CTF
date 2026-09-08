param(
    [int]$Port = 8792
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root ".venv\Scripts\python.exe"

& $Python -m cairn.cli run `
    --config (Join-Path $Root "dispatch.codex.yaml") `
    --db-path (Join-Path $Root "data\cairn.db") `
    --port $Port
