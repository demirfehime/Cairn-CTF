$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root ".venv\Scripts\python.exe"

& $Python -m pip check
& $Python -m pytest -q (Join-Path $Root "cairn\tests")
& $Python -m cairn.cli dispatch --config (Join-Path $Root "dispatch.codex.yaml") --startup-healthcheck-only

