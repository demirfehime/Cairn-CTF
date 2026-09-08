$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$VenvPython = Join-Path $Root ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $VenvPython)) {
    python -m venv (Join-Path $Root ".venv")
}

& $VenvPython -m pip install -e (Join-Path $Root "cairn") pytest httpx
& $VenvPython -m pip check

Write-Output "Cairn installed. Codex MCP config: $Root\.codex\config.toml"
