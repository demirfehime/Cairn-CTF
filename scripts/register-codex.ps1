$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$Config = Join-Path $Root "dispatch.codex.yaml"
$Database = Join-Path $Root "data\cairn.db"

if (-not (Test-Path -LiteralPath $Python)) {
    throw "Cairn virtual environment not found. Run scripts\install.ps1 first."
}

& codex mcp get cairn-ctf --json *> $null
if ($LASTEXITCODE -eq 0) {
    Write-Output "Cairn CTF MCP is already registered. Use 'codex mcp get cairn-ctf --json' to inspect it."
    exit 0
}

& codex mcp add cairn-ctf -- $Python -m cairn.codex_mcp --config $Config --db-path $Database --port 8792
Write-Output "Cairn CTF MCP registered. Restart Codex or open a new Codex session."
