param([switch]$Install)
$ErrorActionPreference = "Stop"
$workspacePath = Split-Path -Parent $PSScriptRoot
Push-Location (Join-Path $workspacePath "web")
try {
    if ($Install -or -not (Test-Path -LiteralPath "node_modules")) {
        pnpm install --frozen-lockfile
        if ($LASTEXITCODE -ne 0) { throw "Frontend dependency installation failed." }
    }
    pnpm dev
} finally { Pop-Location }
