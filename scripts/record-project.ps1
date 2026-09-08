param(
    [Parameter(Mandatory = $true)][string]$ProjectId,
    [string]$Server = "http://127.0.0.1:8792",
    [string]$OutputDir = ""
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not $OutputDir) {
    $OutputDir = Join-Path $Root "records\$ProjectId"
}

& $Python -m cairn.cli record `
    --server $Server `
    --project-id $ProjectId `
    --output-dir $OutputDir `
    --follow
