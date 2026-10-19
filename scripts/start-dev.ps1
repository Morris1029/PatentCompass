[CmdletBinding()]
param(
    [switch]$SkipBuild,
    [switch]$ServicesOnly,
    [int]$Port = 8000
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$apiRoot = Join-Path $projectRoot 'api'
$composeFile = Join-Path $projectRoot 'deploy/compose.yml'

function Invoke-Checked {
    param([string]$Executable, [string[]]$Arguments)
    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Executable exited with code $LASTEXITCODE"
    }
}

if (-not (Test-Path -LiteralPath (Join-Path $apiRoot '.env'))) {
    Copy-Item -LiteralPath (Join-Path $apiRoot '.env.example') -Destination (Join-Path $apiRoot '.env')
}
New-Item -ItemType Directory -Force -Path (Join-Path $projectRoot 'storage') | Out-Null
Push-Location $apiRoot
try {
    Invoke-Checked 'uv' @('sync', '--frozen')
    Invoke-Checked 'docker' @('compose', '-f', $composeFile, 'up', '-d', '--wait', 'postgres', 'redis')
    Invoke-Checked 'uv' @('run', '--frozen', 'alembic', 'upgrade', 'head')
    $workerArgs = @('compose', '-f', $composeFile, 'up', '-d', 'worker')
    if (-not $SkipBuild) { $workerArgs += '--build' }
    Invoke-Checked 'docker' $workerArgs
    if (-not $ServicesOnly) {
        Write-Host "PatentCompass API: http://127.0.0.1:$Port/docs"
        Write-Host 'Press Ctrl+C to stop the local API. Docker services remain available.'
        Invoke-Checked 'uv' @('run', '--frozen', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', "$Port", '--reload')
    }
} finally {
    Pop-Location
}
